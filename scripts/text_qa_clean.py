"""Stage02 text-only judging, chained to Stage01 accepted rows and receipts."""
import argparse
from collections import Counter
import hashlib
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import threading
from urllib.parse import urlparse

PROJECT = Path(__file__).resolve().parents[1]
REPO = PROJECT
import mechanical_clean as core
from text_checks import extract_pairs
from text_contract import FIELDS, COT, LANGUAGES, parse, issues_for

PROMPT = PROJECT/'prompts/text_qa_quality_v1.txt'
DEFAULT = {'stage1_delivery': None, 'output': None, 'api_base': 'http://127.0.0.1:8018/v1',
           'model': 'Qwen3.8-27B', 'model_revision': None, 'workers': 64, 'part_rows': 128,
           'scratch_root': None, 'scratch_limit_mb': 8192, 'timeout_seconds': 120,
           'max_input_chars': 24000, 'minimum_score': None}


def emit(event, value):
    print('[text-qa-clean] '+event+' '+json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def api_location(base):
    p = urlparse(base)
    core.require(p.scheme == 'http' and p.hostname in ('127.0.0.1', 'localhost', '::1')
                 and not p.username and not p.password and not p.query and not p.fragment
                 and p.path.rstrip('/') == '/v1', 'v1 only supports explicit local HTTP vLLM; no external API/proxy')
    return p.hostname, p.port or 80


def config(path):
    core.require(path.stat().st_size <= 128*1024, 'config too large')
    c = json.loads(path.read_bytes())
    core.require(isinstance(c, dict) and set(c) == set(DEFAULT), 'config keys differ from template')
    for name in ('stage1_delivery', 'output'):
        core.require(isinstance(c[name], str) and Path(c[name]).is_absolute()
                     and core.outside_repo(c[name]), name+' needs external absolute path')
        c[name] = str(Path(c[name]).resolve())
    core.require(isinstance(c['model'], str) and c['model'].strip(), 'served model name required')
    core.require(isinstance(c['model_revision'], str) and c['model_revision'].strip(),
                 'declare a frozen model revision/checksum; served alias alone cannot identify weights')
    api_location(c['api_base'])
    core.require(type(c['workers']) is int and 1 <= c['workers'] <= 512, 'workers must be1..512')
    core.require(type(c['part_rows']) is int and c['workers'] <= c['part_rows'] <= 4096,
                 'part_rows must be >=workers and <=4096')
    core.require(type(c['scratch_limit_mb']) is int and 16 <= c['scratch_limit_mb'] <= 131072,
                 'invalid scratch limit')
    core.require(type(c['timeout_seconds']) is int and 1 <= c['timeout_seconds'] <= 600, 'invalid timeout')
    core.require(type(c['max_input_chars']) is int and 100 <= c['max_input_chars'] <= 262144, 'invalid input size cap')
    core.require(c['minimum_score'] is None or (type(c['minimum_score']) is int
                 and 1 <= c['minimum_score'] <= 10), 'minimum_score must be null or1..10')
    if c['scratch_root'] is not None:
        core.require(isinstance(c['scratch_root'], str) and Path(c['scratch_root']).is_absolute()
                     and Path(c['scratch_root']).is_dir() and core.outside_repo(c['scratch_root']),
                     'scratch root must exist outside code')
    parent = Path(c['stage1_delivery'])
    receipt = json.loads((parent/'receipt.json').read_bytes())
    identity_path = parent.parent/'identity.json'
    core.require(core.digest_file(identity_path) == receipt['identity_sha256'], 'stage1 identity receipt mismatch')
    identity = json.loads(identity_path.read_bytes())
    core.require('clarity' in identity['config'] and 'text' in identity['config'], 'upstream must be mechanical Stage01')
    core.require(identity['limit'] is None, 'do not treat Stage01 pilot as full Stage02 input')
    for name in ('summary.json',):
        info = receipt['files'][name]
        core.require(core.digest_file(parent/name) == info['sha256'], 'stage1 summary changed')
    source = parent/'accepted.jsonl'
    core.require(source.is_file() and core.stamp(source)['bytes'] == receipt['files']['accepted.jsonl']['bytes'],
                 'stage1 accepted size mismatch')
    output = Path(c['output'])
    protected = [parent.parent, Path(identity['config']['source'])]
    protected += [Path(x).resolve() for x in identity['config']['allowed_media_roots']]
    core.require(not any(output == p or p in output.parents for p in protected), 'output inside immutable upstream/media')
    if c['scratch_root'] is not None:
        scratch = Path(c['scratch_root']).resolve()
        core.require(not any(scratch == p or p in scratch.parents for p in protected), 'scratch inside immutable upstream/media')
    c.update(source=str(source), exact_duplicate=False,
             parent_receipt_sha256=core.digest_file(parent/'receipt.json'),
             expected_source_sha256=receipt['files']['accepted.jsonl']['sha256'])
    return c


class Judge:
    def __init__(self, c, runtime, cache_path):
        self.config, self.runtime = c, runtime
        self.prompt = PROMPT.read_text(encoding='utf-8')
        self.db = sqlite3.connect(str(cache_path), check_same_thread=False)
        self.lock = threading.Lock(); self.local = threading.local(); self.clients = []
        try:
            self._build_lineage(cache_path)
        except BaseException:
            self.db.close()
            raise

    def _build_lineage(self, cache_path):
        c = self.config
        self.db.execute('CREATE TABLE lineage(idx INTEGER PRIMARY KEY, original_row INTEGER, sha TEXT)')
        parent = Path(c['stage1_delivery'])
        r = json.loads((parent/'receipt.json').read_bytes())
        core.require(core.digest_file(parent/'receipt.json') == c['parent_receipt_sha256'], 'stage1 receipt changed')
        h = hashlib.sha256(); n = 0
        with (parent/'audit.jsonl').open('rb') as f:
            for raw in f:
                core.require(len(raw) <= core.MAX_LINE, 'stage1 audit line too large')
                h.update(raw); a = json.loads(raw)
                if a['status'] == 'accepted':
                    self.db.execute('INSERT INTO lineage VALUES (?,?,?)', (n, a['source_row'], a['raw_sha256']))
                    n += 1
                    if n % 4096 == 0:
                        self.db.commit(); core.check_scratch(Path(cache_path).parent, c['scratch_limit_mb'])
        core.require(h.hexdigest() == r['files']['audit.jsonl']['sha256'], 'stage1 audit SHA mismatch')
        summary = json.loads((parent/'summary.json').read_bytes())
        core.require(n == summary['counts'].get('accepted', 0), 'stage1 lineage count mismatch')
        self.db.commit()

    def lineage(self, index, raw_sha):
        with self.lock:
            value = self.db.execute('SELECT original_row,sha FROM lineage WHERE idx=?', (index,)).fetchone()
        core.require(value is not None and value[1] == raw_sha, 'stage1 row-to-QA lineage mismatch')
        return value[0]

    def request(self, payload):
        if not getattr(self.local, 'client', None):
            host, port = api_location(self.config['api_base'])
            self.local.client = http.client.HTTPConnection(host, port, timeout=self.config['timeout_seconds'])
            with self.lock:
                self.clients.append(self.local.client)
        client = self.local.client
        body = json.dumps({'model': self.config['model'],
              'messages': [{'role': 'system', 'content': self.prompt}, {'role': 'user', 'content': payload}],
              'temperature': 0, 'max_tokens': 32,
              'chat_template_kwargs': {'enable_thinking': False}}, ensure_ascii=False).encode()
        try:
            client.request('POST', '/v1/chat/completions', body, {'Content-Type': 'application/json'})
            response = client.getresponse(); raw = response.read(65537)
            core.require(len(raw) <= 65536, 'response exceeds64KiB')
            core.require(response.status == 200, 'HTTP status '+str(response.status))
            obj = json.loads(raw, object_pairs_hook=core.unique_keys,
                             parse_constant=core.invalid_number, parse_float=core.safe_float)
            return obj['choices'][0]['message']['content'], obj.get('usage', {})
        except Exception:
            # No implicit replay after uncertain execution, including keepalive errors.
            client.close(); self.local.client = None
            with self.lock:
                self.clients.remove(client)
            raise

    def score(self, question, answer, previous, existing_cot=None):
        payload = json.dumps({'previous_qa': previous, 'question': question, 'answer': answer,
                              'existing_cot_fields': existing_cot or {}}, ensure_ascii=False)
        if len(payload) > self.config['max_input_chars']:
            return {'status': 'input_too_long', 'model_calls': 0}
        try:
            response, usage = self.request(payload)
            labels = parse(response)
            safe_usage = {k: v for k, v in usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                          and type(v) is int and v >= 0}
            return {'status': 'ok', 'model_calls': 1, 'labels': labels, 'usage': safe_usage}
        except Exception as exc:
            return {'status': 'model_error', 'model_calls': 1,
                    'error_type': type(exc).__name__, 'error': str(exc)[:200]}

    def close(self):
        self.db.close()
        for client in self.clients:
            client.close()


def analyse(item, c, judge):
    index, raw = item
    a = {'source_row': index, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
         'issues': [], 'qa_pairs': 0, 'text_metrics': [], 'media': [], 'duplicate_key': None,
         'qa_labels': []}
    a['original_source_row'] = judge.lineage(index, a['raw_sha256'])
    row = json.loads(raw.decode('utf-8'))
    try:
        pairs = extract_pairs(row)
    except ValueError as exc:
        a['issues'].append({'reason': 'unsupported_qa_structure', 'action': 'review', 'error': str(exc)})
        return raw, a
    a['qa_pairs'] = len(pairs)
    previous = []
    for j, (q, answer) in enumerate(pairs):
        attached, attachment_issues = cot_fields(row, j, len(pairs))
        a['issues'].extend(dict(issue, qa_index=j) for issue in attachment_issues)
        result = judge.score(q, answer, previous, attached)
        result['qa_index'] = j
        a['qa_labels'].append(result)
        if result['status'] == 'ok':
            a['issues'].extend(dict(issue, qa_index=j) for issue in issues_for(result['labels'], c['minimum_score']))
        else:
            a['issues'].append({'reason': result['status'], 'action': 'review', 'qa_index': j})
        previous.append({'question': q, 'answer': answer})
    return raw, a


def cot_fields(row, index, qa_count):
    attached, issues = {}, []
    for key in ('cot', 'reasoning_process', 'rationale'):
        value = row.get(key)
        if value is None or value == '':
            continue
        if isinstance(value, list) and len(value) == qa_count:
            value = value[index]
        elif qa_count != 1:
            issues.append({'reason': 'ambiguous_cot_attachment', 'action': 'review'})
            continue
        if value is None or value == '':
            continue
        if not isinstance(value, str):
            issues.append({'reason': 'unsupported_cot_field', 'action': 'review'})
        else:
            attached[key] = value
    return attached, issues


def summary_builder(c):
    def build(summary, parts):
        core.require(summary['source_hash_scope'] != 'entire_file'
                     or summary['source_sha256_scanned'] == c['expected_source_sha256'], 'stage1 accepted SHA changed')
        parent = Path(c['stage1_delivery'])
        core.require(core.digest_file(parent/'receipt.json') == c['parent_receipt_sha256'], 'stage1 receipt changed')
        bins = {'quality_score': list(range(1,11)), 'qa_match': [0,1,2], 'refusal': [0,1,2,3],
                'obvious_error': [0,1,2], 'cot_class': [0,1,2,'unknown'], 'language': [0,1,2,3,4]}
        totals = {f: Counter({str(k): 0 for k in bins[f]}) for f in FIELDS}
        passed = {f: Counter({str(k): 0 for k in bins[f]}) for f in FIELDS}
        states = Counter(); calls = 0; usage = Counter()
        for part in sorted(parts.glob('part-*')):
            with (part/'audit.jsonl').open('rb') as f:
                for raw in f:
                    a = json.loads(raw)
                    for item in a['qa_labels']:
                        states[item['status']] += 1; calls += item['model_calls']
                        usage.update(item.get('usage', {}))
                        if item['status'] == 'ok':
                            for field in FIELDS:
                                key = 'unknown' if item['labels'][field] is None else str(item['labels'][field])
                                totals[field][key] += 1
                                if a['status'] == 'accepted':
                                    passed[field][key] += 1
        summary.update(stage_name='第二步：纯文本QA质量清洗', stage_id='stage02_text_qa',
                       status='TEXT_QA_STAGE_COMPLETE' if summary['rows'] else 'EMPTY_UPSTREAM',
                       stage_limitations='仅读取QA及此前对话文本；未验证媒体事实，不生成或补写CoT。',
                       model_calls=calls, model_status_counts=dict(states), token_usage=dict(usage),
                       qa_distributions={k: dict(v) for k,v in totals.items()},
                       accepted_qa_distributions={k: dict(v) for k,v in passed.items()},
                       label_legend={'cot': COT, 'language': LANGUAGES},
                       qa_display_names={'quality_score': '质量分', 'qa_match': 'QA匹配', 'refusal': '拒答类型',
                           'obvious_error': '文本可证明错误', 'cot_class': 'CoT类别', 'language': '语言'},
                       qa_value_names={'qa_match': {'0':'不匹配','1':'匹配','2':'未定'},
                           'refusal': {'0':'正常回答','1':'无效拒答','2':'合理拒答','3':'未定'},
                           'obvious_error': {'0':'未发现可证明错误','1':'明确错误','2':'未定'},
                           'cot_class': {'0':'已有有效CoT','1':'无CoT且不适合补充','2':'无CoT但适合补充','unknown':'未定'},
                           'language': {'0':'未定','1':'中文','2':'英文','3':'其他','4':'混合'}},
                       parent_stage='stage01_mechanical', parent_receipt_sha256=c['parent_receipt_sha256'],
                       minimum_score=c['minimum_score'], media_reads=0, cot_generated=0)
        upstream = json.loads((parent/'summary.json').read_bytes())
        summary['upstream_counts'] = {'stage1_input_rows': upstream['rows'],
                                     'stage1_accepted_rows': upstream['counts'].get('accepted', 0)}
        summary['cumulative_retained_fraction_rows'] = (
            summary['counts'].get('accepted', 0)/upstream['rows']
            if summary['scope'] == 'full' and upstream['rows'] else None)
        return summary
    return build


def write_attributes(stage, parts, summary):
    h = hashlib.sha256(); total = 0
    name = 'attributes.jsonl'
    with (stage/name).open('xb') as out:
        for part in sorted(parts.glob('part-*')):
            with (part/'audit.jsonl').open('rb') as f:
                for raw in f:
                    a = json.loads(raw)
                    for qa in a['qa_labels']:
                        value = {'stage1_accepted_row': a['source_row'],
                                 'original_source_row': a['original_source_row'], 'source_row_sha256': a['raw_sha256'],
                                 'qa_index': qa['qa_index'], 'row_status': a['status'],
                                 'label_status': qa['status'], 'labels': qa.get('labels'),
                                 'cot_candidate': a['status'] == 'accepted' and qa.get('labels', {}).get('cot_class') == 2}
                        data = core.encoded(value); out.write(data); h.update(data); total += len(data)
        out.flush()
        os.fsync(out.fileno())
    return {name: {'sha256': h.hexdigest(), 'bytes': total}}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init-config'); init.add_argument('path', type=Path)
    for name in ('pilot', 'run'):
        cmd = commands.add_parser(name); cmd.add_argument('--config', type=Path, required=True)
        if name == 'pilot':
            cmd.add_argument('--rows', type=int, default=100)
    a = p.parse_args()
    if a.command == 'init-config':
        core.require(core.outside_repo(a.path), 'private config must be outside code')
        core.save(a.path, DEFAULT); emit('CONFIG_CREATED', {'path': str(a.path.resolve())}); return
    c = config(a.config)
    limit = a.rows if a.command == 'pilot' else None
    core.require(limit is None or 1 <= limit <= 10000, 'pilot rows must be1..10000')
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=REPO, timeout=10, text=True).strip()
    core.require(not dirty, 'formal run requires clean checkout; preserve unrelated changes')
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, timeout=10, text=True).strip()
    runtime = {'stage': 'text_qa_v1', 'api_base': c['api_base'], 'model': c['model'],
               'declared_model_revision': c['model_revision'], 'model_weights_verified': False,
               'prompt_sha256': core.digest_file(PROMPT), 'producer_commit': commit}
    files = [Path(__file__), Path(__file__).with_name('text_contract.py'), Path(core.__file__)]
    files += [Path(core.__file__).with_name('text_checks.py'), Path(core.__file__).with_name('media_checks.py')]
    identity = {str(x.relative_to(REPO)): core.digest_file(x) for x in files}
    root = Path(c['output'])/('pilot-%d' % limit if limit else 'full')
    emit('SCOPE', {'media_reads': 0, 'cot_generated': 0, 'upstream': c['stage1_delivery'],
                   'workers': c['workers'], 'minimum_score': c['minimum_score']})
    core.run(c, root, runtime, limit, Judge, identity, analyse, summary_builder(c), emit, write_attributes,
             allow_empty=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('FAIL', {'error': type(exc).__name__+': '+str(exc), 'original_writes': 0})
        sys.exit(1)

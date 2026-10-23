"""Resumable mechanical gates for image/video QA. Standard library, no model.

Original row bytes are routed intact; audit metadata never enters training rows.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

from text_checks import check_pairs, extract_pairs, fingerprint, normalize
from media_checks import Inspector, REPO, references, resolve, blur

MAX_LINE = 16*1024*1024
MAX_BATCH_BYTES = 16*1024*1024
CATEGORIES = ('accepted', 'rejected', 'review')
DEFAULT = {
    'source': None, 'media_root': None, 'allowed_media_roots': [], 'output': None,
    'workers': 4, 'part_rows': 128, 'timeout_seconds': 30,
    'scratch_root': None, 'scratch_limit_mb': 8192,
    'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe',
    'clarity': {'image_threshold': None, 'video_threshold': None, 'action': 'review'},
    'text': {'ngram': 4, 'min_tokens': 32, 'threshold': .6, 'action': 'review'},
    'exact_duplicate': True,
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def encoded(obj):
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)+'\n').encode()


def save(path, obj):
    with Path(path).open('xb') as f:
        f.write(encoded(obj)); f.flush(); os.fsync(f.fileno())


def digest_file(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(4*1024*1024), b''):
            h.update(block)
    return h.hexdigest()


def stamp(path):
    s = Path(path).stat()
    return {'bytes': s.st_size, 'mtime_ns': s.st_mtime_ns, 'inode': s.st_ino, 'device': s.st_dev}


def emit(event, obj):
    print('[mechanical-clean] '+event+' '+json.dumps(obj, ensure_ascii=False, allow_nan=False), flush=True)


def outside_repo(path):
    p = Path(path).resolve()
    return p != REPO and REPO not in p.parents


def read_config(path):
    require(Path(path).stat().st_size <= 128*1024, 'config too large')
    c = json.loads(Path(path).read_text(encoding='utf-8'))
    require(isinstance(c, dict) and set(c) == set(DEFAULT), 'config keys differ from template')
    for k in ('clarity', 'text'):
        require(isinstance(c[k], dict) and set(c[k]) == set(DEFAULT[k]), 'invalid '+k+' keys')
    for k in ('source', 'media_root', 'output'):
        require(isinstance(c[k], str) and Path(c[k]).is_absolute(), k+' must be explicit absolute path')
        c[k] = str(Path(c[k]).resolve())
    require(Path(c['source']).is_file(), 'source JSONL missing')
    require(outside_repo(c['source']) and outside_repo(c['output']), 'annotations/output must be outside code repo')
    require(Path(c['media_root']).is_dir(), 'media_root must be an existing directory')
    roots = c['allowed_media_roots']
    require(isinstance(roots, list) and roots and all(isinstance(x, str) and Path(x).is_absolute() for x in roots),
            'explicit allowed_media_roots required')
    c['allowed_media_roots'] = [str(Path(x).resolve()) for x in roots]
    require(all(Path(x).is_dir() and Path(x).parent != Path(x) for x in roots), 'unsafe/missing media root')
    output = Path(c['output'])
    require(not any(output == Path(x) or Path(x) in output.parents for x in c['allowed_media_roots']),
            'output must be outside immutable media roots')
    require(type(c['workers']) is int and 1 <= c['workers'] <= 32, 'workers must be1..32')
    require(type(c['part_rows']) is int and 1 <= c['part_rows'] <= 4096, 'part_rows must be1..4096')
    require(type(c['timeout_seconds']) in (int, float) and math.isfinite(c['timeout_seconds'])
            and 1 <= c['timeout_seconds'] <= 300, 'invalid timeout')
    require(type(c['exact_duplicate']) is bool, 'exact_duplicate must be boolean')
    require(type(c['scratch_limit_mb']) is int and 16 <= c['scratch_limit_mb'] <= 131072,
            'scratch_limit_mb must be16..131072')
    if c['scratch_root'] is not None:
        require(isinstance(c['scratch_root'], str) and Path(c['scratch_root']).is_absolute()
                and Path(c['scratch_root']).is_dir() and outside_repo(c['scratch_root']),
                'scratch_root must be existing absolute directory outside code')
        c['scratch_root'] = str(Path(c['scratch_root']).resolve())
        require(not any(Path(c['scratch_root']) == Path(x) or Path(x) in Path(c['scratch_root']).parents
                        for x in c['allowed_media_roots']), 'scratch must be outside immutable media roots')
    require(c['text']['action'] in ('review', 'reject') and c['clarity']['action'] in ('review', 'reject'),
            'actions must be review/reject')
    for k in ('image_threshold', 'video_threshold'):
        v = c['clarity'][k]
        require(v is None or (type(v) in (int, float) and math.isfinite(v) and v >= 0), 'invalid '+k)
    t = c['text']
    require(type(t['ngram']) is int and 2 <= t['ngram'] <= 16, 'ngram must be2..16')
    require(type(t['min_tokens']) is int and t['ngram'] <= t['min_tokens'] <= 10000, 'invalid min_tokens')
    require(type(t['threshold']) in (int, float) and math.isfinite(t['threshold'])
            and 0 < t['threshold'] <= 1, 'invalid repetition threshold')
    require(all(isinstance(c[k], str) and c[k] for k in ('ffmpeg', 'ffprobe')), 'tool paths required')
    return c


@contextmanager
def writer_lock(root):
    root.mkdir(parents=True, exist_ok=True)
    with (root/'writer.lock').open('a+b') as f:
        if os.name == 'nt':
            import msvcrt
            f.seek(0); f.write(b'0'); f.flush(); f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            if os.name == 'nt':
                f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(f, fcntl.LOCK_UN)


def decision(issues):
    if any(x['action'] == 'reject' for x in issues):
        return 'rejected'
    return 'review' if issues else 'accepted'


def analyse(item, config, inspector):
    index, raw = item
    audit = {'source_row': index, 'raw_sha256': hashlib.sha256(raw).hexdigest(),
             'issues': [], 'qa_pairs': 0, 'text_metrics': [], 'media': [], 'duplicate_key': None}
    try:
        row = json.loads(raw.decode('utf-8'), object_pairs_hook=unique_keys,
                         parse_constant=lambda x: invalid_number(x), parse_float=safe_float)
        require(isinstance(row, dict), 'JSON row must be object')
    except (ValueError, UnicodeError):
        audit['issues'].append({'reason': 'invalid_json_row', 'action': 'reject'})
        return raw, audit
    try:
        pairs = extract_pairs(row)
        audit['qa_pairs'] = len(pairs)
        issues, metrics = check_pairs(pairs, config['text'])
        audit['issues'].extend(issues); audit['text_metrics'] = metrics
    except ValueError as exc:
        audit['issues'].append({'reason': 'unsupported_qa_structure', 'action': 'review', 'error': str(exc)})
        pairs = None
    try:
        refs = references(row)
    except ValueError as exc:
        audit['issues'].append({'reason': 'unsupported_media_structure', 'action': 'review', 'error': str(exc)})
        refs = None
    # Do not decode media for rows already certainly rejected by empty/invalid QA.
    if refs is not None and decision(audit['issues']) != 'rejected':
        measures = inspector.inspect(refs)
        audit['media'] = measures
        audit['issues'].extend({'reason': m['reason'], 'action': m['status'], 'media_index': i}
                               for i, m in enumerate(measures) if m['status'] in ('review', 'reject'))
    if pairs is not None and refs is not None and decision(audit['issues']) == 'accepted':
        try:
            # Preserve media order and clip bounds. Do not collapse different videos,
            # clips, answers, prior conversation, options or original extra fields.
            identity_row = dict(row)
            identity_row.pop('id', None)
            for key in ('images', 'videos', 'image_path', 'video_path'):
                identity_row.pop(key, None)
            identity_row.pop('conversations', None)
            identity_row.pop('question', None); identity_row.pop('answer', None)
            audit['duplicate_key'] = fingerprint([
                [{**r, 'path': str(resolve(r['path'], config))} for r in refs],
                [[normalize(q), normalize(a)] for q, a in pairs], identity_row])
        except ValueError:
            pass  # unsafe paths already receive a review reason from media inspection
    return raw, audit


def check_part(path):
    receipt = json.loads((path/'receipt.json').read_bytes())
    for name, info in receipt['files'].items():
        p = path/name
        require(p.is_file() and p.stat().st_size == info['bytes'] and digest_file(p) == info['sha256'],
                'checkpoint changed: '+str(p))
    return receipt


def unique_keys(items):
    result = {}
    for key, value in items:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def invalid_number(value):
    raise ValueError('nonfinite JSON number: '+value)


def safe_float(value):
    result = float(value)
    require(math.isfinite(result), 'nonfinite JSON float')
    return result


def check_scratch(path, limit_mb):
    used = sum(p.stat().st_size for p in Path(path).iterdir() if p.is_file())
    require(used <= limit_mb*1024*1024, 'scratch budget exhausted; completed parts retained')
    require(shutil.disk_usage(path).free > 256*1024*1024, 'scratch free space below256MiB')


def run(config, root, runtime, limit=None, inspector_class=Inspector, code_identity=None,
        analyse_fn=analyse, summary_builder=None, event=None, artifact_writer=None, allow_empty=False):
    emit_event = event or emit
    source = Path(config['source'])
    initial = stamp(source)
    identity = {'config': config, 'source_stat': initial, 'runtime': runtime, 'limit': limit,
                'code': code_identity or {p.name: digest_file(p) for p in
                    (Path(__file__), Path(__file__).with_name('text_checks.py'),
                     Path(__file__).with_name('media_checks.py'), Path(blur.__file__))}}
    with writer_lock(root):
        identity_path = root/'identity.json'
        if identity_path.exists():
            require(json.loads(identity_path.read_bytes()) == identity, 'run binding changed; use new output root')
        else:
            require(not any(root.iterdir()) or {p.name for p in root.iterdir()} == {'writer.lock'},
                    'unknown existing output root')
            save(identity_path, identity)
        if (root/'delivery').exists():
            check_part(root/'delivery')
            summary = json.loads((root/'delivery/summary.json').read_bytes())
            emit_event('REUSED', summary)
            return summary
        parts = root/'parts'; parts.mkdir(exist_ok=True)
        # SQLite only on disposable scratch (prefer local SSD), never in the SFS
        # output directory. Durable continuation comes from part receipts/audits.
        scratch = tempfile.TemporaryDirectory(prefix='mechanical-state-', dir=config['scratch_root'])
        try:
            inspector = inspector_class(config, runtime, Path(scratch.name)/'media_cache.sqlite3')
        except BaseException:
            scratch.cleanup()
            raise
        try:
            dedup = sqlite3.connect(str(Path(scratch.name)/'dedup.sqlite3'))
        except BaseException:
            inspector.close(); scratch.cleanup()
            raise
        emit_event('SCRATCH', {'path': scratch.name, 'limit_mb': config['scratch_limit_mb'],
                         'contents': 'derived cache/index only; no copied annotations/media'})
        dedup.execute('CREATE TABLE IF NOT EXISTS seen(key TEXT PRIMARY KEY, source_row INTEGER NOT NULL)')
        # Reconstruct from immutable completed parts: interrupted-part transactions
        # must never suppress their own rows after a restart.
        dedup.execute('DELETE FROM seen'); dedup.commit()
        counts, reasons, qa_counts = Counter(), Counter(), Counter()
        source_digest = hashlib.sha256(); row_count = offset = part_number = 0
        started = last = time.monotonic(); fresh_rows = 0
        try:
            with source.open('rb', buffering=4*1024*1024) as src:
                for part in sorted(parts.glob('part-*')):
                    require(part.name == 'part-%06d' % part_number and part.is_dir(), 'noncontiguous checkpoints')
                    receipt = check_part(part)
                    require(receipt['start_offset'] == offset and receipt['start_row'] == row_count,
                            'checkpoint source positions changed')
                    size = receipt['end_offset']-offset
                    h = hashlib.sha256()
                    while size:
                        data = src.read(min(size, 4*1024*1024))
                        require(bool(data), 'source prefix truncated')
                        source_digest.update(data); h.update(data); size -= len(data)
                    require(h.hexdigest() == receipt['input_sha256'], 'checkpoint source bytes changed')
                    with (part/'audit.jsonl').open('rb') as audits:
                        for line in audits:
                            a = json.loads(line); add_counts(a, counts, reasons, qa_counts)
                            if hasattr(inspector, 'restore'):
                                inspector.restore(a['media'])
                            if a['status'] == 'accepted' and a['duplicate_key']:
                                dedup.execute('INSERT OR IGNORE INTO seen VALUES (?,?)', (a['duplicate_key'], a['source_row']))
                    row_count, offset = receipt['end_row'], receipt['end_offset']
                    part_number += 1
                    check_scratch(scratch.name, config['scratch_limit_mb'])
                dedup.commit()
                if hasattr(inspector, 'flush'):
                    inspector.flush()
                with ThreadPoolExecutor(max_workers=config['workers']) as pool:
                    while limit is None or row_count < limit:
                        batch, begin, block_digest, batch_bytes = [], src.tell(), hashlib.sha256(), 0
                        while len(batch) < config['part_rows'] and (limit is None or row_count+len(batch) < limit):
                            raw = src.readline(MAX_LINE+1)
                            if not raw:
                                break
                            require(len(raw) <= MAX_LINE, 'source line exceeds16MiB; no truncation')
                            block_digest.update(raw); source_digest.update(raw)
                            batch.append((row_count+len(batch), raw))
                            batch_bytes += len(raw)
                            if batch_bytes >= MAX_BATCH_BYTES:
                                break
                        if not batch:
                            break
                        stage = Path(tempfile.mkdtemp(prefix='.part-%06d-' % part_number, dir=parts))
                        with ExitStack() as stack:
                            outputs = {k: stack.enter_context((stage/(k+'.jsonl')).open('xb'))
                                       for k in CATEGORIES+('audit',)}
                            # map is bounded by the configured annotation chunk and preserves input order.
                            for raw, a in pool.map(lambda item: analyse_fn(item, config, inspector), batch):
                                status = decision(a['issues'])
                                key = a['duplicate_key']
                                if status == 'accepted' and key and config['exact_duplicate']:
                                    first = dedup.execute('SELECT source_row FROM seen WHERE key=?', (key,)).fetchone()
                                    if first:
                                        a['issues'].append({'reason': 'exact_duplicate_same_context',
                                                            'action': 'reject', 'first_source_row': first[0]})
                                        status = 'rejected'
                                if status == 'accepted' and key:
                                    dedup.execute('INSERT OR IGNORE INTO seen VALUES (?,?)', (key, a['source_row']))
                                a['status'] = status
                                outputs[status].write(raw); outputs['audit'].write(encoded(a))
                                add_counts(a, counts, reasons, qa_counts)
                            for f in outputs.values():
                                f.flush(); os.fsync(f.fileno())
                        files = {p.name: {'bytes': p.stat().st_size, 'sha256': digest_file(p)}
                                 for p in stage.iterdir()}
                        save(stage/'receipt.json', {'start_row': row_count, 'end_row': row_count+len(batch),
                             'start_offset': begin, 'end_offset': src.tell(),
                             'input_sha256': block_digest.hexdigest(), 'files': files})
                        # Publish first, then commit dedup. Restart always rebuilds dedup from published audit.
                        os.rename(stage, parts/('part-%06d' % part_number))
                        dedup.commit(); part_number += 1; row_count += len(batch)
                        fresh_rows += len(batch)
                        check_scratch(scratch.name, config['scratch_limit_mb'])
                        if time.monotonic()-last >= 10:
                            emit_event('PROGRESS', {'rows': row_count, 'counts': dict(counts),
                                 'fresh_rows': fresh_rows,
                                 'fresh_rows_per_second': round(fresh_rows/max(time.monotonic()-started,.001), 2)})
                            last = time.monotonic()
            require(stamp(source) == initial, 'source changed during run')
            require(row_count > 0 or allow_empty, 'source has no rows')
            require(sum(counts.values()) == row_count, 'row conservation failed')
            result = {'status': 'MECHANICAL_STAGE_COMPLETE', 'scope': 'pilot' if limit else 'full',
                      'stage_id': 'stage01_mechanical',
                      'rows': row_count, 'counts': dict(counts), 'qa_counts': dict(qa_counts),
                      'reason_counts': dict(reasons), 'source_sha256_scanned': source_digest.hexdigest(),
                      'source_hash_scope': 'prefix' if limit else 'entire_file',
                      'seconds_this_invocation': round(time.monotonic()-started, 3),
                      'full_media_decode_verified': False, 'strict_data_format_verified': False,
                      'semantic_quality_verified': False, 'source_writes': 0, 'model_calls': 0}
            if summary_builder is not None:
                result = summary_builder(result, parts)
            publish(root, parts, result, artifact_writer)
            emit_event('COMPLETE', result)
            return result
        finally:
            dedup.close(); inspector.close(); scratch.cleanup()


def add_counts(audit, counts, reasons, qa_counts):
    counts[audit['status']] += 1
    qa_counts[audit['status']] += audit['qa_pairs']
    # Count distinct reasons once per row, not per repeated frame/QA.
    reasons.update(set(x['reason'] for x in audit['issues']))


def publish(root, parts, summary, artifact_writer=None):
    stage = Path(tempfile.mkdtemp(prefix='.delivery-', dir=root))
    hashes = {}
    for name in CATEGORIES+('audit',):
        h = hashlib.sha256()
        with (stage/(name+'.jsonl')).open('xb') as out:
            for part in sorted(parts.glob('part-*')):
                with (part/(name+'.jsonl')).open('rb') as inp:
                    for block in iter(lambda: inp.read(4*1024*1024), b''):
                        out.write(block); h.update(block)
            out.flush(); os.fsync(out.fileno())
        hashes[name+'.jsonl'] = {'sha256': h.hexdigest(), 'bytes': (stage/(name+'.jsonl')).stat().st_size}
    if artifact_writer is not None:
        hashes.update(artifact_writer(stage, parts, summary))
    save(stage/'summary.json', summary)
    stage_name = summary.get('stage_name', '图片/视频机械清洗')
    lines = ['# '+stage_name+'统计', '',
             '本文件是阶段报告，不是正式训练数据README或全媒体/语义验收证明。', '',
             '| 分组 | 样本行 | 可解析QA |', '|---|---:|---:|']
    for k in CATEGORIES:
        lines.append('| %s | %s | %s |' % (k, summary['counts'].get(k,0), summary['qa_counts'].get(k,0)))
    lines += ['', '| 原因（每行同原因只计一次，可重叠） | 行数 |', '|---|---:|']
    lines += ['| %s | %s |' % p for p in sorted(summary['reason_counts'].items())]
    lines += ['', 'accepted仅表示本阶段配置下通过；review不自动进入训练。',
              summary.get('stage_limitations', '仅抽样视频帧；不证明整段可解码。图片/视频blur阈值和n-gram阈值仍需真实样本校准。'),
              '源行按原始字节保留，不移除源token字段、不补媒体时长、不改写/裁剪媒体。']
    for group, title in (('qa_distributions', '全部受审QA'), ('accepted_qa_distributions', '通过集QA')):
        for field, values in summary.get(group, {}).items():
            display = summary.get('qa_display_names', {}).get(field, field)
            names = summary.get('qa_value_names', {}).get(field, {})
            lines += ['', '## '+title+'：'+display, '', '| 标签 | QA数 |', '|---|---:|']
            lines += ['| %s %s | %s |' % (key, names.get(key,''), count) for key,count in values.items()]
    (stage/'report.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
    for name in ('summary.json', 'report.md'):
        hashes[name] = {'sha256': digest_file(stage/name), 'bytes': (stage/name).stat().st_size}
    save(stage/'receipt.json', {'files': hashes, 'identity_sha256': digest_file(root/'identity.json'),
                               'published_utc': datetime.now(timezone.utc).isoformat()})
    require(not (root/'delivery').exists(), 'delivery already exists')
    os.rename(stage, root/'delivery')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    init = sub.add_parser('init-config'); init.add_argument('path', type=Path)
    for name in ('pilot', 'run'):
        p = sub.add_parser(name); p.add_argument('--config', type=Path, required=True)
        if name == 'pilot':
            p.add_argument('--rows', type=int, default=100)
    status = sub.add_parser('status'); status.add_argument('--output', type=Path, required=True)
    a = parser.parse_args()
    if a.command == 'init-config':
        require(outside_repo(a.path), 'write private runtime config outside code checkout')
        save(a.path, DEFAULT); emit('CONFIG_CREATED', {'path': str(a.path.resolve())}); return
    if a.command == 'status':
        p = a.output/'delivery/summary.json'
        emit('STATUS', json.loads(p.read_bytes()) if p.is_file() else {'status': 'NOT_PUBLISHED'})
        return
    c = read_config(a.config)
    limit = a.rows if a.command == 'pilot' else None
    require(limit is None or 1 <= limit <= 10000, 'pilot rows must be1..10000')
    runtime = blur.tools(c['ffmpeg'], c['ffprobe'])
    runtime['binary_sha256'] = {k: digest_file(runtime[k]) for k in ('ffmpeg', 'ffprobe')}
    commit = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=REPO, timeout=10, text=True).strip()
    dirty = subprocess.check_output(['git', 'status', '--porcelain'], cwd=REPO, timeout=10, text=True).strip()
    require(not dirty, 'formal pilot/run requires clean versioned checkout; preserve unrelated changes')
    runtime['producer_commit'] = commit
    emit('SCOPE', {'source': c['source'], 'media_roots': c['allowed_media_roots'],
                   'model_calls': 0, 'media_writes': 0, 'workers': c['workers'],
                   'video_frames_per_clip': 8, 'producer_commit': commit})
    root = Path(c['output'])/('pilot-%d' % limit if limit else 'full')
    run(c, root, runtime, limit)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        emit('FAIL', {'error': type(exc).__name__+': '+str(exc),
                      'original_writes': 0, 'completed_parts_retained': True})
        sys.exit(1)

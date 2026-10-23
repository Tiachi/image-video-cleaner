"""Stage03 sampled-visual QA quality, chained to Stage02 accepted bytes."""
import argparse
import base64
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import text_qa_clean as text_stage
from text_checks import extract_pairs
from media_checks import references, blur
from frame_cache import FrameCache, CacheCapacityError, CacheIntegrityError, CacheMissError
from media_contract import parse, issues_for
core = text_stage.core
PROJECT, REPO = text_stage.PROJECT, text_stage.REPO
PROMPT = PROJECT/'prompts/media_qa_quality_v1.txt'
DEFAULT = {'stage2_delivery': None, 'output': None, 'frame_cache_root': None,
           'cache_namespace':'image-video', 'frame_cache_mode':'read_write',
           'vision_processor_kwargs':{'max_pixels':262144},
           'frame_cache_limit_mb': 65536, 'frames_per_video': 8, 'max_images_per_request': 32,
           'max_frame_pixels': 8388608, 'max_frame_bytes': 83886080, 'max_request_image_bytes': 33554432,
           'api_base': 'http://127.0.0.1:8018/v1', 'model': 'Qwen3.8-27B', 'model_revision': None,
           'workers': 8, 'part_rows': 32, 'timeout_seconds': 120, 'max_input_chars': 24000,
           'scratch_root': None, 'scratch_limit_mb': 8192, 'minimum_score': None,
           'ffmpeg': 'ffmpeg', 'ffprobe': 'ffprobe'}


def emit(event, value):
    print('[media-qa-clean] '+event+' '+json.dumps(value, ensure_ascii=False, allow_nan=False), flush=True)


def parent_info(delivery, expected_stage):
    r = json.loads((delivery/'receipt.json').read_bytes())
    identity_file = delivery.parent/'identity.json'
    core.require(core.digest_file(identity_file) == r['identity_sha256'], 'upstream identity mismatch')
    identity = json.loads(identity_file.read_bytes())
    core.require(identity['limit'] is None, 'upstream pilot is not a formal input')
    core.require(core.digest_file(delivery/'summary.json') == r['files']['summary.json']['sha256'],
                 'upstream summary mismatch')
    summary = json.loads((delivery/'summary.json').read_bytes())
    core.require(summary['stage_id'] == expected_stage, 'unexpected upstream stage')
    source = delivery/'accepted.jsonl'
    core.require(source.stat().st_size == r['files']['accepted.jsonl']['bytes'], 'upstream accepted size mismatch')
    return r, identity, summary


def config(path, require_model=True):
    core.require(path.stat().st_size <= 128*1024, 'config too large')
    c = json.loads(path.read_bytes(), object_pairs_hook=core.unique_keys,
                   parse_constant=core.invalid_number, parse_float=core.safe_float)
    core.require(isinstance(c, dict) and set(c) == set(DEFAULT), 'config keys differ from template')
    for name in ('stage2_delivery', 'output', 'frame_cache_root'):
        core.require(isinstance(c[name], str) and Path(c[name]).is_absolute() and core.outside_repo(c[name]),
                     name+' must be absolute and outside code')
        c[name] = str(Path(c[name]).resolve())
    for name in (('model', 'model_revision') if require_model else ('model',)):
        core.require(isinstance(c[name], str) and c[name].strip(), 'declare '+name)
    core.require(isinstance(c['cache_namespace'],str) and re.fullmatch(r'[A-Za-z0-9_-]{1,64}',c['cache_namespace']),
                 'invalid dataset cache namespace')
    core.require(c['frame_cache_mode'] in ('read_write','require_hit'), 'frame_cache_mode must be read_write/require_hit')
    processor = c['vision_processor_kwargs']
    core.require(isinstance(processor,dict) and set(processor)=={'max_pixels'}
                 and type(processor['max_pixels']) is int and 4096<=processor['max_pixels']<=16777216,
                 'declare bounded vision_processor_kwargs.max_pixels')
    text_stage.api_location(c['api_base'])
    for name, low, high in (('workers',1,32), ('part_rows',1,4096), ('frames_per_video',1,16),
            ('max_images_per_request',1,32), ('frame_cache_limit_mb',1024,1048576),
            ('max_frame_pixels',1024,16777216), ('max_frame_bytes',1048576,268435456),
            ('max_request_image_bytes',1048576,134217728), ('timeout_seconds',1,600),
            ('max_input_chars',100,262144), ('scratch_limit_mb',16,131072)):
        core.require(type(c[name]) is int and low <= c[name] <= high, 'invalid '+name)
    core.require(c['part_rows'] >= c['workers'], 'part_rows must be >=workers')
    core.require(c['max_frame_bytes'] >= 9*c['max_frame_pixels']+1048576,
                 'frame byte reservation must cover native16-bitRGBA PNG worst-case pixels+overhead')
    core.require(c['frames_per_video'] <= c['max_images_per_request'], 'video sampling exceeds request image cap')
    core.require(c['minimum_score'] is None or type(c['minimum_score']) is int and 1 <= c['minimum_score'] <= 10,
                 'minimum_score must be null or1..10')
    parent = Path(c['stage2_delivery'])
    r2, i2, _ = parent_info(parent, 'stage02_text_qa')
    stage1 = Path(i2['config']['stage1_delivery'])
    core.require(core.digest_file(stage1/'receipt.json') == i2['config']['parent_receipt_sha256'],
                 'stage1 ancestor receipt changed')
    _, i1, _ = parent_info(stage1, 'stage01_mechanical')
    c.update(media_root=i1['config']['media_root'], allowed_media_roots=i1['config']['allowed_media_roots'])
    protected = [parent.parent, stage1.parent, Path(i1['config']['source'])]
    protected += [Path(x).resolve() for x in c['allowed_media_roots']]
    paths = [Path(c['output']), Path(c['frame_cache_root'])]
    if c['scratch_root'] is not None:
        core.require(isinstance(c['scratch_root'], str) and Path(c['scratch_root']).is_absolute()
                     and Path(c['scratch_root']).is_dir() and core.outside_repo(c['scratch_root']),
                     'scratch must exist outside code')
        c['scratch_root'] = str(Path(c['scratch_root']).resolve()); paths.append(Path(c['scratch_root']))
    for path_value in paths:
        core.require(not any(path_value == p or p in path_value.parents or path_value in p.parents for p in protected),
                     'output/cache/scratch overlaps immutable upstream/media')
    out, cache = paths[:2]
    core.require(out != cache and out not in cache.parents and cache not in out.parents,
                 'output and frame cache must be separate roots')
    c.update(source=str(parent/'accepted.jsonl'), exact_duplicate=False,
             parent_receipt_sha256=core.digest_file(parent/'receipt.json'),
             expected_source_sha256=r2['files']['accepted.jsonl']['sha256'])
    return c


class Judge(text_stage.Judge):
    def __init__(self, c, runtime, cache_path):
        super().__init__(c, runtime, cache_path)
        try:
            self.prompt = PROMPT.read_text(encoding='utf-8')
            self.frames = FrameCache(c, runtime)
        except BaseException:
            super().close(); raise

    def _build_lineage(self, cache_path):
        self.db.execute('CREATE TABLE lineage(idx INTEGER PRIMARY KEY, original_row INTEGER, sha TEXT, previous_labels TEXT)')
        c = self.config; parent = Path(c['stage2_delivery'])
        r, _, summary = parent_info(parent, 'stage02_text_qa')
        core.require(core.digest_file(parent/'receipt.json') == c['parent_receipt_sha256'], 'stage2 receipt changed')
        h = hashlib.sha256(); n = 0
        with (parent/'audit.jsonl').open('rb') as stream:
            for raw in stream:
                core.require(len(raw) <= core.MAX_LINE, 'stage2 audit line too large')
                h.update(raw); a = json.loads(raw)
                if a['status'] == 'accepted':
                    self.db.execute('INSERT INTO lineage VALUES (?,?,?,?)',
                        (n, a['original_source_row'], a['raw_sha256'], json.dumps(a['qa_labels'])))
                    n += 1
                    if n % 4096 == 0:
                        self.db.commit(); core.check_scratch(Path(cache_path).parent, c['scratch_limit_mb'])
        core.require(h.hexdigest() == r['files']['audit.jsonl']['sha256'], 'stage2 audit SHA mismatch')
        core.require(n == summary['counts'].get('accepted', 0), 'stage2 lineage count mismatch')
        self.db.commit()

    def previous_labels(self, index):
        with self.lock:
            value = self.db.execute('SELECT previous_labels FROM lineage WHERE idx=?', (index,)).fetchone()
        return json.loads(value[0])

    def content(self, frames):
        result = []
        for frame in frames:
            path = Path(frame['path'])
            data = path.read_bytes()
            core.require(len(data) == frame['bytes'] and hashlib.sha256(data).hexdigest() == frame['sha256'],
                         'cached PNG changed before request')
            descriptor = {k: frame[k] for k in ('frame_id', 'media_index', 'relative_pts_sec', 'clip_start', 'clip_end')}
            result += [{'type': 'text', 'text': json.dumps(descriptor)},
                       {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,'+base64.b64encode(data).decode('ascii')}}]
        return result

    def score_visual(self, payload_text, images, frames):
        if len(payload_text) > self.config['max_input_chars']:
            return {'status': 'input_too_long', 'model_calls': 0}
        try:
            response, usage = self.request(images+[{'type': 'text', 'text': payload_text}])
            labels = parse(response, len(frames))
            safe_usage = {k:v for k,v in usage.items() if k in ('prompt_tokens','completion_tokens','total_tokens')
                          and type(v) is int and v >= 0}
            evidence = [{k: frames[index-1][k] for k in ('frame_id','cache_key','file','relative_pts_sec')}
                        for index in labels['evidence_frame_ids']]
            return {'status': 'ok', 'model_calls': 1, 'labels': labels, 'evidence': evidence, 'usage': safe_usage}
        except Exception as exc:
            return {'status': 'model_error', 'model_calls': 1,
                    'error_type': type(exc).__name__, 'error': str(exc)[:200]}

    def close(self):
        try:
            self.frames.close()
        finally:
            super().close()


def analyse(item, c, judge):
    index, raw = item
    a = {'source_row': index, 'raw_sha256': hashlib.sha256(raw).hexdigest(), 'issues': [],
         'qa_pairs': 0, 'text_metrics': [], 'media': [], 'duplicate_key': None, 'qa_labels': []}
    a['original_source_row'] = judge.lineage(index, a['raw_sha256'])
    a['text_qa_labels'] = judge.previous_labels(index)
    row = json.loads(raw.decode('utf-8'))
    pairs = extract_pairs(row); a['qa_pairs'] = len(pairs)
    core.require(len(pairs) == len(a['text_qa_labels']), 'stage2 QA count mismatch')
    core.require(all(x['qa_index'] == j and x['status'] == 'ok' for j,x in enumerate(a['text_qa_labels'])),
                 'invalid accepted Stage02 QA lineage')
    try:
        frames = judge.frames.frames(references(row))
        a['media'] = [{k:v for k,v in frame.items() if k != 'path'} for frame in frames]
        images = judge.content(frames)
    except (CacheCapacityError, CacheIntegrityError, CacheMissError):
        raise
    except Exception as exc:
        a['issues'].append({'reason': 'frame_evidence_unavailable', 'action': 'review',
                            'error_type': type(exc).__name__, 'error': str(exc)[:300]})
        a['qa_labels'] = [{'qa_index': i, 'status': 'frame_evidence_unavailable', 'model_calls': 0} for i in range(len(pairs))]
        return raw, a
    previous = []
    for j, (question, answer) in enumerate(pairs):
        attached, attachment_issues = text_stage.cot_fields(row, j, len(pairs))
        a['issues'].extend(dict(issue, qa_index=j) for issue in attachment_issues)
        payload = json.dumps({'previous_qa': previous, 'question': question, 'answer': answer,
                              'existing_cot_fields': attached, 'number_of_images': len(frames)}, ensure_ascii=False)
        result = judge.score_visual(payload, images, frames); result['qa_index'] = j
        a['qa_labels'].append(result)
        if result['status'] == 'ok':
            a['issues'].extend(dict(issue, qa_index=j) for issue in issues_for(result['labels'], c['minimum_score']))
        else:
            a['issues'].append({'reason': result['status'], 'action': 'review', 'qa_index': j})
        previous.append({'question': question, 'answer': answer})
    return raw, a


def summary_builder(c):
    def build(summary, parts):
        core.require(summary['source_hash_scope'] != 'entire_file'
                     or summary['source_sha256_scanned'] == c['expected_source_sha256'], 'stage2 accepted SHA changed')
        parent = Path(c['stage2_delivery'])
        core.require(core.digest_file(parent/'receipt.json') == c['parent_receipt_sha256'], 'stage2 receipt changed')
        totals = {'media_quality_score': Counter({str(k):0 for k in range(1,11)}),
                  'media_alignment': Counter({str(k):0 for k in range(3)})}
        passed = {k:Counter(v) for k,v in totals.items()}
        states = Counter(); usage = Counter(); cache = Counter(); calls = 0
        for part in sorted(parts.glob('part-*')):
            with (part/'audit.jsonl').open('rb') as stream:
                for raw in stream:
                    a = json.loads(raw)
                    for frame in a['media']:
                        cache['frame_references'] += 1
                        cache['cached_frame_references' if frame['cache_hit'] else 'new_frame_references'] += 1
                    for qa in a['qa_labels']:
                        states[qa['status']] += 1; calls += qa['model_calls']; usage.update(qa.get('usage', {}))
                        if qa['status'] == 'ok':
                            for field in totals:
                                value = str(qa['labels'][field]); totals[field][value] += 1
                                if a['status'] == 'accepted': passed[field][value] += 1
        upstream = json.loads((parent/'summary.json').read_bytes())
        root_rows = upstream['upstream_counts']['stage1_input_rows']
        summary.update(stage_id='stage03_media_qa', stage_name='第三步：带媒体QA质量清洗',
            status='MEDIA_QA_STAGE_COMPLETE' if summary['rows'] else 'EMPTY_UPSTREAM',
            stage_limitations='仅图片/实际切片稀疏帧；不验证未采到的事件与完整时序；分数不是人工准确率。',
            qa_distributions={k:dict(v) for k,v in totals.items()},
            accepted_qa_distributions={k:dict(v) for k,v in passed.items()},
            qa_display_names={'media_quality_score':'带媒体质量分','media_alignment':'媒体匹配'},
            qa_value_names={'media_alignment':{'0':'明确视觉矛盾','1':'采样证据支持','2':'采样证据不足'}},
            model_calls=calls, model_status_counts=dict(states), token_usage=dict(usage),
            frame_cache_observations=dict(cache), frame_cache_root=c['frame_cache_root'],
            cache_observation_scope='completed audits, including reused parts; not fresh invocation throughput',
            parent_stage='stage02_text_qa', parent_receipt_sha256=c['parent_receipt_sha256'],
            minimum_score=c['minimum_score'], original_media_writes=0, cot_generated=0,
            full_media_decode_verified=False, semantic_quality_verified=False,
            upstream_counts=dict(upstream['upstream_counts'], stage2_input_rows=upstream['rows'],
                                 stage2_accepted_rows=upstream['counts'].get('accepted',0)),
            cumulative_retained_fraction_rows=(summary['counts'].get('accepted',0)/root_rows
                                              if summary['scope']=='full' and root_rows else None))
        return summary
    return build


def write_attributes(stage, parts, summary):
    h = hashlib.sha256(); size = 0; name = 'attributes.jsonl'
    with (stage/name).open('xb') as out:
        for part in sorted(parts.glob('part-*')):
            with (part/'audit.jsonl').open('rb') as stream:
                for raw in stream:
                    a = json.loads(raw)
                    for qa, previous in zip(a['qa_labels'], a['text_qa_labels']):
                        core.require(qa['qa_index'] == previous['qa_index'], 'attribute QA index mismatch')
                        data = core.encoded({'stage2_accepted_row':a['source_row'], 'original_source_row':a['original_source_row'],
                            'source_row_sha256':a['raw_sha256'], 'qa_index':qa['qa_index'], 'row_status':a['status'],
                            'label_status':qa['status'], 'media_labels':qa.get('labels'), 'evidence':qa.get('evidence',[]),
                            'text_labels':previous['labels'], 'cot_candidate':a['status']=='accepted' and previous['labels']['cot_class']==2})
                        out.write(data); h.update(data); size += len(data)
        out.flush(); text_stage.os.fsync(out.fileno())
    return {name:{'sha256':h.hexdigest(), 'bytes':size}}


def analyse_cache(item,c,judge):
    index,raw = item
    a = {'source_row':index, 'raw_sha256':hashlib.sha256(raw).hexdigest(), 'issues':[],
         'qa_pairs':0, 'text_metrics':[], 'media':[], 'duplicate_key':None, 'qa_labels':[]}
    a['original_source_row'] = judge.lineage(index,a['raw_sha256'])
    row = json.loads(raw); a['qa_pairs'] = len(extract_pairs(row))
    try:
        frames = judge.frames.frames(references(row))
        a['media'] = [{k:v for k,v in frame.items() if k != 'path'} for frame in frames]
    except (CacheCapacityError,CacheIntegrityError,CacheMissError):
        raise
    except Exception as exc:
        a['issues'].append({'reason':'frame_preparation_failed','action':'review',
                            'error_type':type(exc).__name__, 'error':str(exc)[:300]})
    return raw,a


def cache_summary(c):
    def build(summary,parts):
        result = summary_builder(c)(summary,parts)
        for field in ('qa_distributions','accepted_qa_distributions','qa_display_names','qa_value_names'):
            result.pop(field,None)
        result.update(stage_id='stage03_frame_cache',stage_name='第三步前置：纯CPU帧预缓存',
                      status='FRAME_CACHE_PREPARED' if not result['counts'].get('review',0) else 'PARTIAL_FRAME_CACHE',
                      stage_limitations='只准备帧并核验缓存；没有QA质量推理或质量认证。', model_calls=0)
        return result
    return build


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    init = commands.add_parser('init-config'); init.add_argument('path', type=Path)
    for name in ('pilot','run','cache'):
        cmd = commands.add_parser(name); cmd.add_argument('--config', type=Path, required=True)
        if name in ('pilot','cache'): cmd.add_argument('--rows',type=int,default=100 if name=='pilot' else None)
    args = parser.parse_args()
    if args.command == 'init-config':
        core.require(core.outside_repo(args.path), 'private config belongs outside code')
        core.save(args.path, DEFAULT); emit('CONFIG_CREATED', {'path':str(args.path.resolve())}); return
    c = config(args.config,require_model=args.command!='cache')
    limit = args.rows if args.command in ('pilot','cache') else None
    if args.command == 'cache': c['frame_cache_mode'] = 'read_write'
    core.require(limit is None or 1 <= limit <= 10000, 'pilot rows must be1..10000')
    core.require(not subprocess.check_output(['git','status','--porcelain'],cwd=REPO,timeout=10,text=True).strip(),
                 'run requires clean checkout; preserve unrelated changes')
    commit = subprocess.check_output(['git','rev-parse','HEAD'],cwd=REPO,timeout=10,text=True).strip()
    tools = {}
    for name in ('ffmpeg','ffprobe'):
        tools[name] = shutil.which(c[name]); core.require(tools[name], 'missing '+name+'; specify reviewed existing executable')
        tools[name+'_sha256'] = core.digest_file(Path(tools[name]))
        tools[name+'_version'] = blur.run_command([tools[name],'-version'],15).splitlines()[0]
    runtime = dict(tools, stage='media_qa_v1', producer_commit=commit, prompt_sha256=core.digest_file(PROMPT),
                   api_base=c['api_base'], model=c['model'], declared_model_revision=c['model_revision'],
                   model_weights_verified=False, declared_vision_processor_kwargs=c['vision_processor_kwargs'],
                   processor_profile_verified=False)
    files = [Path(__file__), Path(text_stage.__file__), Path(core.__file__), Path(blur.__file__)]
    files += [Path(__file__).with_name(n) for n in ('media_contract.py','frame_cache.py','text_contract.py')]
    files += [Path(core.__file__).with_name(n) for n in ('text_checks.py','media_checks.py')]
    identity = {str(p.relative_to(REPO)):core.digest_file(p) for p in files}
    root = Path(c['output'])/(('cache-%d'%limit if limit else 'cache-full') if args.command=='cache'
                              else ('pilot-%d'%limit if limit else 'full'))
    emit('SCOPE', {'upstream':c['stage2_delivery'], 'frame_cache':c['frame_cache_root'],
                   'frames_per_video':c['frames_per_video'], 'media_resize':False,
                   'workers':c['workers'], 'minimum_score':c['minimum_score']})
    if args.command == 'cache':
        core.run(c,root,runtime,limit,Judge,identity,analyse_cache,cache_summary(c),emit,allow_empty=True)
    else:
        core.run(c,root,runtime,limit,Judge,identity,analyse,summary_builder(c),emit,write_attributes,allow_empty=True)


if __name__ == '__main__':
    try: main()
    except Exception as exc:
        emit('FAIL', {'error':type(exc).__name__+': '+str(exc), 'original_writes':0,
                      'completed_parts_retained':True}); sys.exit(1)

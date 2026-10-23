"""Annotation-derived media IO only. Reuse the existing bounded FFmpeg engine."""
import json
import math
import os
from pathlib import Path
import sqlite3
import stat
import sys
import threading

REPO = Path(__file__).resolve().parents[1]
import ffmpeg_tools as blur
from text_checks import fingerprint


def finite(value):
    return type(value) in (float, int) and math.isfinite(value)


def references(row):
    refs = []
    # Canonical list fields plus explicit scalar aliases; conflicting aliases fail.
    for plural, alias, kind in (('images', 'image_path', 'image'),
                                ('videos', 'video_path', 'video')):
        values = row.get(plural)
        other = row.get(alias)
        if values is None:
            values = [other] if isinstance(other, str) and other else []
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise ValueError('invalid '+plural)
        if other and values != [other]:
            raise ValueError('conflicting '+plural+'/'+alias)
        if len(values) > 32:
            raise ValueError('at most32 references per media kind')
        for i, value in enumerate(values):
            start = end = None
            if kind == 'video':
                for key in ('video_start', 'video_end'):
                    bound = row.get(key)
                    if isinstance(bound, list):
                        if len(bound) != len(values):
                            raise ValueError('clip array length mismatch')
                        bound = bound[i]
                    elif len(values) > 1 and bound is not None:
                        raise ValueError('ambiguous scalar multi-video bounds')
                    if bound is not None and (not finite(bound) or bound < 0):
                        raise ValueError('invalid '+key)
                    if key == 'video_start':
                        start = bound
                    else:
                        end = bound
                if end is not None and end <= (start or 0):
                    raise ValueError('nonpositive clip')
            refs.append({'kind': kind, 'path': value, 'start': start, 'end': end})
    if not any(r['kind'] in ('image', 'video') for r in refs):
        raise ValueError('no explicit image/video reference')
    return refs


def resolve(value, config):
    if '\x00' in value or '://' in value or '\\' in value:
        raise ValueError('unsupported/unsafe media path')
    p = Path(value)
    if '..' in p.parts:
        raise ValueError('media path traversal')
    p = (p if p.is_absolute() else Path(config['media_root'])/p).resolve()
    if not any(p == Path(root).resolve() or Path(root).resolve() in p.parents
               for root in config['allowed_media_roots']):
        raise ValueError('media path outside allowlist')
    return p


def image_detect(path, runtime, timeout):
    command = [runtime['ffmpeg'], '-hide_banner', '-loglevel', 'error', '-nostdin',
               '-threads', '1', '-filter_threads', '1', '-protocol_whitelist', 'file',
               '-i', str(path), '-map', '0:v:0', '-an', '-sn', '-dn', '-frames:v', '1',
               '-vf', blur.FILTER+',metadata=mode=print:key=lavfi.blur:file=-', '-f', 'null', '-']
    # Decodes the still image at native resolution; animated formats use frame0 only.
    score, pts = blur.parse_frame(blur.run_command(command, timeout))
    return {'status': 'ok', 'mean_blur': score, 'valid_frames': 1,
            'frames': [{'blur': score, 'pts_sec': pts}], 'sample_scope': 'first_image_frame'}


class Inspector:
    def __init__(self, config, runtime, cache_path):
        self.config, self.runtime = config, runtime
        self.cache = sqlite3.connect(str(cache_path), check_same_thread=False)
        self.cache.execute('CREATE TABLE IF NOT EXISTS media(key TEXT PRIMARY KEY, result TEXT NOT NULL)')
        self.lock = threading.Lock()
        self.workers = config['workers']
        # Fixed-size striped locks avoid both duplicate concurrent probes and an
        # unbounded in-memory map of millions of media paths.
        self.key_locks = [threading.Lock() for _ in range(self.workers*8)]

    def close(self):
        self.cache.close()

    def restore(self, measures):
        """Rebuild disposable local cache from verified immutable part audits."""
        with self.lock:
            for result in measures:
                if result.get('cache_key') and result.get('measurement', {}).get('status') == 'ok':
                    self.cache.execute('INSERT OR IGNORE INTO media VALUES (?,?)',
                        (result['cache_key'], json.dumps(result['measurement'], allow_nan=False)))

    def flush(self):
        with self.lock:
            self.cache.commit()

    def one(self, ref):
        result = dict(ref)
        try:
            path = resolve(ref['path'], self.config)
        except ValueError as exc:
            return dict(result, status='review', reason='unsafe_media_reference', error=str(exc))
        result['resolved_path'] = str(path)
        try:
            before = path.stat()
        except FileNotFoundError:
            return dict(result, status='reject', reason='media_missing')
        except OSError as exc:
            return dict(result, status='review', reason='media_stat_error', error=type(exc).__name__)
        if not stat.S_ISREG(before.st_mode):
            return dict(result, status='review', reason='media_not_regular_file')
        if before.st_size == 0:
            return dict(result, status='reject', reason='media_zero_bytes')
        signature = {'bytes': before.st_size, 'mtime_ns': before.st_mtime_ns,
                     'inode': before.st_ino, 'device': before.st_dev}
        result['media_stat'] = signature
        key = fingerprint([str(path), signature, ref, self.runtime, blur.FILTER])
        result['cache_key'] = key
        with self.key_locks[int(key[:8], 16) % len(self.key_locks)]:
            with self.lock:
                cached = self.cache.execute('SELECT result FROM media WHERE key=?', (key,)).fetchone()
            if cached:
                measure = json.loads(cached[0]); result['cache_hit'] = True
            else:
                try:
                    if ref['kind'] == 'image':
                        measure = image_detect(path, self.runtime, self.config['timeout_seconds'])
                    else:
                        measure = blur.detect({'path': str(path), 'start': ref['start'], 'end': ref['end']},
                                              self.runtime, timeout=self.config['timeout_seconds'],
                                              resolver=lambda p: Path(p))
                except Exception as exc:
                    measure = {'status': 'error', 'error': type(exc).__name__+': '+str(exc)[:500]}
                after = path.stat()
                if signature != {'bytes': after.st_size, 'mtime_ns': after.st_mtime_ns,
                                 'inode': after.st_ino, 'device': after.st_dev}:
                    raise RuntimeError('media changed during inspection: '+str(path))
                # Do not permanently cache transient timeout/decoder failures.
                if measure.get('status') == 'ok':
                    with self.lock:
                        self.cache.execute('INSERT OR REPLACE INTO media VALUES (?,?)',
                                           (key, json.dumps(measure, allow_nan=False)))
                        self.cache.commit()
                result['cache_hit'] = False
        result['measurement'] = measure
        if measure.get('status') != 'ok':
            return dict(result, status='review', reason='clarity_probe_failed', clarity='unknown')
        threshold = self.config['clarity'][ref['kind']+'_threshold']
        if threshold is None:
            return dict(result, status='review', reason='clarity_threshold_uncalibrated', clarity='unknown')
        is_blurry = measure['mean_blur'] > threshold
        return dict(result, threshold=threshold, is_blurry=is_blurry,
                    status=self.config['clarity']['action'] if is_blurry else 'ok',
                    reason='blurry' if is_blurry else None,
                    clarity='blurry' if is_blurry else 'sampled_clear')

    def inspect(self, refs):
        # Deduplicate identical references inside a row before IO; preserve field order.
        unique = list({fingerprint(ref): ref for ref in refs}.values())
        # Row concurrency belongs to the coordinator: no nested worker explosion.
        return [self.one(ref) for ref in unique]

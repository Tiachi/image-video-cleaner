"""Bounded, immutable native PNG frame cache for annotation-referenced clips."""
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import re
import shutil
import stat
import struct
import tempfile
import threading

import text_qa_clean as text_stage
from media_checks import resolve, blur
core = text_stage.core
VERSION = 'native-png-midpoints-v1'


class CacheCapacityError(RuntimeError):
    """Infrastructure failure must stop instead of classifying later rows as bad."""


class CacheIntegrityError(RuntimeError):
    """Never silently refresh/overwrite a corrupt immutable cache."""


class CacheMissError(RuntimeError):
    """Cache-only judging never silently decodes a missing entry."""


def png_size(path):
    with path.open('rb') as stream:
        head = stream.read(24)
    core.require(len(head) == 24 and head[:16] == b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR', 'invalid cached PNG header')
    return struct.unpack('>II', head[16:24])


def signature(path):
    value = path.stat()
    core.require(stat.S_ISREG(value.st_mode) and value.st_size > 0, 'media must be nonempty regular file')
    return core.stamp(path)


def metadata(path, kind, runtime, timeout):
    if kind == 'video':
        return blur.probe(path, runtime['ffprobe'], timeout)
    obj = json.loads(blur.run_command([runtime['ffprobe'], '-v', 'error', '-protocol_whitelist', 'file',
        '-select_streams', 'v:0', '-show_entries', 'stream=width,height', '-of', 'json', str(path)], timeout))
    core.require(len(obj.get('streams', [])) == 1, 'still image has no video stream')
    return dict(obj['streams'][0], pts_origin=0)


def extract(path, target, destination, runtime, timeout):
    command = [runtime['ffmpeg'], '-n', '-hide_banner', '-loglevel', 'info', '-nostdin',
               '-threads', '1', '-filter_threads', '1', '-copyts']
    if target is not None:
        command += ['-ss', format(target, '.9f')]
    command += ['-protocol_whitelist', 'file', '-i', str(path), '-map', '0:v:0',
                '-an', '-sn', '-dn', '-frames:v', '1', '-vf', 'showinfo', '-fps_mode', 'passthrough',
                '-c:v', 'png', '-threads', '1', str(destination)]
    _, stderr = blur.run_command(command, timeout, include_stderr=True)
    found = re.search(r'\bn:\s*0\s+pts:\s*\S+\s+pts_time:([^\s]+)', stderr)
    core.require(found is not None, 'missing decoded frame timestamp')
    pts = float(found[1]); core.require(math.isfinite(pts), 'nonfinite frame timestamp')
    return pts


class FrameCache:
    def __init__(self, c, runtime):
        self.c, self.runtime = c, runtime
        self.extractor_sha256 = core.digest_file(Path(__file__))
        self.profile = {'version':VERSION, 'decoder':'ffmpeg', 'format':'PNG_native_no_resize',
                        'sampling':'clip_uniform_midpoints', 'frames_per_video':c['frames_per_video'],
                        'max_frame_pixels':c['max_frame_pixels'], 'max_frame_bytes':c['max_frame_bytes'],
                        'extractor_sha256':self.extractor_sha256,
                        'ffmpeg_sha256':runtime['ffmpeg_sha256'], 'ffprobe_sha256':runtime['ffprobe_sha256']}
        self.root = Path(c['frame_cache_root'])
        self.owner = core.writer_lock(self.root)
        self.owner.__enter__()
        try:
            self.lock = threading.Lock()
            self.stripes = [threading.Lock() for _ in range(c['workers']*8)]
            self.usage = self.root/'usage.json'
            self.limit = c['frame_cache_limit_mb']*1024*1024
            if not self.usage.exists():
                # Never adopt an unknown old cache or walk a media/cache tree.
                with os.scandir(self.root) as entries:
                    names = [e.name for e in itertools.islice(entries, 3)]
                core.require(set(names) <= {'writer.lock'}, 'cache root has content but no capacity ledger')
                core.save(self.usage, {'allocated_bytes': 0, 'version': 1})
            obj = json.loads(self.usage.read_bytes())
            self.allocated = obj['allocated_bytes']
            core.require(obj['version'] == 1 and type(self.allocated) is int and 0 <= self.allocated <= self.limit,
                         'invalid/exhausted frame cache ledger')
        except BaseException:
            self.owner.__exit__(None, None, None)
            raise

    def close(self):
        self.owner.__exit__(None, None, None)

    def account(self, delta):
        with self.lock:
            if self.allocated+delta > self.limit:
                raise CacheCapacityError('frame cache capacity exhausted; choose new root/limit before continuing')
            if shutil.disk_usage(self.root).free < max(delta, 0)+1024**3:
                raise CacheCapacityError('frame cache disk needs reservation plus1GiB free')
            value = self.allocated+delta
            fd, name = tempfile.mkstemp(prefix='.usage-', dir=self.root)
            with os.fdopen(fd, 'wb') as out:
                out.write(core.encoded({'allocated_bytes': value, 'version': 1})); out.flush(); os.fsync(out.fileno())
            os.replace(name, self.usage)
            self.allocated = value

    def key(self, path, before, ref):
        profile = dict(self.profile,ffmpeg_sha256=self.runtime['ffmpeg_sha256'],ffprobe_sha256=self.runtime['ffprobe_sha256'])
        identity = {'version': VERSION, 'source': str(path), 'stat': before, 'kind': ref['kind'],
                    'dataset':self.c['cache_namespace'], 'profile':profile,
                    'extractor_sha256': self.extractor_sha256,
                    'start': ref['start'], 'end': ref['end'], 'frames_per_video': self.c['frames_per_video'],
                    'max_frame_pixels': self.c['max_frame_pixels'], 'max_frame_bytes': self.c['max_frame_bytes'],
                    'ffmpeg_sha256': self.runtime['ffmpeg_sha256'], 'ffprobe_sha256': self.runtime['ffprobe_sha256']}
        return hashlib.sha256(core.encoded(identity)).hexdigest(), identity

    def verify(self, directory, identity):
        manifest = directory/'manifest.json'
        core.require(manifest.stat().st_size <= 65536, 'frame manifest too large')
        checksum = directory/'manifest.sha256'
        core.require(checksum.stat().st_size == 65 and checksum.read_text(encoding='ascii').strip()
                     == core.digest_file(manifest), 'frame manifest SHA mismatch')
        obj = json.loads(manifest.read_bytes())
        core.require(obj['identity'] == identity, 'frame cache identity mismatch')
        expected = self.c['frames_per_video'] if identity['kind'] == 'video' else 1
        core.require(len(obj['frames']) == expected, 'frame cache count mismatch')
        for i, frame in enumerate(obj['frames']):
            core.require(frame['file'] == '%02d.png' % i, 'invalid cached frame name')
            file = directory/frame['file']
            core.require(file.resolve().parent == directory.resolve() and not file.is_symlink(), 'unsafe cached frame')
            core.require(file.stat().st_size == frame['bytes'] <= self.c['max_frame_bytes'], 'cached frame size mismatch')
            core.require(core.digest_file(file) == frame['sha256'], 'cached frame SHA mismatch')
            core.require(list(png_size(file)) == frame['size'], 'cached PNG dimensions mismatch')
        return obj

    def get(self, ref):
        path = resolve(ref['path'], self.c)
        before = signature(path)
        key, identity = self.key(path, before, ref)
        profile_key = hashlib.sha256(core.encoded(identity['profile'])).hexdigest()[:20]
        directory = self.root/self.c['cache_namespace']/profile_key/key[:2]/key
        core.require(directory.resolve().is_relative_to(self.root.resolve()), 'unsafe cache directory')
        with self.stripes[int(key[:8], 16) % len(self.stripes)]:
            hit = directory.exists()
            if hit:
                try:
                    obj = self.verify(directory, identity)
                except Exception as exc:
                    raise CacheIntegrityError('cache verification failed: '+key) from exc
            else:
                if self.c['frame_cache_mode'] == 'require_hit':
                    raise CacheMissError('required cache missing: '+key+'; run explicit cache preparation first')
                count = self.c['frames_per_video'] if ref['kind'] == 'video' else 1
                info = metadata(path, ref['kind'], self.runtime, self.c['timeout_seconds'])
                width, height = info.get('width'), info.get('height')
                core.require(type(width) is int and type(height) is int and width > 0 and height > 0
                             and width*height <= self.c['max_frame_pixels'], 'native frame exceeds pixel budget/invalid dimensions')
                if ref['kind'] == 'video':
                    start = 0.0 if ref['start'] is None else float(ref['start'])
                    end = info['duration'] if ref['end'] is None else float(ref['end'])
                    core.require(0 <= start < end <= info['duration']+.001, 'clip outside video duration')
                    end = min(end, info['duration'])
                    times = [start+(end-start)*(i+.5)/count for i in range(count)]
                else:
                    start = end = None; times = [None]
                reservation = count*self.c['max_frame_bytes']+65536
                self.account(reservation)
                directory.parent.mkdir(parents=True, exist_ok=True)
                stage = Path(tempfile.mkdtemp(prefix='.stage-', dir=directory.parent))
                frames, seen = [], set()
                for i, target in enumerate(times):
                    name = '%02d.png' % i; file = stage/name
                    pts = extract(path, target, file, self.runtime, self.c['timeout_seconds'])
                    relative = pts-info['pts_origin']
                    if ref['kind'] == 'video':
                        core.require(start-.001 <= relative < end and pts not in seen,
                                     'duplicate/outside-clip decoded frame; not enough distinct visual evidence')
                    seen.add(pts)
                    size = png_size(file)
                    core.require(size == (width, height), 'unexpected transformed frame dimensions')
                    core.require(file.stat().st_size <= self.c['max_frame_bytes'], 'native PNG exceeds frame byte cap')
                    frames.append({'file': name, 'requested_sec': target, 'pts_sec': pts,
                                   'relative_pts_sec': relative, 'bytes': file.stat().st_size,
                                   'size': list(size), 'sha256': core.digest_file(file)})
                core.require(signature(path) == before, 'media changed during frame extraction')
                obj = {'identity': identity, 'clip_start': start, 'clip_end': end, 'frames': frames}
                core.save(stage/'manifest.json', obj)
                with (stage/'manifest.sha256').open('xb') as out:
                    out.write((core.digest_file(stage/'manifest.json')+'\n').encode('ascii'))
                    out.flush(); os.fsync(out.fileno())
                actual = sum(f['bytes'] for f in frames)+(stage/'manifest.json').stat().st_size+65
                core.require(actual <= reservation, 'frame cache reservation exceeded')
                os.rename(stage, directory)
                self.account(actual-reservation)
                obj = self.verify(directory, identity)
            core.require(signature(path) == before, 'media changed during cache read')
        return dict(obj, cache_key=key, cache_hit=hit, directory=str(directory))

    def frames(self, refs):
        visual = [ref for ref in refs if ref['kind'] in ('image', 'video')]
        count = sum(self.c['frames_per_video'] if r['kind'] == 'video' else 1 for r in visual)
        core.require(0 < count <= self.c['max_images_per_request'], 'visual frame count outside request budget')
        entries = [self.get(ref) for ref in visual]
        result = []
        for media_index, entry in enumerate(entries):
            for frame in entry['frames']:
                result.append(dict(frame, frame_id=len(result)+1, media_index=media_index,
                                   cache_key=entry['cache_key'], cache_hit=entry['cache_hit'],
                                   path=str(Path(entry['directory'])/frame['file']),
                                   clip_start=entry['clip_start'], clip_end=entry['clip_end']))
        core.require(sum(frame['bytes'] for frame in result) <= self.c['max_request_image_bytes'],
                     'native images exceed request byte budget; no silent downscale/drop')
        return result

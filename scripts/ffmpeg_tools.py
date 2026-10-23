"""Bounded FFmpeg primitives; no dataset paths or model dependency."""
import json
import math
import os
from pathlib import Path
import re
import shutil
import subprocess
import threading

FILTER = ('blurdetect=low=0.078431372549:high=0.196078431373:'
          'radius=50:block_width=32:block_height=32:block_pct=100:planes=1')

def stat_id(path):
    value = Path(path).stat()
    return {'path': str(path), 'bytes': value.st_size, 'mtime_ns': value.st_mtime_ns}

def finite(value):
    return type(value) in (int, float) and math.isfinite(value)

def run_command(command, timeout, include_stderr=False):
    """No shell, no local logs; cap each output pipe at1MiB while child runs."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE)
    outputs = [bytearray(), bytearray()]; overflow = threading.Event()
    def drain(pipe, buffer):
        try:
            while True:
                chunk = pipe.read(65536)
                if not chunk:
                    break
                if len(buffer)+len(chunk) > 1024*1024:
                    overflow.set()
                    try:
                        process.kill()
                    except OSError:
                        pass
                    break
                buffer.extend(chunk)
        finally:
            pipe.close()
    threads = [threading.Thread(target=drain,args=(pipe,buffer),daemon=True)
               for pipe,buffer in zip((process.stdout,process.stderr),outputs)]
    for thread in threads:
        thread.start()
    try:
        process.wait(timeout=timeout)
    except BaseException:
        process.kill(); process.wait()
        raise
    finally:
        for thread in threads:
            thread.join(timeout=2)
    if overflow.is_set():
        raise RuntimeError('tool output exceeds1MiB')
    if process.returncode:
        raise RuntimeError('tool exited '+str(process.returncode)+': '+outputs[1].decode('utf8','replace')[-1200:])
    stdout, stderr = (value.decode('utf8', 'replace') for value in outputs)
    return (stdout, stderr) if include_stderr else stdout

def probe(path, tool, timeout):
    obj = json.loads(run_command([tool, '-v','error','-protocol_whitelist','file',
        '-select_streams','v:0', '-show_entries',
        'stream=duration,start_time,width,height,codec_name:format=duration,start_time',
        '-of','json', str(path)], timeout))
    streams = obj.get('streams', [])
    if not streams:
        raise ValueError('no video stream')
    stream = streams[0]
    # Prefer video-stream duration: a longer embedded audio track must not extend sampling.
    duration = stream.get('duration', obj.get('format',{}).get('duration'))
    if duration in (None, 'N/A'):
        duration = obj.get('format',{}).get('duration')
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError('missing positive media duration')
    origin = float(stream.get('start_time', obj.get('format',{}).get('start_time',0)) or 0)
    if not math.isfinite(origin):
        raise ValueError('invalid stream start_time')
    return {'duration': duration, 'pts_origin': origin, 'width': stream.get('width'),
            'height': stream.get('height'), 'codec': stream.get('codec_name')}

def frame_command(ffmpeg, path, timestamp):
    return [ffmpeg, '-hide_banner', '-loglevel','error','-nostdin', '-threads','1',
        '-filter_threads','1','-copyts','-ss',format(timestamp,'.9f'),
        '-protocol_whitelist','file','-i',str(path), '-map','0:v:0','-an','-sn','-dn',
        '-frames:v','1','-vf', FILTER+',metadata=mode=print:key=lavfi.blur:file=-',
        '-f','null','-']

def parse_frame(text):
    score = re.search(r'^lavfi\.blur=([^\r\n]+)', text, re.M)
    pts = re.search(r'\bpts_time:([^\s]+)', text)
    if not score or not pts:
        raise ValueError('missing blur score or decoded timestamp')
    score, pts = float(score[1]), float(pts[1])
    if not math.isfinite(score) or score < 0 or not math.isfinite(pts):
        raise ValueError('nonfinite/invalid blur score or timestamp')
    return score, pts

def detect(ref, runtime, threshold=None, timeout=30, resolver=Path):
    result = dict(ref, status='error', mean_blur=None, is_blurry=None, frames=[])
    try:
        path = resolver(ref['path'])
        before = stat_id(path)
        metadata = probe(path, runtime['ffprobe'], timeout)
        start = 0.0 if ref['start'] is None else float(ref['start'])
        end = metadata['duration'] if ref['end'] is None else float(ref['end'])
        if not 0 <= start < end <= metadata['duration'] + .001:
            raise ValueError('clip outside video duration')
        end = min(end, metadata['duration'])
        timestamps = [start+(end-start)*(i+.5)/8 for i in range(8)]
        values, seen_pts = [], set()
        for target in timestamps:
            frame = {'requested_sec': target}
            try:
                score, pts = parse_frame(run_command(frame_command(runtime['ffmpeg'], path, target),timeout))
                relative_pts = pts - metadata['pts_origin']
                frame.update(blur=score, pts_sec=pts, relative_pts_sec=relative_pts)
                if relative_pts < start-.001 or relative_pts >= end:
                    raise ValueError('decoded frame outside requested clip')
                if pts in seen_pts:
                    raise ValueError('duplicate decoded frame timestamp')
                seen_pts.add(pts); values.append(score)
            except (ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
                frame['error'] = str(exc)[:1400]
            result['frames'].append(frame)
        if stat_id(path) != before:
            raise ValueError('video file changed during detection')
        result.update(resolved_path=str(path), media_stat=before, metadata=metadata,
                      valid_frames=len(values), attempted_frames=8)
        # Conservative: do not classify partial failures as clear/blurry.
        if len(values) != 8:
            raise ValueError('need8 distinct valid sampled frames; return unknown')
        mean = sum(values)/len(values)
        result.update(status='ok', mean_blur=mean, is_blurry=label(mean,threshold))
    except (ValueError, RuntimeError, OSError, TypeError, subprocess.TimeoutExpired) as exc:
        result['error'] = str(exc)[:1600]
    return result


def tools(ffmpeg, ffprobe):
    bins = {}
    for name, command in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        resolved = shutil.which(command)
        if not resolved:
            raise ValueError(name+" unavailable; specify existing executable; no automatic installation")
        bins[name] = resolved
    versions = {k:run_command([v,"-version"],15).splitlines()[0] for k,v in bins.items()}
    help_text = run_command([bins["ffmpeg"],"-hide_banner","-h","filter=blurdetect"],15)
    if "block_pct" not in help_text or "planes" not in help_text:
        raise ValueError("FFmpeg lacks required blurdetect options")
    return dict(bins, versions=versions, filter=FILTER)

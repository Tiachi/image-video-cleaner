"""Standalone cached-PNG -> OpenAI image API client. Python>=3.10, stdlib only.

No vLLM installation, torch, media decoder, shared filesystem or server patch.
This transports sampled images with text timestamps, NOT native video tokens.
Copy this single file to another client to use it without the cleaning pipeline.
"""
import argparse
import base64
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import re
import struct
import sys
import time
from urllib.parse import urlparse
import zlib

VERSION = 'cached-png-http-v1'
PNG_HEADER = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def unique_keys(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, 'duplicate JSON key')
        result[key] = value
    return result


def invalid_number(value):
    raise ValueError('nonfinite JSON number')


def safe_float(value):
    result = float(value)
    require(math.isfinite(result), 'nonfinite JSON number')
    return result


def loads(data):
    return json.loads(data, object_pairs_hook=unique_keys, parse_constant=invalid_number, parse_float=safe_float)


def encoded(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True).encode('utf-8')


def endpoint(base):
    require(isinstance(base, str) and not any(ch.isspace() or ord(ch)<32 or ord(ch)==127
            for ch in base) and '\\' not in base, 'invalid API URL')
    p = urlparse(base)
    require(p.scheme in ('http', 'https') and p.hostname and p.username is None and p.password is None
            and not p.query and not p.fragment and p.path in ('/v1', '/v1/'),
            'API must be http(s)://host[:port]/v1 without credentials/query/fragment')
    port = p.port
    require(port is None or 1<=port<=65535, 'invalid API port')
    return p.scheme, p.hostname, port or (443 if p.scheme=='https' else 80)


def chat_body(model, system, content, *, processor_kwargs=None, max_tokens=32):
    require(isinstance(model, str) and model.strip(), 'served model alias required')
    require(isinstance(system, str) and isinstance(content, (str, list)), 'invalid messages')
    require(type(max_tokens) is int and 1<=max_tokens<=4096, 'invalid output token cap')
    body = {'model': model, 'messages': [{'role':'system', 'content':system},
                                       {'role':'user', 'content':content}],
            'temperature':0, 'max_tokens':max_tokens, 'chat_template_kwargs':{'enable_thinking':False}}
    if processor_kwargs is not None:
        require(isinstance(processor_kwargs, dict) and set(processor_kwargs)=={'max_pixels'}
                and type(processor_kwargs['max_pixels']) is int
                and 4096<=processor_kwargs['max_pixels']<=16777216, 'invalid image processor budget')
        body['mm_processor_kwargs'] = dict(processor_kwargs)
    return body


def processor_options(mode, kwargs):
    require(mode in ('request','server_default'), 'processor mode must be request/server_default')
    # Validate the declared budget even when it cannot control the remote server.
    chat_body('validation-only','',[],processor_kwargs=kwargs)
    return dict(kwargs) if mode=='request' else None


def exchange(connection, method, path, body=None, headers=None):
    """One bounded attempt, no environment proxy, redirect or hidden replay."""
    connection.request(method, path, body, headers or {'Content-Type':'application/json'})
    response = connection.getresponse()
    raw = response.read(65537)
    require(len(raw)<=65536, 'response exceeds64KiB')
    # Do not expose a response body that may contain private request data/keys.
    require(response.status==200, 'HTTP status '+str(response.status)+'; no retry or fallback')
    obj = loads(raw)
    require(isinstance(obj, dict), 'API response must be an object')
    return obj


def completion(obj):
    choice = obj['choices'][0]
    content = choice['message']['content']
    require(isinstance(content, str) and content.strip(), 'empty/non-text API answer')
    return content, obj.get('usage', {})


def png_content(frames, *, max_frames=32, max_frame_bytes=83886080, max_bytes=33554432):
    """Verify bytes again immediately before sending. Never resize/re-encode."""
    require(isinstance(frames, list) and 0<len(frames)<=max_frames, 'invalid request frame count')
    # Bound allocation before any PNG reads/base64 copies.
    require(all(type(f.get('bytes')) is int and 0<f['bytes']<=max_frame_bytes for f in frames),
            'invalid frame byte declaration')
    require(sum(f['bytes'] for f in frames)<=max_bytes, 'request PNG byte budget exceeded')
    result = []
    for index, frame in enumerate(frames):
        require(type(frame['frame_id']) is int and frame['frame_id']==index+1, 'frame IDs must be ordered1..N')
        path = Path(frame['path'])
        require(path.is_file() and not path.is_symlink() and path.stat().st_size==frame['bytes'],
                'cached PNG path/size changed')
        with path.open('rb') as stream:
            data = stream.read(frame['bytes']+1)
        require(len(data)==frame['bytes'] and hashlib.sha256(data).hexdigest()==frame['sha256'],
                'cached PNG bytes changed')
        require(len(data)>=24 and data[:16]==PNG_HEADER, 'cached frame is not PNG')
        if 'size' in frame:
            require(list(struct.unpack('>II',data[16:24]))==list(frame['size']), 'cached PNG geometry changed')
        descriptor = {k:frame[k] for k in ('frame_id','media_index','relative_pts_sec','clip_start','clip_end')}
        # Do not send source filenames, manifests, cache paths or upstream labels.
        result += [{'type':'text', 'text':json.dumps(descriptor, allow_nan=False)},
                   {'type':'image_url','image_url':{'url':'data:image/png;base64,'+
                                                  base64.b64encode(data).decode('ascii')}}]
    return result


def load_manifest(directory, *, max_frames=32, max_bytes=33554432):
    """Read exactly one published native PNG entry; no tree walk or decoding.

    The paired checksum proves consistency, not authentication. The pipeline
    additionally verifies source/decoder identity using FrameCache.get().
    """
    directory = Path(directory)
    require(directory.is_dir() and not directory.is_symlink(), 'cache entry must be a regular directory')
    manifest, receipt = directory/'manifest.json', directory/'manifest.sha256'
    require(not manifest.is_symlink() and manifest.is_file() and 0<manifest.stat().st_size<=65536,
            'invalid/unfinished cache manifest')
    require(not receipt.is_symlink() and receipt.is_file() and receipt.stat().st_size==65,
            'missing cache completion checksum')
    raw = manifest.read_bytes()
    require(receipt.read_text(encoding='ascii')==hashlib.sha256(raw).hexdigest()+'\n',
            'cache manifest SHA mismatch')
    obj = loads(raw)
    identity = obj['identity']
    require(identity['version']=='native-png-midpoints-v1' and identity['kind'] in ('image','video'),
            'unsupported cache protocol; no UniMoE tensor/pickle loading')
    frames = obj['frames']
    require(isinstance(frames, list) and 0<len(frames)<=max_frames, 'cache frame count outside budget')
    expected = identity['frames_per_video'] if identity['kind']=='video' else 1
    require(type(expected) is int and len(frames)==expected, 'cache frame count differs from identity')
    start, end = obj['clip_start'], obj['clip_end']
    if identity['kind']=='video':
        require(all(type(x) in (float,int) and math.isfinite(x) for x in (start,end)) and 0<=start<end,
                'invalid clip interval')
    result, previous = [], None
    for index, frame in enumerate(frames):
        require(frame['file']=='%02d.png'%index, 'unsafe or unordered cache filename')
        require(isinstance(frame['sha256'],str) and re.fullmatch('[0-9a-f]{64}',frame['sha256']),
                'invalid PNG SHA')
        pts = frame['relative_pts_sec']
        require(type(pts) in (float,int) and math.isfinite(pts), 'invalid frame PTS')
        if identity['kind']=='video':
            require(start-.001<=pts<end and (previous is None or previous<pts),
                    'duplicate/outside-clip/nonmonotonic frame PTS')
        previous = pts
        result.append(dict(frame, path=str(directory/frame['file']), frame_id=index+1, media_index=0,
                           clip_start=start, clip_end=end))
    # Full bytes and geometry checks without opening original source media.
    png_content(result, max_frames=max_frames, max_bytes=max_bytes)
    return result


class Client:
    """One client per thread; callers own concurrency. No automatic retries."""
    def __init__(self, api_base, model, timeout=120, api_key_env=None):
        scheme, host, port = endpoint(api_base)
        require(type(timeout) is int and 1<=timeout<=600, 'invalid HTTP timeout')
        require(isinstance(model, str) and model.strip(), 'served model alias required')
        self.model, self.api_base = model, api_base
        self.headers = {'Content-Type':'application/json'}
        if api_key_env:
            require(re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', api_key_env), 'invalid API key environment name')
            key = os.environ.get(api_key_env)
            require(key and not any(ord(c)<32 or ord(c)==127 for c in key), 'API key unavailable/invalid')
            self.headers['Authorization'] = 'Bearer '+key
        cls = http.client.HTTPSConnection if scheme=='https' else http.client.HTTPConnection
        self.connection = cls(host, port, timeout=timeout)

    def close(self):
        self.connection.close()

    def models(self):
        try:
            obj = exchange(self.connection, 'GET', '/v1/models', headers=self.headers)
            require(any(isinstance(m,dict) and m.get('id')==self.model for m in obj['data']),
                    'served model alias not found; alias is case-sensitive')
        except BaseException:
            self.close()
            raise

    def chat(self, body):
        require(body.get('model')==self.model, 'request model differs from configured alias')
        try:
            obj = exchange(self.connection, 'POST', '/v1/chat/completions', encoded(body), self.headers)
            completion(obj)
            return obj
        except BaseException:
            self.close()
            raise


def solid_png(rgb, size=64):
    """Generate a valid synthetic visual probe without PIL/FFmpeg."""
    def chunk(kind, data):
        return struct.pack('>I',len(data))+kind+data+struct.pack('>I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('>IIBBBBB',size,size,8,2,0,0,0))+\
        chunk(b'IDAT',zlib.compress((b'\0'+bytes(rgb)*size)*size))+chunk(b'IEND',b'')


def preflight(client, processor_kwargs, count=8):
    require(type(count) is int and 1<=count<=32, 'probe image count must be1..32')
    client.models()  # Never spend a visual call on an unknown alias.
    palette = [('red',(255,0,0)), ('green',(0,255,0)), ('blue',(0,0,255)), ('yellow',(255,255,0))]
    # Deterministic synthetic order, not exposed in the text descriptors.
    expected, content = [], []
    for index in range(count):
        name, rgb = palette[(index*3+1)%len(palette)]
        expected.append(name)
        content += [{'type':'text','text':'Synthetic frame '+str(index+1)},
                    {'type':'image_url','image_url':{'url':'data:image/png;base64,'+
                       base64.b64encode(solid_png(rgb)).decode('ascii')}}]
    content.append({'type':'text','text':'List the solid background color of every image in image order. '
        'Use only red, green, blue or yellow, comma-separated. Return exactly '+str(count)+' color names.'})
    body = chat_body(client.model, 'Read the supplied images. Return only the requested list.', content,
                     processor_kwargs=processor_kwargs, max_tokens=4*count+16)
    obj = client.chat(body)
    answer, _ = completion(obj)
    observed = [s.strip().lower() for s in answer.strip().split(',')]
    return {'status':'pass' if observed==expected else 'failed_visual_probe',
            'probe':'synthetic-color-order-v1', 'images':count, 'expected':expected, 'observed':observed,
            'request_sha256':hashlib.sha256(encoded(body)).hexdigest(),
            'response':obj, 'processor_kwargs_sent':dict(processor_kwargs) if processor_kwargs is not None else None,
            'model_api_calls':1, 'source_media_reads':0,
            'limits':['synthetic behavior only; does not attest remote processor internals or model weights',
                      'small PNGs do not prove production-size token/memory capacity or judge accuracy']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest='command', required=True)
    info = sub.add_parser('inspect'); info.add_argument('--cache-entry',type=Path,required=True)
    for name in ('check','request'):
        cmd = sub.add_parser(name)
        cmd.add_argument('--config',type=Path)
        cmd.add_argument('--api-base'); cmd.add_argument('--model')
        cmd.add_argument('--api-key-env'); cmd.add_argument('--max-pixels',type=int)
        cmd.add_argument('--processor-mode',choices=('request','server_default'))
        cmd.add_argument('--timeout',type=int)
        cmd.add_argument('--output',type=Path,required=True)
        if name=='check': cmd.add_argument('--images',type=int)
        else:
            cmd.add_argument('--cache-entry',type=Path,required=True)
            cmd.add_argument('--system-file',type=Path,required=True)
            cmd.add_argument('--prompt-file',type=Path,required=True)
    a = p.parse_args()
    if a.command=='inspect':
        frames = load_manifest(a.cache_entry)
        print(json.dumps({'version':VERSION,'frames':[{k:v for k,v in f.items() if k!='path'} for f in frames],
                          'api_calls':0, 'source_media_reads':0},ensure_ascii=False))
        return
    # Flat Stage03 config OR the unified pipeline file; no upstream outputs/FFmpeg needed.
    c = {}
    if a.config:
        require(a.config.stat().st_size<=262144, 'config too large')
        source = loads(a.config.read_bytes())
        c = dict(source.get('stage03',{}), **source.get('api',{})) if 'schema_version' in source else source
    base, model = a.api_base or c.get('api_base'), a.model or c.get('model')
    processor = {'max_pixels':a.max_pixels if a.max_pixels is not None else
                 c.get('vision_processor_kwargs',{}).get('max_pixels',262144)}
    processor = processor_options(a.processor_mode or c.get('vision_processor_mode','request'), processor)
    out = a.output.resolve()
    code = Path(__file__).resolve().parent
    if code.name=='scripts': code=code.parent
    require(a.output.is_absolute() and not out.is_relative_to(code), 'report must be absolute outside code')
    # Reserve the unique result before any HTTP request, never overwrite/reissue unknowingly.
    out.parent.mkdir(parents=True,exist_ok=True)
    started = time.monotonic()
    with out.open('xb') as stream:
        report = {'version':VERSION, 'status':'failed', 'api_base':base, 'model':model,
                  'model_identity':'endpoint_alias_only', 'remote_source_modified':False}
        client = None
        try:
            client = Client(base,model,a.timeout if a.timeout is not None else c.get('timeout_seconds',120),a.api_key_env)
            if a.command=='check':
                report.update(preflight(client,processor,a.images if a.images is not None else c.get('max_images_per_request',8)))
            else:
                frames = load_manifest(a.cache_entry)
                for file in (a.system_file,a.prompt_file): require(file.stat().st_size<=65536, 'prompt too large')
                content = png_content(frames)+[{'type':'text','text':a.prompt_file.read_text(encoding='utf-8')}]
                body = chat_body(model,a.system_file.read_text(encoding='utf-8'),content,processor_kwargs=processor)
                report.update(status='complete',response=client.chat(body),frames=len(frames),
                              request_sha256=hashlib.sha256(encoded(body)).hexdigest(),
                              processor_kwargs_sent=processor,protocol='sampled-images-with-text-timestamps',
                              evidence=[{k:f[k] for k in ('frame_id','sha256','relative_pts_sec','clip_start','clip_end')} for f in frames])
        except Exception as exc:
            # Do not write response bodies, authorization values or base64 payloads.
            report['error_type'] = type(exc).__name__
            if type(exc) is ValueError: report['error'] = str(exc)[:200]
        finally:
            if client: client.close()
            report['elapsed_seconds'] = round(time.monotonic()-started,3)
            stream.write(encoded(report)+b'\n'); stream.flush(); os.fsync(stream.fileno())
        print(json.dumps({k:report[k] for k in ('version','status','elapsed_seconds')},ensure_ascii=False),flush=True)
        print('FRAME API REPORT: '+str(out),flush=True)
        require(report['status'] in ('pass','complete'), 'frame API failed; inspect the saved report; no automatic retry')


if __name__=='__main__':
    try: main()
    except Exception as exc:
        print('FRAME API FAIL: '+type(exc).__name__+': '+str(exc),file=sys.stderr)
        sys.exit(1)

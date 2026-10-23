"""Real synthetic PNG/loopback transport; no real media/model/NPU calls."""
import base64
from copy import deepcopy
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch
import zlib

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import frame_cache_client as api
import media_qa_clean as media
import pipeline_config as pipeline


def pixel(data):
    """Independently verify generated PNG chunks/CRCs and decode the first pixel."""
    offset, compressed = 8, b''
    while offset<len(data):
        size = struct.unpack('>I',data[offset:offset+4])[0]
        kind, payload = data[offset+4:offset+8], data[offset+8:offset+8+size]
        crc = struct.unpack('>I',data[offset+8+size:offset+12+size])[0]
        if crc != zlib.crc32(kind+payload)&0xffffffff: raise ValueError('bad PNG CRC')
        if kind==b'IDAT': compressed += payload
        offset += size+12
    return tuple(zlib.decompress(compressed)[1:4])


class CacheClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.entry = self.root/'cache'; self.entry.mkdir()
        frames = []
        for index, color in enumerate(((255,0,0),(0,255,0))):
            data = api.solid_png(color); name = '%02d.png'%index
            (self.entry/name).write_bytes(data)
            frames.append({'file':name,'relative_pts_sec':12.5+index*5,'requested_sec':12.5+index*5,
                           'pts_sec':12.5+index*5,'bytes':len(data),'size':[64,64],
                           'sha256':hashlib.sha256(data).hexdigest()})
        self.manifest = {'identity':{'version':'native-png-midpoints-v1','kind':'video',
                            'source':'/does/not/exist/source.mp4','frames_per_video':2},
                         'clip_start':10,'clip_end':20,'frames':frames}
        self.publish()

    def publish(self):
        raw = api.encoded(self.manifest)
        (self.entry/'manifest.json').write_bytes(raw)
        (self.entry/'manifest.sha256').write_bytes((hashlib.sha256(raw).hexdigest()+'\n').encode('ascii'))

    def test_cache_only_transports_original_png_and_pts_without_source_media(self):
        frames = api.load_manifest(self.entry)
        content = api.png_content(frames)
        self.assertEqual(len(content),4)
        descriptor = json.loads(content[0]['text'])
        self.assertEqual(descriptor['relative_pts_sec'],12.5)
        self.assertEqual(descriptor['clip_start'],10)
        self.assertNotIn('source',descriptor); self.assertNotIn('path',descriptor)
        for index in range(2):
            data = base64.b64decode(content[2*index+1]['image_url']['url'].split(',',1)[1],validate=True)
            self.assertEqual(data,(self.entry/('%02d.png'%index)).read_bytes())
        self.assertEqual(pixel(data),(0,255,0))

    def test_incomplete_or_tampered_manifest_is_not_adopted(self):
        (self.entry/'manifest.sha256').unlink()
        with self.assertRaisesRegex(ValueError,'checksum'): api.load_manifest(self.entry)
        self.publish(); (self.entry/'manifest.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError,'SHA'): api.load_manifest(self.entry)

    def test_frame_tampering_and_non_png_fail_before_http(self):
        frames = api.load_manifest(self.entry)
        (self.entry/'00.png').write_bytes(b'changed')
        with self.assertRaises(ValueError): api.png_content(frames)
        frames[0].update(bytes=7,sha256=hashlib.sha256(b'changed').hexdigest())
        with self.assertRaisesRegex(ValueError,'not PNG'): api.png_content(frames)

    def test_unsafe_names_and_invalid_pts_fail(self):
        for change in ({'file':'../outside.png'},{'relative_pts_sec':25},{'relative_pts_sec':float('nan')}):
            original = deepcopy(self.manifest)
            self.manifest['frames'][0].update(change)
            if any(isinstance(v,float) and v!=v for v in change.values()):
                with self.assertRaises(ValueError): self.publish()
            else:
                self.publish()
                with self.assertRaises(ValueError): api.load_manifest(self.entry)
            self.manifest = original

    def test_duplicate_pts_and_wrong_cache_format_fail(self):
        self.manifest['frames'][1]['relative_pts_sec']=12.5; self.publish()
        with self.assertRaisesRegex(ValueError,'PTS'): api.load_manifest(self.entry)
        self.manifest['identity']['version']='hf-jpeg-torch'; self.publish()
        with self.assertRaisesRegex(ValueError,'unsupported cache'): api.load_manifest(self.entry)

    def test_byte_budget_precedes_reads_and_never_drops_frames(self):
        frames = api.load_manifest(self.entry)
        with patch.object(Path,'open',side_effect=AssertionError('must not read')):
            with self.assertRaisesRegex(ValueError,'budget'): api.png_content(frames,max_bytes=1)
        with self.assertRaisesRegex(ValueError,'count'): api.png_content(frames,max_frames=1)

    def test_stage03_uses_adapter_and_processor_budget_but_text_does_not(self):
        judge = object.__new__(media.Judge)
        judge.config = dict(media.DEFAULT,model='Qwen-Exact-Alias',vision_processor_kwargs={'max_pixels':524288})
        judge.prompt = 'synthetic'
        frames = api.load_manifest(self.entry)
        self.assertEqual(judge.content(frames),api.png_content(frames))
        body = judge.request_body(judge.content(frames))
        self.assertEqual(body['model'],'Qwen-Exact-Alias')
        self.assertEqual(body['mm_processor_kwargs'],{'max_pixels':524288})
        self.assertNotIn('mm_processor_kwargs',api.chat_body('Qwen','text','text'))
        (self.entry/'00.png').write_bytes(b'bad')
        with self.assertRaises(media.CacheIntegrityError): judge.content(frames)

    def test_pipeline_api_check_does_not_require_upstream_data_or_vllm_python(self):
        c = deepcopy(pipeline.DEFAULT); c['paths']['work_root']=str(self.root/'work')
        c['api']={'api_base':'http://192.0.2.1:8000/v1','model':'qwen'}
        resolved = pipeline.resolve(c)
        command = pipeline.child_command(resolved,'stage03','api-check',self.root/'config.json')
        self.assertIn('frame_cache_client.py',command[3]); self.assertEqual(command[4],'check')
        self.assertIn('--output',command)
        self.assertFalse((self.root/'work').exists())

    def test_processor_server_default_is_explicit_not_silent_fallback(self):
        judge=object.__new__(media.Judge); judge.prompt='synthetic'
        judge.config=dict(media.DEFAULT,vision_processor_mode='server_default')
        self.assertNotIn('mm_processor_kwargs',judge.request_body([]))
        judge.config['vision_processor_mode']='typo'
        with self.assertRaises(ValueError): judge.request_body([])

    def test_legacy_pipeline_config_migrates_without_overwrite(self):
        c=deepcopy(pipeline.DEFAULT); c['paths']['work_root']=str(self.root/'work')
        del c['stage03']['vision_processor_mode']
        r=pipeline.resolve(c)
        self.assertEqual(r['stage03']['vision_processor_mode'],'request')
        self.assertNotIn('vision_processor_mode',c['stage03'])

    def test_single_file_help_needs_no_pipeline_or_installed_packages(self):
        code = self.root/'isolated'; code.mkdir()
        standalone = code/'client.py'; shutil.copyfile(api.__file__,standalone)
        r = subprocess.run([sys.executable,'-I','-B',str(standalone),'--help'],cwd=code,
                           capture_output=True,text=True,timeout=10)
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('inspect',r.stdout)


class LoopbackTests(unittest.TestCase):
    def setUp(self):
        self.received = []; self.alias='Qwen-Case'; self.fail_status=None; self.wrong=False
        owner = self
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_GET(self):
                owner.received.append(('GET',self.path,None,self.headers.get('Authorization')))
                self.reply(200,{'data':[{'id':owner.alias}]})
            def reply(self,status,obj):
                data = json.dumps(obj).encode()
                self.send_response(status); self.send_header('Content-Length',str(len(data)))
                self.end_headers(); self.wfile.write(data)
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.received.append(('POST',self.path,body,self.headers.get('Authorization')))
                if owner.fail_status: return self.reply(owner.fail_status,{'error':'private body not saved'})
                colors=[]
                for item in body['messages'][1]['content']:
                    if item['type']=='image_url':
                        data=base64.b64decode(item['image_url']['url'].split(',',1)[1])
                        colors.append({(255,0,0):'red',(0,255,0):'green',(0,0,255):'blue',(255,255,0):'yellow'}[pixel(data)])
                text='wrong' if owner.wrong else ','.join(colors)
                self.reply(200,{'choices':[{'message':{'content':text},'finish_reason':'stop'}],
                                'usage':{'completion_tokens':len(colors)}})
            def log_message(self,*args): pass
        self.server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_port)+'/v1'
        self.client=api.Client(self.base,self.alias)
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.client.close(); self.server.shutdown(); self.server.server_close(); self.thread.join(2)

    def test_probe_checks_exact_alias_frame_order_and_kwargs_without_proxy(self):
        with patch.dict(os.environ,{'HTTP_PROXY':'http://invalid.invalid:1','HTTPS_PROXY':'http://invalid.invalid:1'}):
            report=api.preflight(self.client,{'max_pixels':262144},8)
        self.assertEqual(report['status'],'pass')
        self.assertEqual([r[0] for r in self.received],['GET','POST'])
        self.assertEqual(self.received[1][2]['mm_processor_kwargs'],{'max_pixels':262144})
        self.assertEqual(len([v for v in self.received[1][2]['messages'][1]['content'] if v['type']=='image_url']),8)
        self.assertNotIn('data:image',json.dumps(report))

    def test_wrong_alias_stops_before_visual_request(self):
        self.alias='different'
        with self.assertRaisesRegex(ValueError,'alias not found'): api.preflight(self.client,{'max_pixels':4096})
        self.assertEqual(len(self.received),1)

    def test_gateway_rejection_and_redirect_have_no_fallback_or_retry(self):
        for status in (301,400,500):
            self.fail_status=status
            with self.assertRaisesRegex(ValueError,'HTTP status '+str(status)):
                api.preflight(self.client,{'max_pixels':4096})
        self.assertEqual([r[0] for r in self.received],['GET','POST']*3)

    def test_wrong_visual_output_is_not_a_pass(self):
        self.wrong=True
        self.assertEqual(api.preflight(self.client,{'max_pixels':4096},2)['status'],'failed_visual_probe')

    def test_explicit_server_default_sends_no_processor_extension(self):
        report=api.preflight(self.client,None,2)
        self.assertEqual(report['status'],'pass')
        self.assertIsNone(report['processor_kwargs_sent'])
        self.assertNotIn('mm_processor_kwargs',self.received[1][2])

    def test_auth_stays_in_header_not_reports(self):
        with patch.dict(os.environ,{'SYNTHETIC_FRAME_TEST_KEY':'test-only-not-a-real-key'}):
            client=api.Client(self.base,self.alias,api_key_env='SYNTHETIC_FRAME_TEST_KEY')
            try: report=api.preflight(client,{'max_pixels':4096},1)
            finally: client.close()
        self.assertEqual(self.received[0][3],'Bearer test-only-not-a-real-key')
        self.assertNotIn('test-only-not-a-real-key',json.dumps(report))

    def test_cli_report_no_overwrite_and_failed_probe_remains_failed(self):
        with tempfile.TemporaryDirectory() as temp:
            report=Path(temp)/'probe.json'
            args=['client','check','--api-base',self.base,'--model',self.alias,'--images','2','--output',str(report)]
            self.wrong=True
            with patch.object(sys,'argv',args):
                with self.assertRaisesRegex(ValueError,'frame API failed'): api.main()
            obj=json.loads(report.read_bytes())
            self.assertEqual(obj['status'],'failed_visual_probe')
            calls=len(self.received)
            with patch.object(sys,'argv',args):
                with self.assertRaises(FileExistsError): api.main()
            self.assertEqual(len(self.received),calls)

    def test_standalone_request_reads_one_cache_and_saves_response_without_payload(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp); entry=root/'cache'; entry.mkdir()
            data=api.solid_png((255,0,0)); (entry/'00.png').write_bytes(data)
            manifest={'identity':{'version':'native-png-midpoints-v1','kind':'image','frames_per_video':8},
                      'clip_start':None,'clip_end':None,'frames':[{'file':'00.png','relative_pts_sec':0,
                          'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest(),'size':[64,64]}]}
            raw=api.encoded(manifest); (entry/'manifest.json').write_bytes(raw)
            (entry/'manifest.sha256').write_bytes((hashlib.sha256(raw).hexdigest()+'\n').encode())
            system=root/'system.txt'; system.write_text('Return color only.',encoding='utf-8')
            prompt=root/'prompt.txt'; prompt.write_text('Audit synthetic evidence.',encoding='utf-8')
            out=root/'response.json'
            args=['client','request','--api-base',self.base,'--model',self.alias,'--cache-entry',str(entry),
                  '--system-file',str(system),'--prompt-file',str(prompt),'--output',str(out)]
            with patch.object(sys,'argv',args): api.main()
            saved=json.loads(out.read_bytes())
            self.assertEqual(saved['status'],'complete')
            self.assertEqual(saved['response']['choices'][0]['message']['content'],'red')
            self.assertNotIn('data:image',out.read_text())
            self.assertNotIn(str(entry),json.dumps(self.received))
            self.assertEqual((entry/'00.png').read_bytes(),data)
            self.assertEqual(len(self.received),1)


class ResponseValidationTests(unittest.TestCase):
    def test_tls_alias_and_response_bound(self):
        with patch.object(api.http.client,'HTTPSConnection') as tls:
            client=api.Client('https://example.test/v1','Exact')
            tls.assert_called_once_with('example.test',443,timeout=120)
            tls.return_value.getresponse.return_value.read.return_value=b'x'*65537
            with self.assertRaisesRegex(ValueError,'64KiB'): client.chat(api.chat_body('Exact','s','p'))
            tls.return_value.close.assert_called_once()

    def test_duplicate_nonfinite_and_empty_answers_are_rejected(self):
        for raw in ('{"x":1,"x":2}','{"x":NaN}','{"x":1e999}'):
            with self.assertRaises(ValueError): api.loads(raw)
        for answer in ('',None,[]):
            with self.assertRaises(ValueError): api.completion({'choices':[{'message':{'content':answer}}]})
        for invalid in (0,True,16777217):
            with self.assertRaises(ValueError): api.chat_body('qwen','s',[],processor_kwargs={'max_pixels':invalid})


if __name__=='__main__': unittest.main()

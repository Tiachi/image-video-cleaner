"""Synthetic transport checks; never contact a real model or remote endpoint."""
import http.server
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch, MagicMock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import text_qa_clean as text
import media_qa_clean as media


class TransportTests(unittest.TestCase):
    def judge(self, cls, base, alias='qwen3.8-27b'):
        # Transport only: upstream lineage is covered by the pipeline tests.
        judge=object.__new__(cls)
        judge.config=dict(text.DEFAULT,api_base=base,model=alias)
        judge.prompt='Synthetic quality check'
        judge.local=threading.local(); judge.lock=threading.Lock(); judge.clients=[]
        self.addCleanup(lambda:[c.close() for c in judge.clients])
        return judge

    def test_text_and_visual_requests_work_with_url_and_exact_alias(self):
        received=[]
        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version='HTTP/1.1'
            def do_POST(self):
                body=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                received.append((self.path,body,self.headers.get('Authorization')))
                data=json.dumps({'choices':[{'message':{'content':'8,1,0,0,1,2'}}],
                                 'usage':{'completion_tokens':11}}).encode()
                self.send_response(200); self.send_header('Content-Length',str(len(data)))
                self.end_headers(); self.wfile.write(data)
            def log_message(self,*args):pass
        server=http.server.ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread=threading.Thread(target=server.serve_forever,daemon=True); thread.start()
        try:
            base='http://127.0.0.1:'+str(server.server_port)+'/v1/'
            # http.client directly connects even if inherited proxy env is invalid.
            with patch.dict(os.environ,{'HTTP_PROXY':'http://invalid.invalid:1','HTTPS_PROXY':'http://invalid.invalid:1'}):
                for cls,payload in ((text.Judge,'Synthetic question and answer'),
                                    (media.Judge,[{'type':'text','text':'Synthetic image QA'},
                                                  {'type':'image_url','image_url':{'url':'data:image/png;base64,AA=='}}])):
                    judge=self.judge(cls,base,'qwen3.8-27b')
                    answer,usage=judge.request(payload)
                    self.assertEqual(answer,'8,1,0,0,1,2')
                    self.assertEqual(usage['completion_tokens'],11)
                    judge.request(payload)
                    self.assertEqual(len(judge.clients),1)  # keepalive reuse
                    for client in judge.clients:client.close()
            self.assertEqual(len(received),4)
            for path,body,authorization in received:
                self.assertEqual(path,'/v1/chat/completions')
                self.assertEqual(body['model'],'qwen3.8-27b')
                self.assertFalse(body['chat_template_kwargs']['enable_thinking'])
                self.assertIsNone(authorization)
            self.assertIsInstance(received[2][1]['messages'][1]['content'],list)
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=2)

    def test_https_uses_tls_connection_and_preserves_case(self):
        connection=MagicMock()
        response=connection.getresponse.return_value
        response.status=200
        response.read.return_value=b'{"choices":[{"message":{"content":"8,1,1,0"}}]}'
        with patch.object(text.http.client,'HTTPSConnection',return_value=connection) as tls, \
             patch.object(text.http.client,'HTTPConnection') as plain:
            judge=self.judge(text.Judge,'https://api.example.test/v1','My-Qwen-Alias')
            judge.request('Synthetic')
            tls.assert_called_once_with('api.example.test',443,timeout=120)
            plain.assert_not_called()
            body=json.loads(connection.request.call_args.args[2])
            self.assertEqual(body['model'],'My-Qwen-Alias')

    def test_http_error_is_not_redirected_or_replayed(self):
        for status in (301,401,500):
            connection=MagicMock(); response=connection.getresponse.return_value
            response.status=status; response.read.return_value=b'{}'
            with patch.object(text.http.client,'HTTPConnection',return_value=connection):
                judge=self.judge(text.Judge,'http://192.0.2.1:8018/v1')
                with self.assertRaisesRegex(ValueError,'HTTP status'):
                    judge.request('Synthetic')
                connection.request.assert_called_once()
                connection.close.assert_called_once()
                self.assertIsNone(judge.local.client)
                self.assertEqual(judge.clients,[])


if __name__=='__main__':unittest.main()

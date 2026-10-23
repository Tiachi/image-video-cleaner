from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import media_checks
import mechanical_clean as core
import media_qa_clean
import text_qa_clean
import vllm_service as service


class ScopeTests(unittest.TestCase):
    def test_only_explicit_visual_media_are_references(self):
        refs=media_checks.references({'images':['one.png'],'audios':['../../unrelated.wav'],
                                      'audio_path':{'unsupported':'ignored'}})
        self.assertEqual(refs,[{'kind':'image','path':'one.png','start':None,'end':None}])
        with self.assertRaises(ValueError): media_checks.references({'audios':['one.mp4']})

    def test_qwen_alias_is_shared_by_both_judges_and_service(self):
        self.assertEqual(text_qa_clean.DEFAULT['model'],'Qwen3.8-27B')
        self.assertEqual(media_qa_clean.DEFAULT['model'],service.DEFAULT['served_model_name'])

    def test_entrypoints_import_without_parent_dataset_checkout(self):
        with tempfile.TemporaryDirectory() as temp:
            for name in ('mechanical_clean.py','text_qa_clean.py','media_qa_clean.py','vllm_service.py'):
                r=subprocess.run([sys.executable,'-B',str(core.REPO/'scripts'/name),'--help'],
                                 cwd=temp,text=True,capture_output=True,timeout=10)
                self.assertEqual(r.returncode,0,r.stderr)

    def test_config_examples_track_actual_defaults(self):
        for filename,expected in (('stage01.example.json',core.DEFAULT),
                ('stage02.example.json',text_qa_clean.DEFAULT),('stage03.example.json',media_qa_clean.DEFAULT),
                ('vllm.example.json',service.DEFAULT)):
            self.assertEqual(json.loads((core.REPO/'configs'/filename).read_bytes()),expected)


class ServiceTests(unittest.TestCase):
    def test_sixteen_card_visual_command_is_not_language_only(self):
        c=deepcopy(service.DEFAULT); c['model_path']='/model/local-qwen'
        cmd=service.command(c)
        self.assertNotIn('--language-model-only',cmd)
        limit=json.loads(cmd[cmd.index('--limit-mm-per-prompt')+1])
        self.assertEqual(limit,{'image':32,'video':0})
        self.assertEqual(cmd[cmd.index('--tensor-parallel-size')+1],'2')
        self.assertEqual(cmd[cmd.index('--data-parallel-size')+1],'8')
        self.assertIn('--enable-prefix-caching',cmd); self.assertIn('--async-scheduling',cmd)
        self.assertEqual(json.loads(cmd[cmd.index('--mm-processor-kwargs')+1]),
                         media_qa_clean.DEFAULT['vision_processor_kwargs'])

    def test_memory_gate_refuses_occupied_card_without_stopping_it(self):
        class NPU:
            def device_count(self): return 16
            def mem_get_info(self,index): return (10 if index==9 else 100,100)
        with self.assertRaisesRegex(ValueError,'occupied'): service.memory_gate(NPU(),service.DEFAULT)

    def test_memory_gate_uses_direct_npu_api(self):
        class NPU:
            def device_count(self): return 16
            def mem_get_info(self,index): return (95,100)
        self.assertEqual(len(service.memory_gate(NPU(),service.DEFAULT)),16)


if __name__=='__main__': unittest.main()

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import pipeline_config as p


class UnifiedConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.base=Path(self.temp.name)
        self.c=deepcopy(p.DEFAULT)
        self.c['paths'].update(source=str(self.base/'input/source.jsonl'),media_root=str(self.base/'media'),
                               work_root=str(self.base/'work'))

    def test_stage_chain_is_derived_from_formal_deliveries(self):
        r=p.resolve(self.c)
        self.assertEqual(Path(r['stage02']['stage1_delivery']),self.base/'work/stage01/full/delivery')
        self.assertEqual(Path(r['stage03']['stage2_delivery']),self.base/'work/stage02/full/delivery')
        self.assertEqual(r['stage01']['allowed_media_roots'],[str(self.base/'media')])
        self.assertEqual(Path(r['stage03']['frame_cache_root']),self.base/'work/frame_cache')
        for stage,default in (('stage01',p.core.DEFAULT),('stage02',p.text_stage.DEFAULT),
                              ('stage03',p.media_stage.DEFAULT),('vllm',p.service.DEFAULT)):
            self.assertEqual(set(r[stage]),set(default))

    def test_old_config_without_text_v2_gets_new_defaults(self):
        c=deepcopy(self.c); del c['stage01']['text_v2']
        r=p.resolve(c)
        self.assertEqual(r['stage01']['text_v2'],p.DEFAULT['stage01']['text_v2'])

    def test_explicit_paths_control_downstream_inputs_and_cache(self):
        self.c['paths'].update(stage01_output=str(self.base/'custom-one'),stage02_output=str(self.base/'custom-two'),
                               stage03_output=str(self.base/'custom-three'),frame_cache_root=str(self.base/'frames'),
                               vllm_runtime_root=str(self.base/'service'))
        r=p.resolve(self.c)
        self.assertEqual(Path(r['stage02']['stage1_delivery']),self.base/'custom-one/full/delivery')
        self.assertEqual(Path(r['stage03']['stage2_delivery']),self.base/'custom-two/full/delivery')
        self.assertEqual(Path(r['stage03']['output']),self.base/'custom-three')
        self.assertEqual(Path(r['vllm']['runtime_root']),self.base/'service')

    def test_api_model_and_vision_budget_have_one_authority(self):
        self.c['api'].update(api_base='http://127.0.0.1:8123/v1',model='my-qwen',model_revision='frozen-v1')
        self.c['stage03']['vision_processor_kwargs']['max_pixels']=524288
        self.c['stage03']['max_images_per_request']=16
        r=p.resolve(self.c)
        for stage in ('stage02','stage03'):
            self.assertEqual(r[stage]['model'],'my-qwen'); self.assertEqual(r[stage]['api_base'],self.c['api']['api_base'])
            self.assertEqual(r[stage]['model_revision'],'frozen-v1')
        self.assertEqual(r['vllm']['port'],8123); self.assertEqual(r['vllm']['served_model_name'],'my-qwen')
        self.assertEqual(r['vllm']['vision_max_pixels'],524288); self.assertEqual(r['vllm']['max_images_per_prompt'],16)

    def test_stage_specific_limits_and_tools_are_not_lost(self):
        self.c['stage01']['clarity']['image_threshold']=8
        self.c['stage02']['minimum_score']=7; self.c['stage03']['minimum_score']=8
        self.c['tools'].update(ffmpeg='/tools/ffmpeg',ffprobe='/tools/ffprobe')
        r=p.resolve(self.c)
        self.assertEqual(r['stage01']['clarity']['image_threshold'],8)
        self.assertEqual(r['stage02']['minimum_score'],7); self.assertEqual(r['stage03']['minimum_score'],8)
        self.assertEqual(r['stage01']['ffmpeg'],r['stage03']['ffmpeg'])

    def test_resolve_is_read_only_and_does_not_require_future_deliveries(self):
        before=deepcopy(self.c); r=p.resolve(self.c)
        self.assertEqual(self.c,before)
        self.assertFalse(Path(r['snapshot_root']).exists())
        self.assertFalse((self.base/'input').exists()); self.assertFalse((self.base/'media').exists())

    def test_overlapping_media_output_or_cache_is_refused(self):
        for change in ({'work_root':str(self.base/'media/out')},
                       {'frame_cache_root':str(self.base/'media/cache')},
                       {'stage02_output':str(self.base/'work/stage01/nested')},
                       {'frame_cache_root':str(self.base/'work/config_snapshots')},
                       {'scratch_root':str(self.base/'work/stage03')}):
            c=deepcopy(self.c); c['paths'].update(change)
            with self.assertRaises(ValueError): p.resolve(c)

    def test_unknown_options_and_unsafe_api_do_not_silently_disappear(self):
        c=deepcopy(self.c); c['paths']['stage_output_typo']='anything'
        with self.assertRaises(ValueError):p.resolve(c)
        c=deepcopy(self.c); c['stage02']['model']='duplicate authority'
        with self.assertRaises(ValueError):p.resolve(c)
        c=deepcopy(self.c); c['api']['api_base']='http://user:secret@localhost:8018/v1'
        with self.assertRaises(ValueError):p.resolve(c)

    def test_snapshots_are_idempotent_bound_and_no_clobber(self):
        r=p.resolve(self.c); first=p.snapshot(self.c,r,'stage01')
        self.assertEqual(first,p.snapshot(self.c,r,'stage01'))
        self.assertEqual(json.loads(first.read_bytes()),r['stage01'])
        first.write_text('{}',encoding='utf-8')
        with self.assertRaises(ValueError):p.snapshot(self.c,r,'stage01')

    def test_changes_get_new_snapshot_directory_and_preserve_old(self):
        first=p.snapshot(self.c,p.resolve(self.c),'stage02'); original=first.read_bytes()
        self.c['stage02']['minimum_score']=8
        second=p.snapshot(self.c,p.resolve(self.c),'stage02')
        self.assertNotEqual(first.parent,second.parent); self.assertEqual(first.read_bytes(),original)

    def test_child_commands_use_current_or_separate_model_python(self):
        self.c['api']['service_mode']='managed'
        r=p.resolve(self.c); r['vllm_python']=sys.executable
        cmd=p.child_command(r,'stage03','cache',self.base/'one.json',100)
        self.assertEqual(cmd[-2:],['--rows','100']); self.assertIn('cache',cmd)
        model_cmd=p.child_command(r,'vllm','print',self.base/'v.json')
        self.assertEqual(Path(model_cmd[0]),Path(sys.executable))
        self.assertIn('vllm_service.py',model_cmd[3])
        with self.assertRaises(ValueError):p.child_command(r,'stage02','run',self.base/'one.json',100)

    def test_existing_api_accepts_just_url_and_model_and_no_vllm_section(self):
        self.c['api']={'api_base':'http://192.0.2.1:8123/v1','model':'qwen3.8-27b'}
        del self.c['vllm']
        r=p.resolve(self.c)
        self.assertEqual(r['service_mode'],'existing')
        for stage in ('stage02','stage03'):
            self.assertEqual(r[stage]['model'],'qwen3.8-27b')
            self.assertIsNone(r[stage]['model_revision'])
            self.assertNotIn('service_mode',r[stage])
        self.assertNotIn('model_revision',self.c['api'])
        self.assertFalse((self.base/'work').exists())

    def test_existing_mode_ignores_unused_local_model_path(self):
        self.c['vllm']['model_path']='not a local path; existing server owns weights'
        r=p.resolve(self.c)
        self.assertIsNone(r['vllm']['model_path'])
        for phase in ('print','serve'):
            with self.assertRaisesRegex(ValueError,'existing API mode'):
                p.child_command(r,'vllm',phase,self.base/'v.json')

    def test_managed_launch_cannot_target_remote_or_https_service(self):
        self.c['api']['service_mode']='managed'
        for base in ('http://192.0.2.1:8018/v1','https://localhost:8018/v1'):
            self.c['api']['api_base']=base
            with self.assertRaisesRegex(ValueError,'managed service binds'):
                p.child_command(p.resolve(self.c),'vllm','serve',self.base/'v.json')
        self.c['api']['service_mode']='typo'
        with self.assertRaises(ValueError):p.resolve(self.c)

    def test_show_cli_does_not_launch_or_create_stage_outputs(self):
        config=self.base/'pipeline.local.json'; config.write_text(json.dumps(self.c),encoding='utf-8')
        run=subprocess.run([sys.executable,'-B',str(p.core.REPO/'scripts/pipeline_config.py'),'show','--config',str(config)],
                           cwd=self.base,text=True,capture_output=True,timeout=10)
        self.assertEqual(run.returncode,0,run.stderr); self.assertIn('RESOLVED',run.stdout)
        self.assertFalse((self.base/'work').exists())

    def test_public_template_matches_default(self):
        self.assertEqual(json.loads((p.core.REPO/'configs/pipeline.example.json').read_bytes()),p.DEFAULT)

    def test_dispatch_writes_snapshot_and_forwards_phase_and_rows(self):
        config=self.base/'pipeline.local.json'; config.write_text(json.dumps(self.c),encoding='utf-8')
        with patch.object(sys,'argv',['pipeline_config.py','stage01','--config',str(config),'--phase','pilot','--rows','7']), \
             patch.object(p.subprocess,'run',return_value=SimpleNamespace(returncode=0)) as run:
            p.main()
        command=run.call_args.args[0]
        self.assertEqual(command[-2:],['--rows','7'])
        effective=Path(command[command.index('--config')+1])
        self.assertTrue(effective.is_file())
        self.assertEqual(json.loads(effective.read_bytes())['source'],self.c['paths']['source'])
        self.assertFalse((self.base/'work/stage01').exists())

    def test_failed_child_is_not_reported_as_success(self):
        config=self.base/'pipeline.local.json'; config.write_text(json.dumps(self.c),encoding='utf-8')
        with patch.object(sys,'argv',['pipeline_config.py','stage01','--config',str(config)]), \
             patch.object(p.subprocess,'run',return_value=SimpleNamespace(returncode=2)):
            with self.assertRaisesRegex(ValueError,'failed with status 2'):p.main()

    def test_nonfinite_or_duplicate_config_keys_fail(self):
        path=self.base/'bad.json'
        for raw in ('{"a":NaN}','{"a":1,"a":2}'):
            path.write_text(raw,encoding='utf-8')
            with self.assertRaises(ValueError):p.load(path)


if __name__=='__main__':unittest.main()

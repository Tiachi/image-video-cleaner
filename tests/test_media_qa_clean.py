from copy import deepcopy
import base64
import hashlib
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import media_qa_clean as s
import frame_cache as fc
from media_contract import parse, issues_for
from test_text_qa_clean import FakeMedia, FakeJudge as TextJudge, row
core = s.core
PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR'+struct.pack('>II', 2, 2)


class ContractTests(unittest.TestCase):
    def test_visual_match_and_explicit_threshold(self):
        label = parse('8,1,1,2', 2)
        self.assertEqual(issues_for(label, None), [])
        self.assertEqual(core.decision(issues_for(label, 9)), 'rejected')

    def test_missing_event_is_not_auto_rejected(self):
        for response in ('9,2,0,0', '3,2,1,0'):
            self.assertEqual(core.decision(issues_for(parse(response, 2), 8)), 'review')

    def test_positive_contradiction_rejects(self):
        self.assertEqual(core.decision(issues_for(parse('3,0,2,0', 2), None)), 'rejected')

    def test_invalid_evidence_and_label_conflicts_fail_closed(self):
        for text in ('8,0,1,0', '9,1,0,0', '3,0,0,0', '8,1,3,0', '8,1,1,1', '8,1,0,2', '9,3,0,0',
                     '0,1,1,0', '11,1,1,0', '8,4,1,0', '8,1,1,0 explanation', '{"score":8}'):
            with self.assertRaises(ValueError): parse(text, 2)


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.media = self.base/'media'; self.media.mkdir()
        self.path = self.media/'one.mp4'; self.path.write_bytes(b'fake video')
        self.c = deepcopy(s.DEFAULT)
        self.c.update(media_root=str(self.media), allowed_media_roots=[str(self.media)], workers=1,
                      frame_cache_root=str(self.base/'frames'), frames_per_video=2)
        self.runtime = {'ffmpeg':'ffmpeg', 'ffprobe':'ffprobe', 'ffmpeg_sha256':'v1', 'ffprobe_sha256':'v1'}
        self.cache = fc.FrameCache(self.c, self.runtime); self.addCleanup(self.cache.close)
        self.metadata = patch.object(fc, 'metadata', return_value={'width':2,'height':2,'duration':100,'pts_origin':7})
        self.metadata.start(); self.addCleanup(self.metadata.stop)
        self.calls = []
        def extract(path, target, destination, runtime, timeout):
            self.calls.append(target); destination.write_bytes(PNG)
            return 7+(target or 0)
        self.extract = patch.object(fc, 'extract', side_effect=extract)
        self.extract.start(); self.addCleanup(self.extract.stop)
        self.ref = {'kind':'video','path':'one.mp4','start':10,'end':20}

    def test_actual_clip_midpoints_and_cache_reuse(self):
        first = self.cache.get(self.ref); second = self.cache.get(self.ref)
        self.assertEqual(self.calls, [12.5,17.5])
        self.assertFalse(first['cache_hit']); self.assertTrue(second['cache_hit'])
        self.assertEqual([f['relative_pts_sec'] for f in first['frames']], [12.5,17.5])
        self.assertEqual(self.path.read_bytes(), b'fake video')
        self.assertLess(self.cache.allocated, 65536)

    def test_clip_stat_and_binary_identity_do_not_collide(self):
        first = self.cache.get(self.ref)
        changed = self.cache.get(dict(self.ref, end=30))
        self.path.write_bytes(b'new fake video')
        replaced = self.cache.get(self.ref)
        self.runtime['ffmpeg_sha256'] = 'v2'
        binary = self.cache.get(self.ref)
        self.assertEqual(len({x['cache_key'] for x in (first,changed,replaced,binary)}),4)

    def test_corrupt_cache_never_decodes_or_overwrites(self):
        first = self.cache.get(self.ref); frame = Path(first['directory'])/'00.png'
        frame.write_bytes(b'corrupt')
        with self.assertRaises(fc.CacheIntegrityError): self.cache.get(self.ref)
        self.assertEqual(len(self.calls),2); self.assertEqual(frame.read_bytes(),b'corrupt')

    def test_capacity_stops_before_decode(self):
        self.cache.limit = 1
        with self.assertRaises(fc.CacheCapacityError): self.cache.get(self.ref)
        self.assertEqual(self.calls,[])

    def test_cache_only_missing_never_decodes(self):
        self.c['frame_cache_mode']='require_hit'
        with self.assertRaises(fc.CacheMissError): self.cache.get(self.ref)
        self.assertEqual(self.calls,[])

    def test_cache_only_reuses_prepared_identity(self):
        before=self.cache.get(self.ref); self.c['frame_cache_mode']='require_hit'
        after=self.cache.get(self.ref)
        self.assertEqual(before['cache_key'],after['cache_key']); self.assertTrue(after['cache_hit'])
        self.assertEqual(len(self.calls),2)

    def test_dataset_namespace_isolates_same_media(self):
        before=self.cache.get(self.ref); self.c['cache_namespace']='another-dataset'
        after=self.cache.get(self.ref)
        self.assertNotEqual(before['directory'],after['directory'])
        self.assertIn('another-dataset',after['directory'])

    def test_timestamp_manifest_tamper_is_detected(self):
        first = self.cache.get(self.ref)
        manifest = Path(first['directory'])/'manifest.json'
        value = json.loads(manifest.read_bytes()); value['frames'][0]['relative_pts_sec'] = 99
        manifest.write_bytes(core.encoded(value))
        with self.assertRaises(fc.CacheIntegrityError): self.cache.get(self.ref)
        self.assertEqual(len(self.calls),2)

    def test_outside_clip_or_duplicate_timestamp_cannot_be_used(self):
        self.extract.stop()
        def bad(path,target,destination,runtime,timeout):
            destination.write_bytes(PNG); return 7
        with patch.object(fc,'extract',side_effect=bad):
            with self.assertRaises(ValueError): self.cache.get(self.ref)
        self.assertGreater(self.cache.allocated,0)  # Failed extraction remains charged, no silent cleanup.

    def test_pixel_budget_checks_before_extraction(self):
        self.c['max_frame_pixels'] = 1
        with self.assertRaises(ValueError): self.cache.get(self.ref)
        self.assertEqual(self.calls,[])

    def test_frame_count_checked_before_extra_decoding(self):
        frames = self.cache.frames([self.ref])
        self.assertEqual([f['frame_id'] for f in frames],[1,2])
        self.c['max_images_per_request'] = 1
        with self.assertRaises(ValueError): self.cache.frames([self.ref])
        self.assertEqual(len(self.calls),2)

    def test_extract_command_native_png_and_decoded_pts(self):
        self.extract.stop()
        with patch.object(fc.blur,'run_command',return_value=('', '[Parsed_showinfo_0] n:   0 pts: 550 pts_time:12.5 duration:1')) as run:
            self.assertEqual(fc.extract(self.path,12.5,self.base/'out.png',self.runtime,10),12.5)
        command = run.call_args.args[0]
        self.assertIn('-an',command); self.assertIn('-n',command); self.assertIn('showinfo',command)
        self.assertNotIn('scale', ' '.join(command)); self.assertTrue(run.call_args.kwargs['include_stderr'])


class FakeFrames:
    calls = 0
    failure = None
    def __init__(self,c,runtime):
        self.path = Path(c['frame_cache_root'])/'one.png'
        self.path.parent.mkdir(parents=True,exist_ok=True); self.path.write_bytes(PNG)
    def frames(self,refs):
        type(self).calls += 1
        if self.failure: raise self.failure
        return [{'frame_id':1,'media_index':0,'relative_pts_sec':4,'clip_start':0,'clip_end':8,
                 'path':str(self.path),'file':'00.png','cache_key':'synthetic','cache_hit':True,
                 'bytes':len(PNG),'sha256':hashlib.sha256(PNG).hexdigest()}]
    def close(self): pass


class VisualJudge(s.Judge):
    payloads = []
    def request(self,payload):
        self.payloads.append(payload)
        value = json.loads(payload[-1]['text'])
        answer = value['answer']
        if answer == 'contradiction': return '3,0,1,0',{}
        if answer == 'unseen event': return '9,2,0,0',{}
        if answer == 'bad evidence': return '8,1,2,0',{}
        return '8,1,1,0',{'prompt_tokens':12,'completion_tokens':7}


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name); self.addCleanup(self.tmp.cleanup)
        self.src = self.base/'source.jsonl'
        self.c1 = deepcopy(core.DEFAULT)
        self.c1.update(source=str(self.src),media_root=str(self.base/'media'),allowed_media_roots=[str(self.base/'media')],
                       output=str(self.base/'stage01'),workers=1,part_rows=2,scratch_limit_mb=16)
        self.c2 = deepcopy(s.text_stage.DEFAULT)
        self.c2.update(stage1_delivery=str(self.base/'stage01/full/delivery'),output=str(self.base/'stage02'),
                       model='synthetic',model_revision='test',workers=1,part_rows=2,scratch_limit_mb=16)
        self.c3 = deepcopy(s.DEFAULT)
        self.c3.update(stage2_delivery=str(self.base/'stage02/full/delivery'),output=str(self.base/'stage03'),
                       frame_cache_root=str(self.base/'frames'),model='synthetic',model_revision='test',workers=1,
                       part_rows=2,scratch_limit_mb=16)
        self.frame_patch = patch.object(s,'FrameCache',FakeFrames); self.frame_patch.start(); self.addCleanup(self.frame_patch.stop)
        VisualJudge.payloads = []; FakeFrames.calls=0; FakeFrames.failure=None

    def prepare(self,rows):
        self.src.write_bytes(b''.join(core.encoded(r) for r in rows))
        core.run(self.c1,self.base/'stage01/full',{'test':True},inspector_class=FakeMedia,code_identity={'test':'one'})
        p = self.base/'config02.json'; p.write_bytes(core.encoded(self.c2))
        with patch.object(core,'REPO',self.base/'code'):
            c2 = s.text_stage.config(p)
        core.run(c2,self.base/'stage02/full',{'test':True},None,TextJudge,{'test':'two'},s.text_stage.analyse,
                 s.text_stage.summary_builder(c2),s.emit,s.text_stage.write_attributes,allow_empty=True)
        p = self.base/'config03.json'; p.write_bytes(core.encoded(self.c3))
        with patch.object(core,'REPO',self.base/'code'):
            return s.config(p)

    def run_stage3(self,c):
        return core.run(c,self.base/'stage03/full',{'test':True},None,VisualJudge,{'test':'three'},s.analyse,
                        s.summary_builder(c),s.emit,s.write_attributes,allow_empty=True)

    def test_three_stage_lineage_counts_labels_and_original_bytes(self):
        empty = row('Red.','empty'); empty['conversations'][0]['value']='<image> '
        rows = [empty,row('bad answer','bad'),row('suitable','good'),row('contradiction','wrong'),
                row('unseen event','unseen')]
        c = self.prepare(rows); result = self.run_stage3(c)
        self.assertEqual(result['rows'],3); self.assertEqual(result['counts'],{'accepted':1,'rejected':1,'review':1})
        self.assertEqual(result['qa_distributions']['media_alignment'],{'0':1,'1':1,'2':1})
        self.assertEqual(result['cumulative_retained_fraction_rows'],1/5)
        self.assertEqual(result['model_calls'],3)
        delivery = self.base/'stage03/full/delivery'; core.check_part(delivery)
        self.assertEqual((delivery/'accepted.jsonl').read_bytes(),core.encoded(rows[2]))
        attrs = [json.loads(raw) for raw in (delivery/'attributes.jsonl').read_bytes().splitlines()]
        self.assertEqual([a['original_source_row'] for a in attrs],[2,3,4])
        self.assertEqual(attrs[0]['text_labels']['cot_class'],2); self.assertTrue(attrs[0]['cot_candidate'])
        self.assertEqual(attrs[0]['text_labels']['qa_type'],0)
        self.assertEqual(attrs[0]['text_labels']['option_count'],0)
        self.assertEqual(attrs[0]['qa_structure']['qa_type'],0)
        self.assertEqual(attrs[0]['evidence'][0]['relative_pts_sec'],4)
        self.assertNotIn('private/media',json.dumps(VisualJudge.payloads))
        self.assertNotIn('text_labels',json.dumps(VisualJudge.payloads))
        self.assertEqual(VisualJudge.payloads[0][1]['image_url']['url'],'data:image/png;base64,'+base64.b64encode(PNG).decode())
        self.run_stage3(c); self.assertEqual(len(VisualJudge.payloads),3)

    def test_multiturn_previous_context_but_no_later_answer_leakage(self):
        c = self.prepare([row('Red.','multi',extra=True)])
        result = self.run_stage3(c)
        self.assertEqual(result['qa_counts']['accepted'],2)
        first = json.loads(VisualJudge.payloads[0][-1]['text']); second=json.loads(VisualJudge.payloads[1][-1]['text'])
        self.assertEqual(first['previous_qa'],[])
        self.assertNotIn('suitable',json.dumps(first))
        self.assertEqual(second['previous_qa'][0]['answer'],'Red.')
        self.assertEqual(FakeFrames.calls,1)

    def test_bad_evidence_is_review_no_retry(self):
        result=self.run_stage3(self.prepare([row('bad evidence')]))
        self.assertEqual(result['counts'],{'review':1}); self.assertEqual(len(VisualJudge.payloads),1)

    def test_frame_error_reviews_without_model_call(self):
        c=self.prepare([row()]); FakeFrames.failure=ValueError('short clip, no distinct frames')
        result=self.run_stage3(c)
        self.assertEqual(result['counts'],{'review':1}); self.assertEqual(result['model_calls'],0)
        self.assertEqual(VisualJudge.payloads,[])

    def test_oversized_text_is_reviewed_without_truncation_or_request(self):
        c=self.prepare([row()]); c['max_input_chars']=100
        result=self.run_stage3(c)
        self.assertEqual(result['counts'],{'review':1}); self.assertEqual(result['model_calls'],0)
        self.assertEqual(result['model_status_counts'],{'input_too_long':1})
        self.assertEqual(VisualJudge.payloads,[])

    def test_cache_capacity_is_fatal_not_mass_review(self):
        c=self.prepare([row()]); FakeFrames.failure=fc.CacheCapacityError('full')
        with self.assertRaises(fc.CacheCapacityError): self.run_stage3(c)
        self.assertFalse((self.base/'stage03/full/delivery').exists())

    def test_cpu_precache_has_no_model_request_or_quality_labels(self):
        c=self.prepare([row()])
        result=core.run(c,self.base/'stage03/cache-full',{'test':True},None,VisualJudge,{'test':'cache'},
                        s.analyse_cache,s.cache_summary(c),s.emit,allow_empty=True)
        self.assertEqual(result['status'],'FRAME_CACHE_PREPARED'); self.assertEqual(result['model_calls'],0)
        self.assertEqual(VisualJudge.payloads,[]); self.assertNotIn('qa_distributions',result)
        self.assertFalse((self.base/'stage03/cache-full/delivery/attributes.jsonl').exists())

    def test_parent_tampering_detected_before_model(self):
        c=self.prepare([row()])
        with (self.base/'stage02/full/delivery/audit.jsonl').open('ab') as out: out.write(b'\n')
        with self.assertRaises((ValueError,json.JSONDecodeError)): self.run_stage3(c)
        self.assertEqual(VisualJudge.payloads,[])

    def test_existing_visual_api_accepts_unknown_revision(self):
        self.c3.update(api_base='http://192.0.2.1:8018/v1',model='qwen3.8-27b',model_revision=None)
        c=self.prepare([row()])
        self.assertIsNone(c['model_revision'])
        self.assertEqual(self.run_stage3(c)['counts'],{'accepted':1})

    def test_config_refuses_invalid_api_and_overlapping_output(self):
        self.prepare([row()]); p=self.base/'bad.json'
        for changes in ({'api_base':'https://example.test/not-v1'}, {'output':str(self.base/'media/derived')},
                        {'output':str(self.base/'stage02/full/new')}, {'frame_cache_root':str(self.base/'stage03/cache')},
                        {'minimum_score':True}):
            c=dict(self.c3,**changes); p.write_bytes(core.encoded(c))
            with patch.object(core,'REPO',self.base/'code'):
                with self.assertRaises(ValueError): s.config(p)

    def test_empty_upstream_is_not_resurrected(self):
        c=self.prepare([row('bad answer')]); result=self.run_stage3(c)
        self.assertEqual(result['rows'],0); self.assertEqual(result['status'],'EMPTY_UPSTREAM')
        self.assertEqual(VisualJudge.payloads,[])


if __name__ == '__main__': unittest.main()

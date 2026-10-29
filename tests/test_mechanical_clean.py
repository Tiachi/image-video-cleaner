"""Synthetic fixtures only. Never scan user media or call a model."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1]/'scripts'
sys.path.insert(0, str(SCRIPTS))
import mechanical_clean as m
import media_checks as media
from text_checks import (repetition, extract_pairs, check_pairs, datatrove_repetition,
                         language_proxy)


class FakeInspector:
    def __init__(self, *args):
        pass

    def inspect(self, refs):
        return [dict(r, status='ok', is_blurry=False) for r in refs]

    def close(self):
        pass


def row(q='What color is the cup?', a='Red.', path='a.png', ident='x'):
    return {'id': ident, 'images': [path], 'conversations': [
        {'from': 'human', 'value': '<image>\n'+q}, {'from': 'gpt', 'value': a}]}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        (self.base/'media').mkdir()
        self.c = deepcopy(m.DEFAULT)
        self.c.update(source=str(self.base/'source.jsonl'), media_root=str(self.base/'media'),
                      allowed_media_roots=[str(self.base/'media')], output=str(self.base/'output'),
                      workers=1, part_rows=2, scratch_limit_mb=16)
        self.c['clarity'].update(image_threshold=10, video_threshold=10)
        self.runtime = {'ffmpeg': 'test-only', 'ffprobe': 'test-only'}

    def source(self, rows):
        data = b''.join(m.encoded(r) if not isinstance(r, bytes) else r for r in rows)
        Path(self.c['source']).write_bytes(data)
        return data

    def run_fixture(self, inspector=FakeInspector, limit=None):
        return m.run(self.c, self.base/'output', self.runtime, limit, inspector, {'synthetic': 'v1'})


class TextTests(unittest.TestCase):
    def test_chinese_and_english_loops(self):
        self.assertTrue(repetition('red cup sits here '*30)['flagged'])
        self.assertTrue(repetition('这个杯子是红色的。'*30)['flagged'])

    def test_short_answers_not_repetition(self):
        for text in ('A', 'Yes.', '哈哈哈', '2', 'Okay okay'):
            self.assertFalse(repetition(text)['flagged'])

    def test_normal_long_text_not_flagged(self):
        self.assertFalse(repetition(' '.join('word%d' % x for x in range(80)))['flagged'])

    def test_empty_placeholder_and_invisible(self):
        pairs = extract_pairs(row('<image>\\n\u200b', ' \n'))
        issues, _ = check_pairs(pairs, m.DEFAULT['text'])
        self.assertEqual({x['reason'] for x in issues}, {'empty_question', 'empty_answer'})

    def test_empty_multiturn_rejects_whole_row(self):
        r = row(); r['conversations'] += [{'from': 'human', 'value': 'Next?'}, {'from': 'gpt', 'value': ''}]
        issues, _ = check_pairs(extract_pairs(r), m.DEFAULT['text'])
        self.assertEqual(m.decision(issues), 'rejected')
        self.assertEqual(issues[0]['qa_index'], 1)

    def test_pair_adapter_and_role_validation(self):
        self.assertEqual(extract_pairs({'question': '', 'answer': 'a'}), [('', 'a')])
        self.assertEqual(extract_pairs({'conversations': [
            {'role': 'user', 'content': 'q'}, {'role': 'assistant', 'content': 'a'}]}), [('q', 'a')])
        for turns in ([], [{'from': 'human', 'value': 'q'}],
                      [{'from': 'gpt', 'value': 'a'}, {'from': 'human', 'value': 'q'}]):
            with self.assertRaises(ValueError):
                extract_pairs({'conversations': turns})

    def test_repeated_multiturn_pair_reviews(self):
        issues, _ = check_pairs([('q', 'a'), ('q', 'a')], m.DEFAULT['text'])
        self.assertEqual(m.decision(issues), 'review')

    def test_oversized_text_not_silently_truncated_or_accepted(self):
        issues, metrics = check_pairs([('q', 'a'*262145)], m.DEFAULT['text'])
        self.assertEqual(issues[0]['reason'], 'answer_too_large_for_ngram')
        self.assertEqual(metrics[0]['answer']['status'], 'not_computed')

    def test_datatrove_repetition_rule(self):
        self.assertTrue(datatrove_repetition('中文文字'*100, 8, 128, .6)['flagged'])
        self.assertFalse(datatrove_repetition('短答案', 8, 128, .6)['flagged'])
        self.assertFalse(datatrove_repetition(' '.join('word%d' % i for i in range(80)),
                                              8, 128, .6)['flagged'])

    def test_language_proxy(self):
        self.assertEqual(language_proxy('这是中文'), 'zh_or_mixed')
        self.assertEqual(language_proxy('hello world'), 'latin_script')
        self.assertEqual(language_proxy('12345'), 'other_or_short')

    def test_qa_structure_cross_checks(self):
        question = '以下哪项正确？\nA. 甲\nB. 乙\nC. 丙\nD. 丁'
        issues, metrics = check_pairs([(question, 'B')], m.DEFAULT['text'],
                                      m.DEFAULT['text_v2'])
        self.assertEqual(m.decision(issues), 'accepted')
        self.assertEqual(metrics[0]['qa_structure']['qa_type'], 1)
        self.assertEqual(metrics[0]['qa_structure']['option_count'], 4)
        issues, _ = check_pairs([('下列哪项正确？', 'A')], m.DEFAULT['text'],
                                m.DEFAULT['text_v2'])
        self.assertIn('mcq_options_missing', {x['reason'] for x in issues})
        self.assertEqual(m.decision(issues), 'review')
        issues, _ = check_pairs([(question, 'E')], m.DEFAULT['text'],
                                m.DEFAULT['text_v2'])
        self.assertIn('answer_letter_out_of_range', {x['reason'] for x in issues})
        self.assertEqual(m.decision(issues), 'rejected')

    def test_boilerplate_template_refusal_reviews(self):
        issues, _ = check_pairs([('What is shown?', 'As an AI I cannot see the image.')],
                                m.DEFAULT['text'], m.DEFAULT['text_v2'])
        self.assertIn('template_ai_refusal', {x['reason'] for x in issues})


class MediaTests(Fixture):
    def inspector(self):
        i = media.Inspector(self.c, self.runtime, self.base/'cache.sqlite3')
        self.addCleanup(i.close)
        return i

    def ref(self, path='a.png', kind='image', start=None, end=None):
        return dict(path=path, kind=kind, start=start, end=end)

    def test_missing_zero_directory_and_outside(self):
        i = self.inspector()
        self.assertEqual(i.one(self.ref())['reason'], 'media_missing')
        (self.base/'media/a.png').touch()
        self.assertEqual(i.one(self.ref())['reason'], 'media_zero_bytes')
        self.assertEqual(i.one(self.ref('.'))['reason'], 'media_not_regular_file')
        self.assertEqual(i.one(self.ref('../outside'))['reason'], 'unsafe_media_reference')
        self.assertEqual(i.one(self.ref('https://example.test/a.png'))['reason'], 'unsafe_media_reference')

    def test_separate_image_video_thresholds_and_boundary(self):
        (self.base/'media/a.png').write_bytes(b'fake')
        with patch.object(media, 'image_detect', return_value={'status': 'ok', 'mean_blur': 10}):
            out = self.inspector().one(self.ref())
        self.assertEqual(out['status'], 'ok')  # strictly greater than the cutoff is blurry

    def test_uncalibrated_never_clear(self):
        self.c['clarity']['image_threshold'] = None
        (self.base/'media/a.png').write_bytes(b'fake')
        with patch.object(media, 'image_detect', return_value={'status': 'ok', 'mean_blur': .1}):
            out = self.inspector().one(self.ref())
        self.assertEqual(out['reason'], 'clarity_threshold_uncalibrated')
        self.assertNotIn('is_blurry', out)

    def test_bad_probe_review_not_clear_or_permanent_cache(self):
        (self.base/'media/a.png').write_bytes(b'fake')
        i = self.inspector()
        with patch.object(media, 'image_detect', side_effect=RuntimeError('decoder unavailable')) as detect:
            self.assertEqual(i.one(self.ref())['status'], 'review')
            i.one(self.ref())
        self.assertEqual(detect.call_count, 2)

    def test_concurrent_cache_single_probe_and_mutation_invalidation(self):
        p = self.base/'media/a.png'; p.write_bytes(b'fake')
        i = self.inspector()
        def detect(*args):
            time.sleep(.01)
            return {'status': 'ok', 'mean_blur': 3}
        with patch.object(media, 'image_detect', side_effect=detect) as mocked:
            with ThreadPoolExecutor(max_workers=4) as pool:
                results = list(pool.map(i.one, [self.ref()]*8))
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(sum(not r['cache_hit'] for r in results), 1)
            p.write_bytes(b'changed')
            i.one(self.ref())
            self.assertEqual(mocked.call_count, 2)

    def test_media_mutation_mid_decode_aborts(self):
        p = self.base/'media/a.png'; p.write_bytes(b'fake')
        def mutate(*args):
            p.write_bytes(b'changed')
            return {'status': 'ok', 'mean_blur': 1}
        with patch.object(media, 'image_detect', side_effect=mutate), self.assertRaises(RuntimeError):
            self.inspector().one(self.ref())

    def test_video_clip_preserved_in_probe_and_cache(self):
        (self.base/'media/a.mp4').write_bytes(b'fake')
        i = self.inspector()
        with patch.object(media.blur, 'detect', return_value={'status': 'ok', 'mean_blur': 20}) as mocked:
            out = i.one(self.ref('a.mp4', 'video', 10, 20))
            self.assertEqual(mocked.call_args.args[0]['start'], 10)
            self.assertEqual(mocked.call_args.args[0]['end'], 20)
            self.assertEqual(out['reason'], 'blurry')
            i.one(self.ref('a.mp4', 'video', 20, 30))
            self.assertEqual(mocked.call_count, 2)

    def test_partial_frame_detection_is_review(self):
        (self.base/'media/a.mp4').write_bytes(b'fake')
        with patch.object(media.blur, 'detect', return_value={'status': 'error', 'valid_frames': 7}):
            self.assertEqual(self.inspector().one(self.ref('a.mp4', 'video'))['status'], 'review')

    def test_explicit_blur_reject_policy(self):
        self.c['clarity']['action'] = 'reject'
        (self.base/'media/a.png').write_bytes(b'fake')
        with patch.object(media, 'image_detect', return_value={'status': 'ok', 'mean_blur': 20}):
            self.assertEqual(self.inspector().one(self.ref())['status'], 'reject')

    def test_multi_video_bounds_and_alias_conflicts(self):
        for bad in ({'videos': ['a.mp4', 'b.mp4'], 'video_start': 1},
                    {'videos': ['a.mp4'], 'video_path': 'b.mp4'},
                    {'videos': ['a.mp4'], 'video_start': True},
                    {'videos': ['a.mp4'], 'video_start': 5, 'video_end': 2}):
            with self.assertRaises(ValueError):
                media.references(bad)
        out = media.references({'videos': ['a.mp4', 'b.mp4'], 'video_start': [1, 2], 'video_end': [3, 4]})
        self.assertEqual([x['start'] for x in out], [1, 2])

    def test_image_command_no_resize_or_audio_decode(self):
        with patch.object(media.blur, 'run_command', return_value='frame:0 pts:0 pts_time:0\nlavfi.blur=3.5\n') as command:
            self.assertEqual(media.image_detect(Path('a.png'), self.runtime, 3)['mean_blur'], 3.5)
        args = command.call_args.args[0]
        self.assertIn('-an', args)
        self.assertNotIn('-ss', args)
        self.assertNotIn('scale=', ' '.join(args))


class PipelineTests(Fixture):
    def test_routing_raw_bytes_and_conservation(self):
        rows = [row(ident='a'), row('', 'answer', ident='b'), b'{bad json}\n', {'images': ['a.png']}]
        original = self.source(rows)
        result = self.run_fixture()
        self.assertEqual(result['counts'], {'accepted': 1, 'rejected': 2, 'review': 1})
        self.assertEqual(Path(self.c['source']).read_bytes(), original)
        self.assertEqual((self.base/'output/delivery/accepted.jsonl').read_bytes(), m.encoded(rows[0]))
        m.check_part(self.base/'output/delivery')

    def test_templates_across_media_and_different_answers_kept(self):
        self.source([row(path='a.png'), row(path='b.png'), row(a='Blue.', ident='y')])
        self.assertEqual(self.run_fixture()['counts'], {'accepted': 3})

    def test_same_media_pair_duplicate_rejected_in_order(self):
        self.source([row(ident='a'), row(ident='b'), row(ident='c')])
        result = self.run_fixture()
        self.assertEqual(result['counts'], {'accepted': 1, 'rejected': 2})
        audits = [json.loads(s) for s in (self.base/'output/delivery/audit.jsonl').read_bytes().splitlines()]
        self.assertEqual(audits[2]['issues'][0]['first_source_row'], 0)

    def test_clips_and_context_never_collapsed(self):
        a = row(); a.pop('images'); a.update(videos=['a.mp4'], video_start=0, video_end=10)
        b = deepcopy(a); b.update(video_start=10, video_end=20)
        c = deepcopy(a); c['conversations'] = [
            {'from': 'human', 'value': 'Earlier question?'}, {'from': 'gpt', 'value': 'Earlier answer.'}
        ] + c['conversations']
        self.source([a, b, c])
        self.assertEqual(self.run_fixture()['counts'], {'accepted': 3})

    def test_completed_reuse_and_tamper_detection(self):
        self.source([row()]); self.run_fixture()
        class ShouldNotOpen(FakeInspector):
            def __init__(self, *args):
                raise AssertionError('no probe on reuse')
        self.assertEqual(self.run_fixture(ShouldNotOpen)['rows'], 1)
        p = self.base/'output/delivery/accepted.jsonl'
        p.write_bytes(b'x'*p.stat().st_size)
        with self.assertRaisesRegex(ValueError, 'checkpoint changed'):
            self.run_fixture()

    def test_resume_after_export_failure_reconstructs_dedup(self):
        self.source([row(ident='a'), row(ident='b'), row(path='b.png')])
        with patch.object(m, 'publish', side_effect=RuntimeError('synthetic export crash')):
            with self.assertRaises(RuntimeError):
                self.run_fixture()
        out = self.run_fixture()
        self.assertEqual(out['counts'], {'accepted': 2, 'rejected': 1})

    def test_resume_with_duplicate_filter_disabled(self):
        self.c['exact_duplicate'] = False
        self.source([row(ident='a'), row(ident='b')])
        with patch.object(m, 'publish', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.run_fixture()
        self.assertEqual(self.run_fixture()['counts'], {'accepted': 2})

    def test_interrupted_part_does_not_suppress_its_rows(self):
        self.source([row(path='%s.png' % i) for i in range(4)])
        class Interrupted(FakeInspector):
            def inspect(self, refs):
                if refs[0]['path'] == '3.png':
                    raise RuntimeError('synthetic interruption')
                return super().inspect(refs)
        with self.assertRaises(RuntimeError):
            self.run_fixture(Interrupted)
        self.assertEqual(len(list((self.base/'output/parts').glob('part-*'))), 1)
        self.assertEqual(self.run_fixture()['counts'], {'accepted': 4})

    def test_changed_source_or_config_refuses_resume(self):
        self.source([row()]); self.run_fixture()
        self.c['text']['threshold'] = .8
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.run_fixture()

    def test_pilot_is_explicit_prefix(self):
        self.source([row(path='%s.png' % i) for i in range(5)])
        out = self.run_fixture(limit=2)
        self.assertEqual(out['rows'], 2)
        self.assertEqual(out['source_hash_scope'], 'prefix')

    def test_source_change_refuses_completed_reuse(self):
        self.source([row()]); self.run_fixture()
        self.source([row(a='Blue.')])
        with self.assertRaisesRegex(ValueError, 'binding changed'):
            self.run_fixture()

    def test_strict_json_rejects_duplicate_keys_and_nonfinite(self):
        self.source([b'{"question":"q","answer":"a","answer":"b"}\n',
                     b'{"question":"q","answer":"a","x":NaN}\n',
                     b'{"question":"q","answer":"a","x":1e999}\n'])
        self.assertEqual(self.run_fixture()['reason_counts'], {'invalid_json_row': 3})

    def test_scratch_is_disposable_not_sfs_output_sqlite(self):
        self.source([row()]); self.run_fixture()
        self.assertEqual(list((self.base/'output').glob('*.sqlite*')), [])

    def test_checkpoint_corruption_refuses_resume(self):
        self.source([row()])
        with patch.object(m, 'publish', side_effect=RuntimeError('synthetic export crash')):
            with self.assertRaises(RuntimeError):
                self.run_fixture()
        (self.base/'output/parts/part-000000/audit.jsonl').write_bytes(b'{}\n')
        with self.assertRaisesRegex(ValueError, 'checkpoint changed'):
            self.run_fixture()

    def test_repetition_action_is_explicit(self):
        self.source([row(a='the same repeated words '*40)])
        self.assertEqual(self.run_fixture()['counts'], {'review': 1})

    def test_empty_qa_skips_media_io(self):
        self.source([row('', '')])
        class NoMedia(FakeInspector):
            def inspect(self, refs):
                raise AssertionError('unnecessary decode')
        self.assertEqual(self.run_fixture(NoMedia)['counts'], {'rejected': 1})

    def test_config_refuses_unsafe_or_missing_values(self):
        self.source([row()]); p = self.base/'config.json'
        with patch.object(m, 'REPO', self.base/'fake-code'):
            p.write_bytes(m.encoded(self.c)); self.assertEqual(m.read_config(p)['workers'], 1)
            for key, value in (('output', self.c['media_root']), ('workers', 100), ('source', None)):
                bad = deepcopy(self.c); bad[key] = value; p.write_bytes(m.encoded(bad))
                with self.assertRaises(ValueError):
                    m.read_config(p)


if __name__ == '__main__':
    unittest.main()

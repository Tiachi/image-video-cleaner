from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import text_qa_clean as s
from text_contract import parse, issues_for
from qa_structure import detect as detect_structure
core = s.core


class ContractTests(unittest.TestCase):
    def test_all_three_cot_classes(self):
        for value in (0, 1, 2):
            self.assertEqual(parse('8,1,0,0,%d,2,0,0' % value)['cot_class'], value)

    def test_unknown_is_not_fourth_cot_class(self):
        label = parse('8,1,0,0,9,0,0,0')
        self.assertIsNone(label['cot_class'])
        self.assertEqual(core.decision(issues_for(label, None)), 'review')

    def test_quality_score_limits_and_format(self):
        for value in ('0,1,0,0,1,2,0,0', '11,1,0,0,1,2,0,0', '8,1,0,0,3,2,0,0',
                      '8,1,0,0,1,5,0,0', '8,1,0,0,1,2', '8,1,0,0,1,2 explanation',
                      '{"score":8}', '8,1,0,0,1,2,3', '8,1,0,0,1,2,0,0,0',
                      '8,1,0,0,1,2,4,0', '8,1,0,0,1,2,0,11'):
            with self.assertRaises(ValueError):
                parse(value)
        self.assertEqual(parse(' 8, 1, 0, 0, 1, 2, 0, 0 ')['quality_score'], 8)

    def test_reasonable_refusal_not_auto_rejected(self):
        self.assertEqual(issues_for(parse('9,1,2,0,1,1,0,0'), 8), [])

    def test_invalid_refusal_and_obvious_error_reject(self):
        self.assertEqual(core.decision(issues_for(parse('3,0,1,0,1,1,0,0'), None)), 'rejected')
        self.assertEqual(core.decision(issues_for(parse('3,1,0,1,2,1,0,0'), None)), 'rejected')

    def test_contradictory_high_score_reviews(self):
        result = issues_for(parse('9,1,0,1,2,1,0,0'), 8)
        self.assertEqual(result, [{'reason': 'model_label_contradiction', 'action': 'review'}])

    def test_quality_threshold_is_explicit(self):
        label = parse('6,1,0,0,1,2,0,0')
        self.assertEqual(issues_for(label, None), [])
        self.assertEqual(core.decision(issues_for(label, 7)), 'rejected')

    def test_qa_type_and_option_count_contract(self):
        label = parse('9,1,0,0,1,1,1,4')
        self.assertEqual(label['qa_type'], 1)
        self.assertEqual(label['qa_type_name'], 'mcq')
        self.assertEqual(label['option_count'], 4)
        for value in ('9,1,0,0,1,1,4,4', '9,1,0,0,1,1,1,11'):
            with self.assertRaises(ValueError):
                parse(value)

    def test_qa_structure_cross_check_and_answer_letter(self):
        question = 'Which option is shown?\nA. cat\nB. dog\nC. bird\nD. fish'
        structure = detect_structure(question, 'B')
        self.assertEqual((structure['qa_type'], structure['option_count']), (1, 4))
        self.assertEqual(issues_for(parse('9,1,0,0,1,2,1,4'), None, structure), [])
        mismatch = issues_for(parse('9,1,0,0,1,2,0,0'), None, structure)
        self.assertIn('qa_type_mismatch', [issue['reason'] for issue in mismatch])
        bad = issues_for(parse('9,1,0,0,1,2,1,4'), None, detect_structure(question, 'E'))
        self.assertIn('answer_letter_out_of_range', [issue['reason'] for issue in bad])
        self.assertEqual(core.decision(bad), 'rejected')

    def test_qa_type_option_consistency(self):
        for text, reason in (('9,1,0,0,1,2,1,1', 'mcq_option_count_invalid'),
                             ('9,1,0,0,1,2,0,4', 'oe_option_count_invalid'),
                             ('9,1,0,0,1,2,2,4', 'tf_option_count_invalid'),
                             ('9,1,0,0,1,2,3,4', 'unknown_type_option_count_invalid')):
            self.assertIn(reason, [issue['reason'] for issue in issues_for(parse(text), None)])
        self.assertEqual(issues_for(parse('9,1,0,0,1,2,2,2'), None), [])
        self.assertEqual(issues_for(parse('9,1,0,0,1,2,1,2'), None), [])

    def test_question_structure_false_positives(self):
        self.assertEqual(detect_structure(
            'Spider Solitaire is played with eight decks of 13 spade cards each.', '50')['qa_type'], 0)
        self.assertEqual(detect_structure(
            'The area of the shaded part 1 is smaller than part 2.', '18.5')['qa_type'], 0)
        self.assertEqual(detect_structure(
            'The area of the shaded part \u2460 is smaller than \u2461.', '18.5')['qa_type'], 0)
        yesno = detect_structure('Does this meme carry a negative message?', 'No.')
        self.assertEqual((yesno['qa_type'], yesno['option_count']), (2, 2))
        self.assertEqual(detect_structure('Can you tell the time on the wall clock?', '3:00')['qa_type'], 0)
        self.assertEqual(detect_structure(
            'Are there any other things that have the same material?', 'a box')['qa_type'], 0)
        binary = detect_structure('Is the number even or odd?\nA. even\nB. odd', 'A')
        self.assertEqual((binary['qa_type'], binary['option_count']), (1, 2))

    def test_unlabeled_last_option_reviews_not_rejects(self):
        question = 'Which is true?\nA. one\nB. two\nC. three\nfourth line'
        structure = detect_structure(question, 'D')
        self.assertEqual((structure['qa_type'], structure['option_count']), (1, 4))
        self.assertIn('option_labeling_incomplete', [x['reason'] for x in structure['issues']])
        issues = issues_for(parse('9,1,0,0,1,2,1,4'), None, structure)
        self.assertIn('option_labeling_incomplete', [x['reason'] for x in issues])
        self.assertEqual(core.decision(issues), 'review')
        out_of_range = issues_for(parse('9,1,0,0,1,2,1,4'), None,
                                  detect_structure(question, 'E'))
        self.assertEqual(core.decision(out_of_range), 'rejected')

    def test_remote_http_https_and_ipv6_are_supported(self):
        for value, expected in (('https://api.example.test/v1', ('api.example.test',443)),
                                ('http://192.168.1.2:8123/v1/', ('192.168.1.2',8123)),
                                ('http://[::1]:8018/v1', ('::1',8018))):
            self.assertEqual(s.api_location(value),expected)

    def test_malformed_api_refused(self):
        for value in ('http://user:secret@localhost/v1', 'http://localhost/v1?secret=123',
                      'http://localhost/v1#frag','ftp://localhost/v1','http:///v1',
                      'http://localhost:0/v1','http://localhost:65536/v1','http://localhost:abc/v1',
                      'http://localhost/v1//','http://localhost/v1/chat/completions',
                      'http://local\nhost/v1', 'http://localhost\\bad/v1', None):
            with self.assertRaises(ValueError):
                s.api_location(value)

    def test_unknown_model_revision_is_explicit(self):
        self.assertEqual(s.model_identity({'model':'qwen3.8-27b','model_revision':None}), 'endpoint_alias_only')
        for revision in ('',4,False):
            with self.assertRaises(ValueError):s.model_identity({'model':'qwen3.8-27b','model_revision':revision})


class FakeMedia:
    def __init__(self, *args): pass
    def inspect(self, refs): return [dict(x, status='ok') for x in refs]
    def close(self): pass


class FakeJudge(s.Judge):
    payloads = []
    def request(self, payload):
        value = json.loads(payload); self.payloads.append(value)
        if value['answer'] == 'bad answer': return '3,0,0,0,1,2,0,0', {'prompt_tokens': 10, 'completion_tokens': 11}
        if value['answer'] == 'protocol error': return 'bad response', {}
        if value['answer'] == 'suitable': return '8,1,0,0,2,1,0,0', {'prompt_tokens': 10, 'completion_tokens': 11}
        if value['answer'] == 'has reasoning': return '9,1,0,0,0,1,0,0', {}
        return '8,1,0,0,1,2,0,0', {'prompt_tokens': 10, 'completion_tokens': 11}


def row(answer='Red.', ident='one', extra=False):
    r = {'id': ident, 'images': [ident+'.png'], 'secret_metadata_not_for_model': '/private/media/path',
         'conversations': [{'from': 'human', 'value': '<image>\nWhat color?'}, {'from': 'gpt', 'value': answer}]}
    if extra:
        r['conversations'] += [{'from': 'human', 'value': 'And why?'}, {'from': 'gpt', 'value': 'suitable'}]
    return r


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.base = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.src = self.base/'original.jsonl'; self.parent = self.base/'stage01/full'
        self.c1 = deepcopy(core.DEFAULT)
        self.c1.update(source=str(self.src), media_root=str(self.base/'media'),
                       allowed_media_roots=[str(self.base/'media')], output=str(self.base/'stage01'),
                       workers=1, part_rows=2, scratch_limit_mb=16)
        self.c = deepcopy(s.DEFAULT)
        self.c.update(stage1_delivery=str(self.parent/'delivery'), output=str(self.base/'stage02'),
                      model='synthetic', model_revision='synthetic-v1', workers=2, part_rows=2, scratch_limit_mb=16)
        FakeJudge.payloads = []

    def prepare(self, rows):
        self.src.write_bytes(b''.join(core.encoded(x) for x in rows))
        core.run(self.c1, self.parent, {'test': True}, inspector_class=FakeMedia,
                 code_identity={'synthetic': 'stage01'})
        p = self.base/'stage02-config.json'; p.write_bytes(core.encoded(self.c))
        with patch.object(core, 'REPO', self.base/'fake-code-root'):
            return s.config(p)

    def run_stage2(self, c, limit=None):
        return core.run(c, self.base/'stage02/full', {'synthetic': True}, limit, FakeJudge,
                        {'synthetic': 'stage02'}, s.analyse, s.summary_builder(c), s.emit, s.write_attributes,
                        allow_empty=True)

    def test_existing_api_needs_no_model_revision_or_local_runtime(self):
        self.c.update(api_base='http://192.0.2.1:8018/v1',model='qwen3.8-27b',model_revision=None)
        c=self.prepare([row()])
        self.assertIsNone(c['model_revision'])
        self.assertEqual(self.run_stage2(c)['counts'],{'accepted':1})

    def test_two_stage_lineage_outputs_and_no_metadata_in_model_payload(self):
        original = [row('Red.', 'a'), row('bad answer', 'b'), row('suitable', 'c')]
        c = self.prepare(original); result = self.run_stage2(c)
        self.assertEqual(result['counts'], {'accepted': 2, 'rejected': 1})
        self.assertEqual(result['qa_distributions']['cot_class'], {'0': 0, '1': 2, '2': 1, 'unknown': 0})
        self.assertEqual(result['accepted_qa_distributions']['cot_class'], {'0': 0, '1': 1, '2': 1, 'unknown': 0})
        self.assertEqual(result['model_calls'], 3)
        self.assertEqual(result['media_reads'], 0)
        self.assertEqual(result['cot_generated'], 0)
        values = [json.loads(x) for x in (self.base/'stage02/full/delivery/attributes.jsonl').read_bytes().splitlines()]
        self.assertEqual([x['original_source_row'] for x in values], [0, 1, 2])
        self.assertEqual([x['cot_candidate'] for x in values], [False, False, True])
        self.assertNotIn('private/media', json.dumps(FakeJudge.payloads))
        self.assertEqual((self.base/'stage02/full/delivery/accepted.jsonl').read_bytes(),
                         core.encoded(original[0])+core.encoded(original[2]))
        core.check_part(self.base/'stage02/full/delivery')

    def test_empty_stage1_rejection_is_not_resurrected(self):
        missing = row('anything', 'bad'); missing['conversations'][0]['value'] = '<image> '
        c = self.prepare([missing, row('Red.', 'good')]); result = self.run_stage2(c)
        self.assertEqual(result['rows'], 1)
        a = json.loads((self.base/'stage02/full/delivery/audit.jsonl').read_bytes())
        self.assertEqual(a['source_row'], 0); self.assertEqual(a['original_source_row'], 1)

    def test_context_and_multi_qa_statistics(self):
        c = self.prepare([row('Red.', 'multi', extra=True)])
        result = self.run_stage2(c)
        self.assertEqual(result['rows'], 1); self.assertEqual(result['qa_counts']['accepted'], 2)
        self.assertEqual(FakeJudge.payloads[1]['previous_qa'][0]['answer'], 'Red.')

    def test_explicit_existing_cot_fields_are_text_only_and_aligned(self):
        attached, issues = s.cot_fields({'cot':['first reasoning', None]}, 0, 2)
        self.assertEqual(attached, {'cot':'first reasoning'}); self.assertEqual(issues, [])
        attached, issues = s.cot_fields({'cot':'unmapped row reasoning'}, 0, 2)
        self.assertEqual(attached, {}); self.assertEqual(issues[0]['reason'], 'ambiguous_cot_attachment')
        attached, issues = s.cot_fields({'cot':{'unknown':'structure'}}, 0, 1)
        self.assertEqual(issues[0]['reason'], 'unsupported_cot_field')

    def test_protocol_failure_stays_review_without_retry(self):
        c = self.prepare([row('protocol error')]); result = self.run_stage2(c)
        self.assertEqual(result['counts'], {'review': 1})
        self.assertEqual(result['model_status_counts'], {'model_error': 1})
        self.assertEqual(len(FakeJudge.payloads), 1)

    def test_input_limit_does_not_truncate_and_send(self):
        c = self.prepare([row(' '.join('word%d' % x for x in range(100)))]); c['max_input_chars'] = 100
        result = self.run_stage2(c)
        self.assertEqual(result['reason_counts'], {'input_too_long': 1})
        self.assertEqual(result['model_calls'], 0); self.assertEqual(FakeJudge.payloads, [])

    def test_empty_upstream_has_zero_stats_without_model_calls(self):
        r = row(); r['conversations'][1]['value'] = ''
        c = self.prepare([r]); result = self.run_stage2(c)
        self.assertEqual(result['status'], 'EMPTY_UPSTREAM')
        self.assertEqual(result['rows'], 0); self.assertEqual(FakeJudge.payloads, [])

    def test_stage1_row_tamper_fails_before_model(self):
        c = self.prepare([row()]); path = Path(c['source']); before = path.read_bytes()
        path.write_bytes(before.replace(b'Red.', b'Blue'))
        with self.assertRaisesRegex(ValueError, 'lineage mismatch'):
            self.run_stage2(c)
        self.assertEqual(FakeJudge.payloads, [])

    def test_parent_audit_tamper_refused(self):
        c = self.prepare([row()]); (self.parent/'delivery/audit.jsonl').write_bytes(b'')
        with self.assertRaisesRegex(ValueError, 'audit SHA'):
            self.run_stage2(c)

    def test_resume_does_not_repeat_completed_local_model_calls(self):
        c = self.prepare([row('Red.', 'a'), row('has reasoning', 'b')])
        with patch.object(core, 'publish', side_effect=RuntimeError('synthetic export crash')):
            with self.assertRaises(RuntimeError): self.run_stage2(c)
        self.assertEqual(len(FakeJudge.payloads), 2)
        self.run_stage2(c)
        self.assertEqual(len(FakeJudge.payloads), 2)


if __name__ == '__main__':
    unittest.main()

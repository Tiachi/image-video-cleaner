"""Text-only label contract and deterministic routing. No media or model IO."""
import re

FIELDS = ('quality_score', 'qa_match', 'refusal', 'obvious_error', 'cot_class', 'language')
LANGUAGES = {0: 'unknown', 1: 'zh', 2: 'en', 3: 'other', 4: 'mixed'}
COT = {0: 'already_has_usable_cot', 1: 'no_cot_not_suitable', 2: 'no_cot_suitable', 9: 'unknown'}


def parse(text):
    if not isinstance(text, str) or len(text) > 128 or not re.fullmatch(
            r'\s*(10|[1-9])\s*,\s*[012]\s*,\s*[0123]\s*,\s*[012]\s*,\s*[0129]\s*,\s*[01234]\s*', text):
        raise ValueError('expected six bounded integers, no explanation')
    labels = dict(zip(FIELDS, map(int, text.strip().split(','))))
    labels['raw_cot_class'] = labels['cot_class']
    if labels['cot_class'] == 9:
        labels['cot_class'] = None
    labels['language_name'] = LANGUAGES[labels['language']]
    return labels


def issues_for(labels, minimum_score):
    issues = []
    definite = labels['qa_match'] == 0 or labels['refusal'] == 1 or labels['obvious_error'] == 1
    if definite and labels['quality_score'] > 4:
        return [{'reason': 'model_label_contradiction', 'action': 'review'}]
    for field, bad, reason in (('qa_match', 0, 'qa_mismatch'), ('refusal', 1, 'invalid_refusal'),
                               ('obvious_error', 1, 'text_provable_error')):
        if labels[field] == bad:
            issues.append({'reason': reason, 'action': 'reject'})
    for field, unknown in (('qa_match', 2), ('refusal', 3), ('obvious_error', 2), ('language', 0)):
        if labels[field] == unknown:
            issues.append({'reason': field+'_uncertain', 'action': 'review'})
    if labels['cot_class'] is None:
        issues.append({'reason': 'cot_uncertain', 'action': 'review'})
    if minimum_score is not None and labels['quality_score'] < minimum_score:
        issues.append({'reason': 'below_quality_threshold', 'action': 'reject'})
    return issues

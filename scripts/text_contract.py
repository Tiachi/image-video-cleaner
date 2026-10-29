"""Text-only label contract and deterministic routing. No media or model IO."""
import re

FIELDS = ('quality_score', 'qa_match', 'refusal', 'obvious_error', 'cot_class', 'language',
          'qa_type', 'option_count')
LANGUAGES = {0: 'unknown', 1: 'zh', 2: 'en', 3: 'other', 4: 'mixed'}
COT = {0: 'already_has_usable_cot', 1: 'no_cot_not_suitable', 2: 'no_cot_suitable', 9: 'unknown'}
QA_TYPES = {0: 'open_ended', 1: 'mcq', 2: 'true_false', 3: 'unknown'}
PARSE = re.compile(r'\s*(10|[1-9])\s*,\s*[012]\s*,\s*[0123]\s*,\s*[012]\s*,\s*[0129]\s*,\s*[01234]'
                   r'\s*,\s*[0123]\s*,\s*(10|[0-9])\s*')


def parse(text):
    if not isinstance(text, str) or len(text) > 160 or not PARSE.fullmatch(text):
        raise ValueError('expected eight bounded integers, no explanation')
    labels = dict(zip(FIELDS, map(int, text.strip().split(','))))
    labels['raw_cot_class'] = labels['cot_class']
    if labels['cot_class'] == 9:
        labels['cot_class'] = None
    labels['language_name'] = LANGUAGES[labels['language']]
    labels['qa_type_name'] = QA_TYPES[labels['qa_type']]
    return labels


def issues_for(labels, minimum_score, structure=None):
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

    qa_type, options = labels['qa_type'], labels['option_count']
    if qa_type == 3:
        issues.append({'reason': 'qa_type_uncertain', 'action': 'review'})
    if qa_type == 1 and not 2 <= options <= 10:
        issues.append({'reason': 'mcq_option_count_invalid', 'action': 'review'})
    if qa_type == 0 and options != 0:
        issues.append({'reason': 'oe_option_count_invalid', 'action': 'review'})
    if qa_type == 2 and options != 2:
        issues.append({'reason': 'tf_option_count_invalid', 'action': 'review'})
    if qa_type == 3 and options != 0:
        issues.append({'reason': 'unknown_type_option_count_invalid', 'action': 'review'})

    if structure:
        detected_type = structure.get('qa_type')
        detected_count = structure.get('option_count') or 0
        if detected_type in (0, 1, 2) and qa_type != detected_type:
            issues.append({'reason': 'qa_type_mismatch', 'action': 'review'})
        if detected_type == 1 and qa_type == 1 and detected_count and options != detected_count:
            issues.append({'reason': 'option_count_mismatch', 'action': 'review'})
        if detected_type == 3:
            issues.append({'reason': 'qa_structure_uncertain', 'action': 'review'})
        issues.extend(dict(issue) for issue in structure.get('issues') or [])

    if minimum_score is not None and labels['quality_score'] < minimum_score:
        issues.append({'reason': 'below_quality_threshold', 'action': 'reject'})
    return issues

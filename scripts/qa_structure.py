"""Deterministic question-type and option parsing; no model or media IO.

The parser is deliberately conservative. It reports OE/MCQ/TF plus an option
count, and routes ambiguity to review instead of guessing. Stage02 labels are
cross-checked against this parser; the model is not shown these detections.
"""
from collections import Counter
import re
import unicodedata

LINE_OPTION = re.compile(r'(?m)^\s*\(?([A-Ha-h])\)?[\.、:：\)]\s*(.*?)\s*$')
INLINE_OPTION = re.compile(r'(?<![A-Za-z0-9])([A-Ha-h])[\.、\)]\s*')
CIRCLED = '①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳'
CIRCLED_RE = re.compile('[' + CIRCLED + ']')
JSON_OPTIONS = re.compile(r'(?is)\boptions?\s*[:：]\s*\[(.*?)\]')
JSON_ITEM = re.compile(r"'([^']*)'|\"([^\"]*)\"")
TF_RE = re.compile(r'(?i)(判断题|对错题|正误题|true\s*/\s*false|true\s+or\s+false|'
                   r'是否正确|是对还是错|yes\s*/\s*no|判断对错)')
YESNO_RE = re.compile(r'(?i)^\s*(is|are|was|were|do|does|did|has|have|had)\b'
                      r'(?!.*\b(any|what|which|how|why|where|when|tell|describe|explain|list)\b)'
                      r'.*\?|是否|吗[？?]?\s*$')
MCQ_RE = re.compile(r'(?i)(which of the following|multiple[- ]choice|\bmcq\b|choose|select|'
                    r'下列|哪一项|哪项|以下哪|选项|正确的是|错误的是)')
ANSWER_LETTER = re.compile(r'^\s*\(?([A-Ha-h])\)?(?:\s*[\.、:：\)]\s*.*)?$', re.S)


def _norm(value):
    return ' '.join(unicodedata.normalize('NFKC', value or '').casefold().split())


def _issues_for(options):
    issues = []
    texts = [_norm(x) for x in options if x.strip()]
    if len(texts) != len(set(texts)):
        issues.append({'reason': 'duplicate_options', 'action': 'review'})
    if len(texts) > 10:
        issues.append({'reason': 'too_many_options', 'action': 'review'})
    return issues


def detect(question, answer=''):
    """Return qa_type (0 OE,1 MCQ,2 TF,3 unknown), option count and evidence."""
    question = question or ''
    answer = answer or ''
    line_matches = list(LINE_OPTION.finditer(question))
    line = [(m.group(1).upper(), m.group(2).strip()) for m in line_matches]
    first_line_start = line_matches[0].start() if line_matches else None
    labels = []
    options = []
    method = None
    if len({x[0] for x in line}) >= 2:
        labels = [x[0] for x in line]
        options = [x[1] for x in line]
        method = 'line_markers'
    if not options:
        json_match = JSON_OPTIONS.search(question)
        if json_match:
            items = [a or b for a, b in JSON_ITEM.findall(json_match.group(1))]
            if len(items) >= 2:
                options = items
                labels = [chr(65 + i) for i in range(len(items))]
                method = 'json_options'
    if not options:
        circled = list(CIRCLED_RE.findall(question))
        # Circled digits often label figure parts; require a choice marker or
        # at least three of them before treating them as options.
        if len(circled) >= 3 or (len(circled) >= 2 and MCQ_RE.search(question)):
            options = circled
            labels = [chr(65 + i) for i in range(len(circled))]
            method = 'circled_markers'
    if not options and MCQ_RE.search(question):
        inline = [m.group(1).upper() for m in INLINE_OPTION.finditer(question)]
        if len(set(inline)) >= 2:
            labels = list(dict.fromkeys(inline))
            options = [''] * len(labels)
            method = 'inline_markers'

    answer_letter = None
    match = ANSWER_LETTER.match(answer.strip())
    if match:
        answer_letter = match.group(1).upper()

    issues = _issues_for(options)
    if options:
        qa_type, option_count = 1, len(options)
        if answer_letter and answer_letter not in labels:
            index = ord(answer_letter) - 64
            # Some sources omit the last option label (e.g. A/B/C then an
            # unlabeled fourth line). Do not reject before checking the tail.
            trailing = len([x for x in question[first_line_start:].splitlines()
                            if x.strip()]) if first_line_start is not None else 0
            if first_line_start is not None and index <= trailing:
                option_count = max(option_count, index)
                issues.append({'reason': 'option_labeling_incomplete', 'action': 'review'})
            else:
                issues.append({'reason': 'answer_letter_out_of_range', 'action': 'reject'})
    elif TF_RE.search(question) or YESNO_RE.search(question):
        qa_type, option_count, labels = 2, 2, ['TRUE', 'FALSE']
        if answer_letter:
            issues.append({'reason': 'answer_letter_without_options', 'action': 'review'})
    elif MCQ_RE.search(question):
        qa_type, option_count, method = 3, 0, 'mcq_marker_without_options'
        issues.append({'reason': 'mcq_options_missing', 'action': 'review'})
    else:
        qa_type, option_count, method = 0, 0, 'open_ended'
        if answer_letter:
            issues.append({'reason': 'answer_letter_without_options', 'action': 'review'})

    return {'qa_type': qa_type, 'option_count': option_count,
            'option_labels': labels, 'option_texts': options,
            'answer_letter': answer_letter, 'method': method, 'issues': issues}

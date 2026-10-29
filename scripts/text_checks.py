"""Deterministic QA checks; no semantic inference or text rewriting."""
from collections import Counter
import hashlib
import json
import re
import unicodedata

from qa_structure import detect as detect_qa_structure

PLACEHOLDER = re.compile(r'<(?:image|video)>', re.I)
TOKEN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[^\W_]+', re.UNICODE)
BOILERPLATE = (
    ('lorem_ipsum', re.compile(r'lorem ipsum', re.I)),
    ('javascript', re.compile(r'javascript', re.I)),
    ('template_ai_refusal', re.compile(
        r'(as an ai|\u4f5c\u4e3a\s*ai|\u6211\u662f\u4e00\u4e2a\s*ai|\u6211\u65e0\u6cd5(\u89c2\u770b|\u67e5\u770b|\u770b\u5230)|\u770b\u4e0d[\u5230\u89c1]\s*(\u56fe\u7247|\u56fe\u50cf|\u89c6\u9891)|'
        r'cannot (see|view) (the )?(image|video))', re.I)),
)


def normalize(text):
    return ' '.join(unicodedata.normalize('NFKC', text).casefold().split())


def content(text):
    # A media-only request is explicitly empty for this QA cleaning stage.
    text = PLACEHOLDER.sub('', text).replace('\\n', ' ')
    return ''.join(c for c in text if unicodedata.category(c) != 'Cf').strip()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    allow_nan=False).encode()).hexdigest()


def repetition(text, n=4, min_tokens=32, threshold=.6):
    tokens = TOKEN.findall(normalize(content(text)))
    total = max(0, len(tokens)-n+1)
    counts = Counter(tuple(tokens[i:i+n]) for i in range(total))
    ratio = (total-len(counts))/total if total else 0.0
    return {'tokens': len(tokens), 'n': n, 'ngrams': total,
            'unique_ngrams': len(counts), 'repeated_fraction': round(ratio, 6),
            'flagged': len(tokens) >= min_tokens and ratio >= threshold}


def datatrove_repetition(text, n=8, min_tokens=128, threshold=.6):
    """Datatrove SFT mechanical rule: n-gram repetition over a long text."""
    tokens = TOKEN.findall(normalize(content(text)))
    total = max(0, len(tokens)-n+1)
    if len(tokens) < max(n, min_tokens):
        return {'tokens': len(tokens), 'n': n, 'ngrams': total, 'repeated_fraction': 0.0,
                'flagged': False}
    grams = Counter(tuple(tokens[i:i+n]) for i in range(total))
    ratio = (total-len(grams))/total if total else 0.0
    return {'tokens': len(tokens), 'n': n, 'ngrams': total, 'unique_ngrams': len(grams),
            'repeated_fraction': round(ratio, 6), 'flagged': ratio >= threshold}


def language_proxy(text):
    cjk = latin = 0
    for char in content(text):
        code = ord(char)
        if 0x3400 <= code <= 0x9fff or 0xf900 <= code <= 0xfaff:
            cjk += 1
        elif char.isalpha() and char.isascii():
            latin += 1
    if cjk:
        return 'zh_or_mixed'
    return 'latin_script' if latin else 'other_or_short'


def boilerplate_flags(text):
    return [name for name, pattern in BOILERPLATE if pattern.search(text or '')]


def extract_pairs(row):
    """Strict adapters: conversations, or explicit question/answer scalars.

    No skipping unmatched/unsupported turns and no coercion of missing answers.
    """
    if not isinstance(row, dict):
        raise ValueError('row must be object')
    if 'conversations' not in row:
        if 'question' not in row or 'answer' not in row:
            raise ValueError('need conversations or question+answer')
        if not isinstance(row['question'], str) or not isinstance(row['answer'], str):
            raise ValueError('question/answer must be strings')
        return [(row['question'], row['answer'])]
    turns = row['conversations']
    if not isinstance(turns, list) or not turns or len(turns) % 2:
        raise ValueError('need nonempty alternating QA turns')
    pairs = []
    for i in range(0, len(turns), 2):
        values = []
        for turn, roles in ((turns[i], ('human', 'user')), (turns[i+1], ('gpt', 'assistant'))):
            if not isinstance(turn, dict):
                raise ValueError('turn must be object')
            role = turn.get('from', turn.get('role'))
            text = turn.get('value', turn.get('content'))
            if role not in roles or not isinstance(text, str):
                raise ValueError('unsupported role/order/text content')
            values.append(text)
        pairs.append(tuple(values))
    return pairs


def check_pairs(pairs, config, text_v2=None):
    issues, metrics, seen = [], [], set()
    text_v2 = text_v2 or {}
    for index, (question, answer) in enumerate(pairs):
        item = {'qa_index': index}
        for field, text in (('question', question), ('answer', answer)):
            if not content(text):
                issues.append({'reason': 'empty_'+field, 'action': 'reject', 'qa_index': index})
            if len(text) > 262144:
                issues.append({'reason': field+'_too_large_for_ngram', 'action': 'review', 'qa_index': index})
                item[field] = {'status': 'not_computed', 'characters': len(text)}
                continue  # bounded analysis; never truncate or mark this text clean
            measure = repetition(text, config['ngram'], config['min_tokens'], config['threshold'])
            item[field] = measure
            if measure['flagged']:
                issues.append({'reason': field+'_ngram_repetition',
                               'action': config['action'], 'qa_index': index})
            if text_v2.get('datatrove_enabled'):
                datatrove = datatrove_repetition(text, text_v2['datatrove_ngram'],
                                                 text_v2['datatrove_min_tokens'],
                                                 text_v2['datatrove_threshold'])
                item[field+'_datatrove'] = datatrove
                if datatrove['flagged']:
                    issues.append({'reason': field+'_datatrove_repetition',
                                   'action': text_v2['datatrove_action'], 'qa_index': index})
        if text_v2.get('language_proxy'):
            item['language_proxy'] = language_proxy(question+'\n'+answer)
        if text_v2.get('qa_structure'):
            structure = detect_qa_structure(question, answer)
            item['qa_structure'] = structure
            issues.extend(dict(issue, qa_index=index) for issue in structure['issues'])
        if text_v2.get('boilerplate_enabled'):
            for reason in boilerplate_flags(answer):
                issues.append({'reason': reason, 'action': text_v2['boilerplate_action'],
                               'qa_index': index})
        key = fingerprint([normalize(question), normalize(answer)])
        if key in seen:
            issues.append({'reason': 'repeated_qa_inside_row', 'action': 'review', 'qa_index': index})
        seen.add(key)
        metrics.append(item)
    return issues, metrics

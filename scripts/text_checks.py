"""Deterministic QA checks; no semantic inference or text rewriting."""
from collections import Counter
import hashlib
import json
import re
import unicodedata

PLACEHOLDER = re.compile(r'<(?:image|video)>', re.I)
TOKEN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]|[^\W_]+', re.UNICODE)


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


def check_pairs(pairs, config):
    issues, metrics, seen = [], [], set()
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
        key = fingerprint([normalize(question), normalize(answer)])
        if key in seen:
            issues.append({'reason': 'repeated_qa_inside_row', 'action': 'review', 'qa_index': index})
        seen.add(key)
        metrics.append(item)
    return issues, metrics

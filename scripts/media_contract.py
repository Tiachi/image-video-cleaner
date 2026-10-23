"""Small, explicit contract for image-conditioned QA judging."""
import re


def parse(text, frames):
    if not isinstance(text, str) or not re.fullmatch(r'\s*\d{1,2}\s*,\s*[0-2]\s*,\s*\d{1,2}\s*,\s*\d{1,2}\s*', text):
        raise ValueError('expected score,alignment,evidence1,evidence2')
    score, match, first, second = map(int, text.split(','))
    if not 1 <= score <= 10 or not 0 <= first <= frames or not 0 <= second <= frames:
        raise ValueError('score/evidence outside supplied range')
    if second and (not first or first == second):
        raise ValueError('second evidence needs distinct first evidence')
    if match in (0, 1) and not first:
        raise ValueError('support/contradiction requires visible evidence')
    if match == 0 and score > 4:
        raise ValueError('contradiction conflicts with quality score')
    return {'media_quality_score': score, 'media_alignment': match,
            'evidence_frame_ids': [x for x in (first, second) if x]}


def issues_for(labels, minimum):
    match = labels['media_alignment']
    reason = {0: ('visible_contradiction', 'reject'),
              2: ('sampled_evidence_insufficient', 'review')}.get(match)
    if reason:
        return [{'reason': reason[0], 'action': reason[1]}]
    if minimum is not None and labels['media_quality_score'] < minimum:
        return [{'reason': 'media_quality_below_threshold', 'action': 'reject'}]
    return []

"""Interval unions and stable tasks; no semantic coverage inference."""
from __future__ import annotations
from collections import defaultdict
from clinical_proposals.models import SpanRef
from clinical_proposals.source_adapter import digest
from .models import ReviewTask, ReviewType


def union_characters(spans: list[SpanRef]) -> int:
    groups=defaultdict(list)
    for item in spans:
        groups[item.buffer_id].append((item.char_start,item.char_end))
    total=0
    for intervals in groups.values():
        end=-1
        for a,b in sorted(intervals):
            total += max(0,b-max(a,end))
            end=max(end,b)
    return total


def task(*, revision: str | None, regions=(), spans=(), pages=(), proposal=None,
         candidate=None, reason: str, kind: ReviewType, outcome: str,
         classification='uncertainty') -> ReviewTask:
    seed={'revision':revision,'regions':sorted(set(regions)),
        'spans':[s.model_dump() for s in spans],'proposal':proposal,'candidate':candidate,'reason':reason,'kind':kind}
    return ReviewTask(task_id='review-task:'+digest(seed),source_revision=revision,
        region_ids=tuple(seed['regions']),spans=tuple(spans),pages=tuple(sorted(set(pages))),
        proposal_id=proposal,candidate_key=candidate,reason=reason,review_type=kind,
        classification=classification,technical_outcome=outcome)

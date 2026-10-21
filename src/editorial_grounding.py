"""Narrow editorial wording checks, never clinical entailment or approval.

Only a complete recommendation metadata sentence is recognized. Strength and
certainty must match one complete selected source sentence, in the same roles.
No treatment, population, actor, number, negation or qualifier is discarded.
Unrecognized language remains subject to the existing grounding rule.
"""
from __future__ import annotations

import re


_METADATA = re.compile(
    r"the recommendation (?:is|remains) "
    r"(?P<strength>conditional|weak|strong)"
    r"(?:,? with | and is based on )"
    r"(?P<certainty>very low|low|moderate|high) certainty of evidence[.]?"
)


def recommendation_metadata_equivalent(sentence: str, source_text: str) -> bool:
    """Recognize explicit strength/certainty wording without inferring relations.

    The two grammatical forms retain identical nominal subject, strength and
    certainty. Full matching rejects added indications, exceptions, advice,
    polarity changes and cross-sentence assembly. Review is still mandatory.
    """
    def metadata(value: str) -> tuple[str, str] | None:
        match = _METADATA.fullmatch(" ".join(value.casefold().split()))
        return (match["strength"], match["certainty"]) if match else None

    candidate = metadata(sentence)
    return candidate is not None and any(
        metadata(passage) == candidate
        for passage in re.split(r"(?<=[.!?])\s+", source_text.strip())
    )

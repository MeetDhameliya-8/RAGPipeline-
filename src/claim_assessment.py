"""Conservative claim/source consistency checks; not medical semantic proof.

Numeric presence, copied sentences and lexical similarity never approve a
chunk. Narrow explicit conflicts are rejected; unproved relationships are held.
Source text is transient. Exported records contain offsets and reason codes.
"""
from __future__ import annotations

from collections import Counter
from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field

from .document_concepts import BoundRole
from .evidence import EvidenceDocument, EvidenceSpan, _check_span, text_hash, validate_evidence
from .numeric_evidence import numeric_evidence, UNIT_RE


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class ClaimAssessment(Record):
    claim_id: str
    chunk_number: int
    char_start: int
    char_end: int
    status: Literal["supported", "contradicted", "requires_review", "missing_evidence"]
    reasons: list[str]
    source_span_candidates: list[EvidenceSpan]
    # 'supported' is narrow literal source consistency, never clinical approval.
    support_basis: Literal["literal_source_consistency", "unresolved"] = "unresolved"


class ChunkAssessment(Record):
    chunk_number: int
    claims: list[ClaimAssessment]
    status: Literal["REVIEW_REQUIRED", "REJECTED"]
    reasons: list[str]
    word_count: int
    length_exception: Literal["none", "short_source_evidence", "source_specific_justification_required"]
    clinically_approved: Literal[False] = False


def _norm(value: str) -> str:
    return " ".join(value.casefold().split())


def _sentences(text: str):
    # This is a claim-candidate segmentation, not a semantic decomposition.
    # Decimal points have no following whitespace. Abbreviations can still
    # produce incomplete candidates, which never receive semantic approval.
    start = 0
    for match in re.finditer(r"(?<=[.!?])\s+(?=[A-Z])", text):
        if text[start:match.start()].strip():
            yield start, match.start()
        start = match.end()
    if text[start:].strip():
        yield start, len(text)


def _role_values(doc: EvidenceDocument, roles: list[BoundRole]) -> dict[str, list[str]]:
    result = {}
    for role in roles:
        _check_span(doc, role.evidence)
        value = doc.buffers[role.evidence.buffer_id].text[role.evidence.char_start:role.evidence.char_end]
        result.setdefault(role.role, []).append(_norm(value))
    return result


def _literal_associations(text: str, entities: list[str]):
    """Recognize two explicit textual forms, never nearest-token arm inference.

    Only 'quantity with/in/for entity' and 'entity: quantity' are handled.
    Unrecognized/repeated/ambiguous associations remain review cases. These
    forms do not prove the clinical endpoint, population or time relationship.
    """
    result = []
    for entity in entities:
        label = re.escape(entity)
        for token in numeric_evidence(text):
            unit = UNIT_RE.match(text,token.end)
            suffix = text[unit.end() if unit and token.unit != "%" else token.end:]
            prefix = text[:token.start]
            if (re.match(r"\s+(?:with|in|for|on)\s+"+label+r"(?!\w)", suffix, re.I) or
                    re.search(r"(?<!\w)"+label+r"\s*:\s*$", prefix, re.I)):
                result.append((entity, token.value, token.unit, token.operator))
    return result


def _affirmed(pattern: str, text: str) -> bool:
    for match in re.finditer(pattern,text):
        prefix = re.split(r"[.;:]",text[max(0,match.start()-60):match.start()])[-1]
        if not re.search(r"\b(?:not|no|without)\b(?:\W+\w+){0,5}\W*$",prefix):
            return True
    return False


def _checks(claim: str, source: str, owned_source: str, roles: dict[str, list[str]]) -> tuple[list[str], list[str], list[str]]:
    conflicts, missing, review = [], [], []
    candidate = _norm(claim)
    original = _norm(owned_source)
    tokens = numeric_evidence(claim)
    source_tokens = numeric_evidence(source)
    for t in tokens:
        matches = [s for s in source_tokens if s.value == t.value]
        if t.ambiguous or any(s.ambiguous for s in matches):
            review.append("numeric_glyph_uncertain")
        elif not matches:
            missing.append("numeric_value_unavailable")
        elif not any(s.unit == t.unit and s.operator == t.operator for s in matches):
            review.append("numeric_unit_or_operator_unverified")
    entities = roles.get("intervention", []) + roles.get("comparator", [])
    source_pairs = _literal_associations(_norm(owned_source), entities)
    candidate_pairs = _literal_associations(candidate, entities)
    for entity, value, unit, operator in candidate_pairs:
        found = [(v,u,o) for e,v,u,o in source_pairs if e == entity]
        if found and (value,unit,operator) not in found:
            # Multiple source quantities for the same arm do not license an
            # endpoint association from proximity. Hold instead of choosing.
            if len(set(found)) == 1:
                conflicts.append("explicit_arm_quantity_conflict")
            else:
                review.append("arm_endpoint_quantity_ambiguous")
    # Units/signs: a uniquely retained explicit entity/value association can
    # establish a lexical conflict independently of bare number membership.
    for entity, value, unit, operator in candidate_pairs:
        found = {(u,o) for e,v,u,o in source_pairs if e == entity and v == value}
        if len(found) == 1 and (unit,operator) not in found:
            conflicts.append("explicit_quantity_unit_or_sign_conflict")
    outcome_names = roles.get("outcome", [])
    # Limit contradiction checks to the owned source, not unrelated methods or
    # another endpoint in context. Multi-endpoint wording is conservatively held.
    aligned = bool(outcome_names) and any(name in candidate for name in outcome_names)
    negative_pattern = r"\b(?:not statistically significant|not significant|no significant difference)\b"
    negative = bool(re.search(negative_pattern, original))
    scoped_negative = any(re.search(negative_pattern,_norm(owned_source[a:b])) and
        any(name in _norm(owned_source[a:b]) for name in outcome_names) for a,b in _sentences(owned_source))
    positive = bool(re.search(r"(?<!not )\bstatistically significant\b", candidate))
    if negative and positive and not re.search(r"\b(?:not statistically significant|no statistically significant)\b", candidate):
        if aligned and scoped_negative:
            conflicts.append("significance_polarity_conflict")
        else:
            review.append("significance_polarity_requires_source_review")
    if aligned:
        if negative and _affirmed(r"\b(?:was equivalent|are equivalent|is equivalent|equally effective|established equivalence)\b", candidate):
            review.append("nonsignificance_does_not_establish_equivalence")
        if (re.search(r"\bsuperiority was not (?:assessed|tested|established|demonstrated)\b", original)
                and re.search(r"\b(?:was superior|is superior|superior to|demonstrated superiority)\b", candidate)):
            conflicts.append("noninferiority_or_unassessed_superiority_strengthened")
        if (re.search(r"\bnoninferiority was not (?:established|demonstrated)\b", original)
                and re.search(r"\b(?:was noninferior|demonstrated noninferiority)\b", candidate)):
            conflicts.append("noninferiority_negation_conflict")
    strengths = " ".join(roles.get("recommendation_strength", []))
    if "conditional" in strengths and _affirmed(r"\b(?:strongly recommended|strong recommendation|must receive|recommended for all)\b", candidate):
        conflicts.append("recommendation_strength_increased")
    certainty = " ".join(roles.get("evidence_certainty", []))
    if "low" in certainty and _affirmed(r"\bhigh[- ]certainty\b", candidate):
        conflicts.append("evidence_certainty_increased")
    if (re.search(r"\bno adverse events (?:occurred|were reported)\b", candidate) and
            re.search(r"\badverse events\b", original) and
            any(t.value > 0 and t.unit == "%" for t in numeric_evidence(owned_source))):
        review.append("adverse_event_negation_requires_review")
    # Existing deterministic checks cover additional local numeric/negation
    # patterns in validate_chunks. This prototype makes no exhaustive claim.
    return list(dict.fromkeys(conflicts)), list(dict.fromkeys(missing)), list(dict.fromkeys(review))


def assess_editorial_chunk(chunk_number: int, text: str, doc: EvidenceDocument,
                           ownership: list[EvidenceSpan], context: list[EvidenceSpan],
                           roles: list[BoundRole]) -> ChunkAssessment:
    """Assess text independently of the writing model; never return approval.

    The generation boundary validates the M3 source-review plan first. Direct
    callers receive only consistency checks, not an attestation of that review.
    """
    validate_evidence(doc)
    spans = ownership + context
    available = bool(ownership)
    for item in spans:
        try:
            _check_span(doc, item)
            if item.buffer_id != doc.canonical_buffer_id or item.char_start == item.char_end:
                available = False
        except ValueError:
            available = False
    for role in roles:
        if not any(s.buffer_id == role.evidence.buffer_id and
                   s.char_start <= role.evidence.char_start < role.evidence.char_end <= s.char_end for s in spans):
            available = False
    if available:
        passages = [doc.buffers[s.buffer_id].text[s.char_start:s.char_end] for s in spans]
        source = "\n\n".join(passages)
        owned_source = "\n\n".join(doc.buffers[s.buffer_id].text[s.char_start:s.char_end] for s in ownership)
        available = bool(owned_source.strip())
        scope = _role_values(doc, roles)
    else:
        source, owned_source, scope = "", "", {}
    chunk_reasons = []
    for kind in ("population", "intervention", "comparator", "outcome", "time_point", "qualification", "limitation", "recommendation_strength", "evidence_certainty"):
        if any(value not in _norm(text) for value in scope.get(kind, [])):
            chunk_reasons.append(kind+"_preservation_unverified")
    claims = []
    for a,b in _sentences(text):
        claim = text[a:b]
        conflicts, missing, review = _checks(claim, source, owned_source, scope) if available else ([], ["source_evidence_unavailable"], [])
        literal = available and _norm(claim) in _norm(source)
        status = "contradicted" if conflicts else "missing_evidence" if missing else "requires_review" if review or not literal else "supported"
        reasons = conflicts+missing+review
        if not literal and available:
            reasons.append("semantic_relationship_unverified")
        claims.append(ClaimAssessment(claim_id="claim:"+text_hash(f"{chunk_number}:{a}:{b}:{text_hash(claim)}"),
            chunk_number=chunk_number, char_start=a, char_end=b, status=status, reasons=list(dict.fromkeys(reasons)),
            source_span_candidates=[s.model_copy(deep=True) for s in spans] if available else [],
            support_basis="literal_source_consistency" if status == "supported" else "unresolved"))
    words = len(text.split())
    length = "none" if 100 <= words <= 200 else "short_source_evidence" if words < 100 and len(source.split()) < 100 else "source_specific_justification_required"
    if not claims:
        chunk_reasons.append("no_claim_candidates")
    return ChunkAssessment(chunk_number=chunk_number, claims=claims,
        status="REJECTED" if not claims or any(c.status in {"contradicted","missing_evidence"} for c in claims) else "REVIEW_REQUIRED",
        reasons=list(dict.fromkeys(chunk_reasons)), word_count=words, length_exception=length)


def claim_totals(assessments: list[ChunkAssessment]) -> dict[str,int]:
    counts = Counter(c.status for a in assessments for c in a.claims)
    return {s:counts[s] for s in ("supported","contradicted","requires_review","missing_evidence")}

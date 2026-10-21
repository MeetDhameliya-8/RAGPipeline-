"""Reuse existing deterministic editorial checks; never certify clinical truth."""
from __future__ import annotations

from dataclasses import dataclass
import re

from src import diagnostics as telemetry
from src.chunk_generator import (parse_model_json, repair_json_locally, normalize_generation_result,
    validate_generation_schema, validate_generation_content)
from src.chunk_validator import validate_chunks, eligible_chunk_numbers, _issue, _validation_report
from src.claim_assessment import assess_editorial_chunk, claim_totals
from src.document_concepts import BoundRole
from src.providers.base import ChunkGenerationError
from src.source_abstention import is_source_abstention


@dataclass(frozen=True)
class Assessment:
    document: dict | None
    report: dict
    code: str | None


def _safe_report(report: dict) -> dict:
    report['issues'] = [{**i,'message':'Deterministic validation finding; inspect its rule and bound source evidence.'}
        for i in report['issues']]
    return report


@telemetry.timed('local_chunk_validation')
def assess_response(raw, *, alias, reference, extraction, source, eligibility, resumed=False) -> Assessment:
    count = 0
    phase = 'INVALID_JSON'
    try:
        try:
            parsed = parse_model_json(raw)
        except ChunkGenerationError:
            repaired = repair_json_locally(raw)
            if repaired is None:
                raise
            parsed = parse_model_json(repaired)
        if isinstance(parsed.get('chunks'),list):
            count = len(parsed['chunks'])
            telemetry.count('resumed_candidates' if resumed else 'generated_candidates',count)
        else:
            telemetry.incomplete_candidates()
        if is_source_abstention(parsed,alias):
            telemetry.count('source_held_units')
            report = _validation_report([_issue(None,'ERROR','SOURCE_FIDELITY','Selected source was held.')],0)
            return Assessment(None,_safe_report(report),'SOURCE_PASSAGE_HELD')
        phase = 'SCHEMA_INVALID'
        validate_generation_schema(parsed)
        if len(parsed['chunks']) != 1:
            raise ChunkGenerationError('Expected one clinical candidate.',code='SCHEMA_INVALID')
        document = normalize_generation_result(parsed,source_file=alias)
        document['chunks'][0]['chunk_number'] = 1
        phase = 'REFERENCE_OR_CONTENT_INVALID'
        if (document['article']['source_file'] != alias or document['article']['reference'] != reference or
                document['chunks'][0]['reference'] != reference):
            raise ChunkGenerationError('Source reference/identity mismatch.',code='REFERENCE_OR_CONTENT_INVALID')
        validate_generation_content(document,extraction)
    except (ChunkGenerationError,ValueError,TypeError,KeyError) as exc:
        detail = telemetry.failure(exc,phase)
        if phase == 'INVALID_JSON':
            telemetry.incomplete_candidates()
        else:
            telemetry.count('schema_rejected_candidates' if phase == 'SCHEMA_INVALID' else 'reference_or_content_rejected_candidates',count)
        rule = 'INVALID_REFERENCE' if phase == 'REFERENCE_OR_CONTENT_INVALID' else 'SCHEMA'
        report = _validation_report([_issue(None,'ERROR',rule,'Candidate failed technical or source-reference validation.')],count)
        report['failure_classification'] = detail
        return Assessment(None,_safe_report(report),phase)

    evidence = source.evidence.model_copy(deep=True)
    owned = [s.to_evidence() for s in eligibility.ownership]
    context = [s.to_evidence() for s in eligibility.context]
    selected = '\n\n'.join(evidence.buffers[s.buffer_id].text[s.char_start:s.char_end] for s in owned+context)
    base = validate_chunks(document,chunk_sources={1:[{'text':selected,'review_required':True}]},editorial=True)
    # BoundRole carries only a role/span. It is used here solely as the existing
    # consistency checker's input, never as a ReviewedQuestion or attestation.
    roles = [BoundRole(role=r.role,evidence=r.evidence.to_evidence()) for r in eligibility.scope]
    claim = assess_editorial_chunk(1,document['chunks'][0]['original_text'],evidence,owned,context,roles)
    issues = list(base['issues'])
    chunk = document['chunks'][0]
    norm = lambda x:' '.join(x.casefold().split())
    topics = [evidence.buffers[r.evidence.buffer_id].text[r.evidence.char_start:r.evidence.char_end]
        for r in eligibility.scope if r.role == 'topic']
    if not any(norm(t) in norm(chunk['topic']) for t in topics):
        issues.append(_issue(1,'ERROR','EDITORIAL_TOPIC_IDENTITY','Literal proposal topic identity requires source review.'))
    candidate_populations = set(re.findall(r'\b(?:adults|children|patients|participants)\b',chunk['original_text'],re.I))
    source_populations = set(re.findall(r'\b(?:adults|children|patients|participants)\b',selected,re.I))
    if {p.casefold() for p in candidate_populations} - {p.casefold() for p in source_populations}:
        issues.append(_issue(1,'ERROR','PROPOSAL_SCOPE_CONFLICT','An unsupported population descriptor was introduced.'))
    if claim.status == 'REJECTED':
        issues.append(_issue(1,'ERROR','EDITORIAL_CLAIM_CONFLICT','Independent consistency assessment rejected a claim.'))
    if claim.reasons:
        issues.append(_issue(1,'WARNING','EDITORIAL_SCOPE_REVIEW','Scope/qualification preservation needs independent review.'))
    report = _safe_report(_validation_report(issues,1))
    report.update(claim_assessments=[claim.model_dump(mode='json')],claim_totals=claim_totals([claim]),
        clinically_approved=False,human_source_reviewed=False,document_complete=False,
        source_ownership=[{'chunk_number':1,'proposal_id':eligibility.proposal_id,
            'ownership':[s.model_dump() for s in eligibility.ownership], 'context':[s.model_dump() for s in eligibility.context]}])
    allowed = eligible_chunk_numbers(report,{'REVIEW_REQUIRED'}) == {1}
    telemetry.count('assessed_candidate_events')
    telemetry.count('accepted_candidate_events' if allowed else 'rejected_candidate_events')
    return Assessment(document,report,None if allowed else 'SOURCE_VALIDATION_FAILED')

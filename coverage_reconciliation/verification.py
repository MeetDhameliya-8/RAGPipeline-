"""Verify current A2/A3 links from explicit configuration and bounded artifacts."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from pathlib import Path
from typing import Any

from clinical_proposals.source_adapter import digest
from proposal_editorial import requests,validation
from proposal_editorial.checkpoints import MARKER
from proposal_editorial.models import WorkflowConfig,WorkflowResult
from src.evidence import text_hash
from src import diagnostics as telemetry
from .artifacts import ArtifactReader
from .models import GenerationContext,VerificationError

HEX=re.compile(r'^[a-f0-9]{64}$')
UNIT_STATES=frozenset({'draftable_review','relationship_review','blocked_source','insufficient_evidence',
    'duplicate_deferred','source_hold','unknown_request_outcome','deferred_after_stop','provider_failure',
    'review_required','quarantined','cancelled','unexpected_failure','not_submitted','duplicate_generated',
    'stored_review','existing_row_preserved','storage_failed'})


def capture_generation_context(provider: Any,config: WorkflowConfig | None=None) -> GenerationContext:
    """Read configuration only. This helper never accesses provider.generate."""
    return GenerationContext(provider_sha256=requests.provider_identity(provider),
        generation_contract=requests.generation_contract(),prompt_sha256=text_hash(requests.editorial_prompt()),
        config=config or WorkflowConfig())


def verify_context(value: GenerationContext | None) -> GenerationContext:
    try:
        value=GenerationContext.model_validate(value.model_dump())
        if value.generation_contract!=requests.generation_contract() or value.prompt_sha256!=text_hash(requests.editorial_prompt()):
            raise ValueError()
        return value
    except (ValueError,AttributeError,TypeError):
        raise VerificationError('GENERATION_CONFIGURATION_UNVERIFIED') from None


def verify_workflow(workflow: WorkflowResult,plan,eligibility,reader: ArtifactReader,run_id: str | None=None) -> WorkflowResult:
    try:
        value=WorkflowResult.model_validate(workflow.model_dump())
        if (value.proposal_plan_id!=plan.plan_id or value.evidence_fingerprint!=plan.evidence_fingerprint
                or value.eligibility!=eligibility or len(value.units)!=len(eligibility)):
            raise ValueError()
        if [u.proposal_id for u in value.units]!=[e.proposal_id for e in eligibility]:
            raise ValueError()
        if any(u.source_order!=e.source_order or u.status not in UNIT_STATES for e,u in zip(eligibility,value.units)):
            raise ValueError()
        drafts=[d.proposal_id for d in value.drafts]
        if len(drafts)!=len(set(drafts)) or not set(drafts)<={p.proposal_id for p in plan.proposals}:
            raise ValueError()
        if [d.source_order for d in value.drafts]!=sorted(d.source_order for d in value.drafts):
            raise ValueError()
        expected=[{'proposal_id':u.proposal_id,'source_order':u.source_order,'status':u.status,'code':u.code,
            'eligibility_state':e.state,'eligibility_reasons':e.reasons,'audit':u.audit,'storage':u.storage}
            for e,u in zip(eligibility,value.units)]
        audit=value.diagnostics
        if (digest(audit.get('unit_outcomes'))!=digest(expected) or audit.get('proposal_plan_id')!=plan.plan_id
                or audit.get('evidence_fingerprint')!=plan.evidence_fingerprint
                or audit.get('clinically_approved') is not False or audit.get('production_ingestion') is not False):
            raise ValueError()
        if reader.read('workspace.json')!=MARKER:
            raise ValueError()
        if run_id is not None:
            if not re.fullmatch(r'[a-f0-9]{32}',run_id):
                raise ValueError()
            names=[run_id+'.json']
        else:
            names=reader.names(('runs',),'*.json',reader.limits.max_run_reports)
        matched=False
        for name in names:
            record=reader.read('runs',name)
            if digest(record)==digest(audit):
                matched=True
                break
        if not matched:
            raise VerificationError('WORKFLOW_RUN_RECORD_UNAVAILABLE')
        return value
    except VerificationError:
        raise
    except (ValueError,TypeError,AttributeError,KeyError):
        raise VerificationError('WORKFLOW_IDENTITY_INVALID') from None


@dataclass
class CandidateProof:
    checkpoint: str
    attempts: int
    raw_hash: str | None=None
    count: int | None=None
    assessment: validation.Assessment | None=None
    validation_verified: bool=False
    failure_code: str | None=None


def candidate_proof(reader: ArtifactReader,key: str,policy: str,*,alias: str,reference: str,
                    extraction,source,eligibility) -> CandidateProof:
    if not HEX.fullmatch(key) or not HEX.fullmatch(policy):
        raise VerificationError('CANDIDATE_IDENTITY_INVALID')
    folder=('units',key)
    intent=reader.read(*folder,'request-intent.json',required=False)
    response=reader.read(*folder,'response.json',required=False)
    failure=reader.read(*folder,'last-failure.json',required=False)
    names=[n for n in reader.names(folder,'request-*.json',reader.limits.max_attempts_per_unit+1)
           if n!='request-intent.json']
    if len(names)>reader.limits.max_attempts_per_unit:
        raise VerificationError('ARTIFACT_BUDGET_EXCEEDED')
    attempts={}
    for name in names:
        record=reader.read(*folder,name)
        attempt=name.removeprefix('request-').removesuffix('.json')
        if (not re.fullmatch(r'[a-f0-9]{32}',attempt) or record!={
                'candidate_key':key,'phase':'request_committed','attempt':attempt}):
            raise VerificationError('REQUEST_RECORD_INVALID')
        attempts[attempt]=record
    if intent is not None and (intent.get('attempt') not in attempts or attempts[intent['attempt']]!=intent):
        raise VerificationError('REQUEST_RECORD_INVALID')
    if not response:
        if failure:
            if not intent or failure.get('attempt')!=intent.get('attempt'):
                return CandidateProof('request_outcome_unknown',len(attempts))
            code=telemetry.safe_code(failure.get('code'))
            return CandidateProof('request_failed',len(attempts),failure_code=code)
        return CandidateProof('request_outcome_unknown' if intent else 'no_request_record',len(attempts))
    if (not intent or response.get('candidate_key')!=key or not isinstance(response.get('raw'),str)
            or response.get('raw_sha256')!=text_hash(response['raw'])
            or set(response)!={'candidate_key','raw','raw_sha256','transport'}):
        raise VerificationError('CANDIDATE_RECORD_INVALID')
    count=None
    try:
        from src.chunk_generator import parse_model_json,repair_json_locally
        from src.providers.base import ChunkGenerationError
        try:
            parsed=parse_model_json(response['raw'])
        except ChunkGenerationError:
            repaired=repair_json_locally(response['raw'])
            if repaired is None:
                raise
            parsed=parse_model_json(repaired)
        if isinstance(parsed.get('chunks'),list):
            count=len(parsed['chunks'])
    except (ValueError,ChunkGenerationError,TypeError,AttributeError):
        pass
    # Verification of the saved verdict is independent of its declared status.
    # Existing deterministic checks are rerun unchanged; they never call a model.
    with telemetry.stage('coverage_candidate_verification'):
        assessment=validation.assess_response(response['raw'],alias=alias,reference=reference,
            extraction=extraction,source=source,eligibility=eligibility,resumed=True)
    decision=reader.read(*folder,'validation-'+policy+'.json',required=False)
    valid=decision=={'candidate_key':key,'validation_policy':policy,
        'raw_sha256':response['raw_sha256'],'validation':assessment.report}
    if decision is not None and not valid:
        raise VerificationError('VALIDATION_RECORD_INVALID')
    return CandidateProof('response_verified',len(attempts),response['raw_sha256'],count,
        assessment,valid)


def expected_link(source,plan,item,key: str) -> dict:
    return {'version':'proposal-editorial-v1','candidate_key':key,'proposal_id':item.proposal_id,
        'source_order':item.source_order,'pdf_sha256':source.evidence.pdf_sha256,
        'evidence_fingerprint':plan.evidence_fingerprint,
        'ownership':[s.model_dump() for s in item.ownership], 'context':[s.model_dump() for s in item.context],
        'human_source_reviewed':False,'clinically_approved':False}

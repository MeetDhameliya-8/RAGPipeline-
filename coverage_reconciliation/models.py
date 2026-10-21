"""Versioned technical accounting, separate from clinical completeness."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from clinical_proposals.models import SpanRef
from proposal_editorial.models import WorkflowConfig

VERSION = 'coverage-reconciliation-v1'
Stage = Literal['extraction','citation','source_inventory','proposals','generation','validation','storage','clinical_review']
StageState = Literal['not_started','incomplete','failed','unknown','completed_verified']
SourceState = Literal['owned_by_proposals','supporting_context_only','existing_source_hold',
    'reviewed_nonclinical_exclusion','structural_candidate_exclusion','not_yet_assessed']
GenerationState = Literal['draft_generated_review_eligible','generated_rejected','held_before_generation',
    'generated_source_hold','provider_failure',
    'insufficient_source_evidence','duplicate_deferred','request_budget_deferred','service_stop_deferred',
    'interrupted_or_unknown','not_started','verification_incomplete']
PersistenceState = Literal['verified_local_review','not_stored','readback_incomplete',
    'eligibility_revoked','reconciliation_required']
ReviewType = Literal['source_extraction','citation_identity','clinical_relationship','missing_qualification',
    'clinical_paraphrase_fidelity','document_coverage','local_storage_reconciliation']


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid',frozen=True,hide_input_in_errors=True)


class ReconciliationLimits(Record):
    max_regions: int = Field(default=5000,ge=1,le=20000,strict=True)
    max_proposals: int = Field(default=256,ge=1,le=10000,strict=True)
    max_drafts: int = Field(default=256,ge=1,le=10000,strict=True)
    max_input_characters: int = Field(default=16000000,ge=1,strict=True)
    max_input_records: int = Field(default=200000,ge=1,strict=True)
    max_artifact_bytes: int = Field(default=2000000,ge=1,strict=True)
    max_total_artifact_bytes: int = Field(default=32000000,ge=1,strict=True)
    max_run_reports: int = Field(default=32,ge=1,le=1024,strict=True)
    max_attempts_per_unit: int = Field(default=16,ge=1,le=1000,strict=True)


class GenerationContext(Record):
    provider_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    generation_contract: str = Field(pattern=r'^[a-f0-9]{64}$')
    prompt_sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    config: WorkflowConfig = Field(default_factory=WorkflowConfig)


class StageObservation(Record):
    """Unverified failure observations cannot assert successful work or approval."""
    stage: Stage
    state: Literal['not_started','incomplete','failed','unknown']
    code: Literal['EXTRACTION_FAILED','EXTRACTION_BLOCKED','REFERENCE_IDENTITY_UNRESOLVED',
        'SOURCE_IDENTITY_INVALID','PROPOSAL_IDENTITY_INVALID','AUTHENTICATION_FAILED','QUOTA_EXHAUSTED',
        'TRANSPORT_TIMEOUT','GENERATION_FAILED','SOURCE_VALIDATION_FAILED','STORAGE_READBACK_INCOMPLETE']


class StageRecord(Record):
    stage: Stage
    state: StageState = 'unknown'
    available: bool = False
    reasons: tuple[str,...] = ()


class SourceIdentity(Record):
    pdf_sha256: str
    evidence_document_id: str
    canonical_buffer_id: str
    evidence_fingerprint: str
    source_map_sha256: str
    inventory_sha256: str


class RegionLink(Record):
    region_id: str
    basis: Literal['canonical','warning','unmapped_native','empty_page']
    span: SpanRef | None
    native_spans: tuple[SpanRef,...] = ()
    pages: tuple[int,...]
    sections: tuple[str,...]
    source_state: SourceState
    inventory_state: str
    proposal_ids: tuple[str,...]
    context_for: tuple[str,...]
    proposal_links_verified: bool
    reviewable_proposals: tuple[str,...] = ()
    verified_storage_proposals: tuple[str,...] = ()
    literal_concern_cues: tuple[str,...] = ()
    concern_basis: Literal['literal_cue_not_clinical_priority'] = 'literal_cue_not_clinical_priority'
    reasons: tuple[str,...] = ()


class ProposalLink(Record):
    proposal_id: str
    category: str
    region_ids: tuple[str,...]
    ownership: tuple[SpanRef,...]
    context: tuple[SpanRef,...]
    a2_status: str
    eligibility: str | None = None
    eligibility_reasons: tuple[str,...] = ()
    generation: GenerationState = 'verification_incomplete'
    claimed_generation_status: str | None = None
    failure_code: str | None = None
    checkpoint: str = 'unavailable'
    attempt_count: int | None = None
    resumed_from_checkpoint: bool | None = None
    candidate_key: str | None = None
    response_sha256: str | None = None
    generated_candidate_count: int | None = None
    validation: Literal['review_required','rejected','source_hold','incomplete','not_started'] = 'incomplete'
    validation_policy: str | None = None
    validation_reasons: tuple[str,...] = ()
    persistence: PersistenceState = 'reconciliation_required'
    chunk_id: str | None = None
    clinical_verification: Literal['not_clinically_reviewed','clinical_review_required'] = 'not_clinically_reviewed'
    duplicate_of: str | None = None
    verification_issues: tuple[str,...] = ()


class ReviewTask(Record):
    task_id: str
    source_revision: str | None
    region_ids: tuple[str,...]
    spans: tuple[SpanRef,...]
    pages: tuple[int,...]
    proposal_id: str | None
    candidate_key: str | None
    reason: str
    classification: Literal['source_blocking','uncertainty','technical_incomplete','mandatory_clinical_review']
    review_type: ReviewType
    technical_outcome: str
    # Intentionally no reviewer, approval timestamp or clinical decision fields.


class CoverageReport(Record):
    version: Literal['coverage-reconciliation-v1'] = VERSION
    reconciliation_id: str
    implementation_sha256: str
    scope: Literal['automatic_draft_traceability'] = 'automatic_draft_traceability'
    source: SourceIdentity | None
    proposal_plan_id: str | None
    validation_policy: str
    input_state_sha256: str
    artifact_state_sha256: str
    stages: tuple[StageRecord,...]
    regions: tuple[RegionLink,...]
    proposals: tuple[ProposalLink,...]
    review_tasks: tuple[ReviewTask,...]
    counts: dict[str,int | None]
    source_characters_by_state: dict[str,int]
    review_tasks_by_reason: dict[str,int]
    verification_issues: tuple[str,...]
    technical_reconciliation_complete: bool
    clinically_complete: Literal[False] = False
    clinically_approved_chunks: Literal[0] = 0
    production_ingestion_allowed: Literal[False] = False


class VerificationError(ValueError):
    def __init__(self,code: str):
        self.code=code
        super().__init__(code)

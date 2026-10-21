"""Internal, immutable proposal contracts. No clinical content or review payloads."""
from __future__ import annotations

from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from src.evidence import EvidenceSpan

VERSION = "clinical-proposals-v1"
Category = Literal["study_scope", "primary_outcome", "secondary_outcome", "comparative_outcome",
    "safety", "recommendation", "applicability", "procedure_selection", "contraindication",
    "diagnostic_selection", "source_statement"]
Status = Literal["proposed", "needs_relationship_review", "blocked_source_evidence",
    "duplicate_proposal", "insufficient_evidence"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class ProposalLimits(Record):
    max_proposals: int = Field(default=256, ge=1, le=10000, strict=True)
    max_context_characters: int = Field(default=20000, ge=1, strict=True)
    max_context_regions: int = Field(default=8, ge=0, le=128, strict=True)
    max_region_characters: int = Field(default=50000, ge=1, strict=True)
    max_role_candidates: int = Field(default=256, ge=1, strict=True)
    max_concern_links: int = Field(default=128, ge=0, le=5000, strict=True)
    max_input_characters: int = Field(default=16000000, ge=1, strict=True)
    max_input_regions: int = Field(default=5000, ge=1, strict=True)
    max_input_records: int = Field(default=200000, ge=1, strict=True)


class SpanRef(Record):
    buffer_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)

    @model_validator(mode='after')
    def ordered(self) -> SpanRef:
        if self.char_end < self.char_start:
            raise ValueError('Invalid source span ordering.')
        return self

    @classmethod
    def from_evidence(cls, value: EvidenceSpan) -> SpanRef:
        return cls(**value.model_dump())

    def to_evidence(self) -> EvidenceSpan:
        return EvidenceSpan(**self.model_dump())


class RoleCandidate(Record):
    role: str
    evidence: SpanRef
    basis: Literal["literal_cue_only"] = "literal_cue_only"
    relationship: Literal["unresolved"] = "unresolved"


class ContextCandidate(Record):
    region_id: str
    evidence: SpanRef
    page_numbers: tuple[int, ...]
    section_ids: tuple[str, ...]
    kind: Literal["adjacent_qualification_candidate", "adjacent_referent_candidate", "explicit_section_reference_candidate"]
    link_evidence: tuple[SpanRef, ...]
    relationship: Literal["unresolved"] = "unresolved"


class ContextOverflow(Record):
    candidate_regions: int
    required_characters: int
    candidate_set_sha256: str


class SourceConcern(Record):
    concern_id: str
    kind: Literal["evidence_warning", "region_hold", "package_hold", "package_blocker", "source_map_warning",
        "clinical_plan_hold", "multiview_hold"]
    source_record_sha256: str
    region_ids: tuple[str, ...] = ()
    page_numbers: tuple[int, ...] = ()
    evidence: SpanRef | None = None
    blocking: bool


class ConceptProposal(Record):
    proposal_id: str
    category: Category
    proposed_question: str
    candidate_topic: SpanRef | None = None
    candidate_subtopic: SpanRef | None = None
    source_region_ids: tuple[str, ...]
    ownership: tuple[SpanRef, ...]
    page_numbers: tuple[int, ...]
    section_ids: tuple[str, ...]
    potential_roles: tuple[RoleCandidate, ...]
    supporting_context: tuple[ContextCandidate, ...]
    context_overflow: ContextOverflow | None = None
    concern_ids: tuple[str, ...]
    concern_overflow_count: int = 0
    unresolved_relationships: tuple[str, ...]
    status: Status
    duplicate_of: str | None = None
    generation_eligible: Literal[False] = False
    human_source_reviewed: Literal[False] = False
    clinically_approved: Literal[False] = False


class RegionAccounting(Record):
    region_id: str
    original_coverage: str
    state: Literal["proposal_attached", "blocked_source_region", "supporting_context_only",
        "excluded_or_nonclinical", "not_yet_assessed"]
    ownership: SpanRef | None
    proposal_ids: tuple[str, ...]
    context_for: tuple[str, ...]
    reasons: tuple[str, ...]


class Count(Record):
    name: str
    value: int


class ProposalPlan(Record):
    version: Literal["clinical-proposals-v1"] = VERSION
    plan_id: str
    pdf_sha256: str | None
    evidence_document_id: str
    canonical_buffer_id: str
    evidence_fingerprint: str
    inventory_sha256: str
    auxiliary_source_sha256: str
    limits: ProposalLimits
    proposals: tuple[ConceptProposal, ...]
    source_concerns: tuple[SourceConcern, ...]
    regions: tuple[RegionAccounting, ...]
    region_counts: tuple[Count, ...]
    canonical_character_counts: tuple[Count, ...]
    diagnostics: tuple[Count, ...]
    source_warning_ids: tuple[str, ...]
    document_complete: Literal[False] = False
    clinically_approved: Literal[False] = False
    generation_eligible: Literal[False] = False


class ProposalError(ValueError):
    """Only bounded codes cross the API boundary; never source/error contents."""
    def __init__(self, code: Literal["INPUT_BUDGET_EXCEEDED", "SOURCE_IDENTITY_INVALID", "PROPOSAL_IDENTITY_INVALID"]):
        self.code = code
        super().__init__(code)

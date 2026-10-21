"""Opt-in draft processing contracts; no human-review or approval records."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import Field
from clinical_proposals.models import Record, SpanRef

VERSION = 'proposal-editorial-v1'
EligibilityState = Literal['draftable_review', 'relationship_review', 'blocked_source', 'insufficient_evidence', 'duplicate_deferred']


class WorkflowConfig(Record):
    concurrency: int = Field(default=2, ge=1, le=2, strict=True)
    max_requests: int = Field(default=80, ge=1, le=1000, strict=True)
    max_unit_characters: int = Field(default=20000, ge=200, strict=True)
    retry_uncertain_requests: bool = False


class LiteralScope(Record):
    role: str
    evidence: SpanRef
    basis: Literal['literal_consistency_only'] = 'literal_consistency_only'


class ProposalDraftEligibility(Record):
    proposal_id: str
    source_order: int
    state: EligibilityState
    reasons: tuple[str, ...]
    ownership: tuple[SpanRef, ...]
    context: tuple[SpanRef, ...]
    scope: tuple[LiteralScope, ...] = ()
    clinical_question: str
    document_category: str
    human_source_reviewed: Literal[False] = False
    clinically_approved: Literal[False] = False


class UnitResult(Record):
    proposal_id: str
    source_order: int
    status: str
    code: str | None = None
    candidate_key: str | None = None
    candidate_sha256: str | None = None
    resumed: bool = False
    validation: dict[str, Any] | None = None
    audit: dict[str, Any] = Field(default_factory=dict)
    storage: dict[str, Any] | None = None


class ReviewDraft(Record):
    proposal_id: str
    source_order: int
    document: dict[str, Any]
    validation: dict[str, Any]
    # Each independent local package retains its one-chunk envelope/numbering.
    local_package_id: str | None = None


class WorkflowResult(Record):
    status: str
    eligibility: tuple[ProposalDraftEligibility, ...]
    units: tuple[UnitResult, ...]
    drafts: tuple[ReviewDraft, ...]
    diagnostics: dict[str, Any]
    proposal_plan_id: str
    evidence_fingerprint: str
    document_complete: Literal[False] = False
    clinically_approved: Literal[False] = False
    ingestion_allowed: Literal[False] = False


class WorkflowError(ValueError):
    def __init__(self, code: str, *, result: WorkflowResult | None = None):
        # Callers supply only repository-defined codes, never exception messages.
        self.code = code
        self.result = result
        self.diagnostics: dict[str, Any] = {}
        super().__init__(code)


class WorkflowInterrupted(KeyboardInterrupt):
    def __init__(self, result: WorkflowResult):
        self.result = result
        super().__init__('Editorial workflow interrupted; completed work was preserved.')

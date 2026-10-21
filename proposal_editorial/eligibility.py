"""Conservative local syntax gates, never medical entailment or human review.

Only bounded, self-contained descriptive/selection statements and a narrow
explicit recommendation form can advance automatically. Outcome associations,
unbound context, competing populations and source holds remain unscheduled.
"""
from __future__ import annotations

import re
from clinical_proposals.models import ProposalPlan, SpanRef
from src.source_map import SourceDocument
from .models import LiteralScope, ProposalDraftEligibility, WorkflowConfig

_POPULATION = re.compile(r'\b(?:adults|children|patients|participants)(?:\s+with\s+[^.;,\n]+?)?(?=\s+(?:received|receiving|underwent|were|had|should|are|can)\b|[,.;\n]|$)', re.I)
_ENTITY = re.compile(r'\b(?:treatment|procedure)\s+(?!(?:was|is|were|are|for|of|in|and|or|with|selection|decisions)\b)[a-z][\w-]*', re.I)
_EXPLICIT_RECOMMENDATION = re.compile(
    r'\b(?:panel|guideline)\s+(?:suggests|recommends)\s+(?P<intervention>treatment\s+[a-z][\w-]*)'
    r'\s+as\s+a\s+(?P<strength>conditional|weak|strong)\s+recommendation\s+with\s+'
    r'(?P<certainty>(?:very low|low|moderate|high)\s+certainty\s+of\s+evidence)\b', re.I)
_DESCRIPTIVE_ACTION = re.compile(r'\b(?:received|underwent|eligibility assessment|diagnostic test|contraindicat\w*)\b', re.I)
_ASSERTION_HAZARD = re.compile(r'\b(?:caus\w*|cure\w*|equivalen\w*|superior\w*|noninferior\w*|significan\w*|benefit\w*)\b', re.I)


def assess_eligibility(source: SourceDocument, proposals: ProposalPlan,
                       config: WorkflowConfig) -> tuple[ProposalDraftEligibility, ...]:
    """Call only after current-source proposal reconstruction by orchestration."""
    doc = source.evidence
    from src.adaptive_ingestion import _document_type
    document_category = _document_type(doc.buffers[doc.canonical_buffer_id].text)
    result = []
    planned = 0
    for index, proposal in enumerate(proposals.proposals):
        spans = proposal.ownership
        context = tuple(c.evidence for c in proposal.supporting_context)
        value = '\n\n'.join(doc.buffers[s.buffer_id].text[s.char_start:s.char_end] for s in spans)
        reasons = []
        scope = []
        state = 'draftable_review'
        if proposal.status == 'blocked_source_evidence':
            state, reasons = 'blocked_source', ['existing_source_hold']
        elif proposal.duplicate_of or proposal.status == 'duplicate_proposal':
            state, reasons = 'duplicate_deferred', ['duplicate_proposal']
        elif proposal.status == 'insufficient_evidence' or not spans or not value.strip():
            state, reasons = 'insufficient_evidence', ['independent_source_evidence_unavailable']
        elif context or any(r in proposal.unresolved_relationships for r in (
            'cross_page_relationship_unresolved', 'missing_or_ambiguous_explicit_context_reference',
            'article_vs_cited_study_attribution_unresolved', 'multiple_endpoint_relationships_unresolved')):
            state, reasons = 'relationship_review', ['essential_context_or_relationship_unresolved']
        elif proposal.category in {'primary_outcome', 'secondary_outcome', 'comparative_outcome', 'safety', 'source_statement'}:
            state, reasons = 'relationship_review', ['outcome_relationship_requires_independent_review']
        else:
            def anchor(a, b):
                # A2 v1 supplies one whole canonical owner. Never join source
                # fragments to establish a population/arm/outcome relationship.
                return SpanRef(buffer_id=spans[0].buffer_id, char_start=spans[0].char_start+a,
                    char_end=spans[0].char_start+b)
            populations = list(_POPULATION.finditer(value))
            population_values = {m[0].casefold().strip() for m in populations}
            entities = list(_ENTITY.finditer(value))
            entity_values = {m[0].casefold() for m in entities}
            roles = {r.role for r in proposal.potential_roles}
            if len(spans) != 1 or len(population_values) != 1 or len(entity_values) != 1:
                state, reasons = 'relationship_review', ['population_or_intervention_scope_unresolved']
            elif roles.intersection({'comparator', 'arm', 'primary_outcome', 'secondary_outcome', 'outcome'}) or _ASSERTION_HAZARD.search(value):
                state, reasons = 'relationship_review', ['clinical_association_not_safe_to_formulate_automatically']
            else:
                # Literal anchors feed consistency checks only. They do not
                # become ReviewedQuestion/SourceReview or a clinical certificate.
                scope.extend((LiteralScope(role='population', evidence=anchor(populations[0].start(),populations[0].end())),
                    LiteralScope(role='intervention', evidence=anchor(entities[0].start(),entities[0].end())),
                    LiteralScope(role='topic', evidence=anchor(entities[0].start(),entities[0].end()))))
                numerics = [r.evidence for r in proposal.potential_roles if r.role == 'numerical_finding']
                times = [r.evidence for r in proposal.potential_roles if r.role == 'time_point']
                if any(not any(t.char_start <= n.char_start < n.char_end <= t.char_end for t in times) for n in numerics):
                    state, reasons = 'relationship_review', ['non_time_numeric_association_unresolved']
                elif proposal.category == 'recommendation':
                    matches = list(_EXPLICIT_RECOMMENDATION.finditer(value))
                    if len(matches) != 1 or len(re.findall(r'\b(?:suggests|recommends)\b',value,re.I)) != 1:
                        state, reasons = 'relationship_review', ['recommendation_attribution_or_strength_unresolved']
                    else:
                        m = matches[0]
                        scope += [LiteralScope(role=k,evidence=anchor(*m.span(g))) for k,g in (
                            ('recommendation_strength','strength'),('evidence_certainty','certainty'))]
                elif not _DESCRIPTIVE_ACTION.search(value) or 'recommendation' in roles:
                    state, reasons = 'relationship_review', ['descriptive_scope_not_explicit']
                for r in proposal.potential_roles:
                    if r.role in {'time_point', 'qualification', 'limitation', 'applicability'}:
                        scope.append(LiteralScope(role=r.role, evidence=r.evidence))
        size = sum(s.char_end-s.char_start for s in spans+context)
        if state == 'draftable_review' and size > config.max_unit_characters:
            state, reasons = 'duplicate_deferred', ['source_context_budget_exceeded']
        if state == 'draftable_review' and planned >= config.max_requests:
            state, reasons = 'duplicate_deferred', ['generation_request_budget_exceeded']
        if state == 'draftable_review':
            planned += 1
        result.append(ProposalDraftEligibility(proposal_id=proposal.proposal_id, source_order=index,
            state=state, reasons=tuple(reasons or ['source_bounded_unreviewed_draft_only']), ownership=spans,
            context=context, scope=tuple(scope), clinical_question=proposal.proposed_question,
            document_category=document_category))
    return tuple(result)

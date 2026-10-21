"""Deterministic clinical question proposals, not source-review attestations."""
from __future__ import annotations

from collections import Counter, defaultdict
import re

from src.concept_planner import MENTIONS
from src.document_concepts import DocumentConceptPlan, SourceRegion
from src.evidence import text_hash
from src.source_map import HEADING_KEYWORDS, SourceDocument
from .models import (VERSION, Category, ConceptProposal, ContextCandidate, ContextOverflow,
    Count, ProposalError, ProposalLimits, ProposalPlan, RegionAccounting, RoleCandidate, SpanRef)
from .source_adapter import digest, pin_inputs, source_concerns


_SELECTORS = {
    'procedure_selection': re.compile(r'\b(?:procedure selection|select\w*\s+(?:a\s+)?procedure|indication\w*\s+for\s+(?:the\s+)?procedure)\b', re.I),
    'contraindication': re.compile(r'\bcontraindicat\w*\b', re.I),
    'diagnostic_selection': re.compile(r'\b(?:diagnostic selection|diagnostic test|diagnostic criteria)\b', re.I),
}
_TOPIC = re.compile(r'\b(?:condition|disease|treatment|procedure)\s+(?!(?:was|is|were|are|for|of|in|and|or|with|selection)\b)[a-z][\w-]*', re.I)
_CERTAINTY = re.compile(r'\b(?:very low|low|moderate|high)[ -]certainty(?:\s+of\s+evidence)?\b', re.I)
_ATTRIBUTION = re.compile(r'\b(?:cited|previous study|prior study|according to|et al)\b', re.I)
_REFERENCE = re.compile(r'\b(?:see|defined in|described in)\s+(?:the\s+)?(' +
    '|'.join(re.escape(t) for t in sorted(HEADING_KEYWORDS, key=lambda t: (-len(t), t))) + r')\b', re.I)
_MENTIONS = {k: re.compile(v, re.I) for k, v in MENTIONS.items()}
_ROLE_MAP = {'group': 'arm', 'timeframe': 'time_point', 'endpoint': 'outcome'}
_QUALIFIERS = {'qualification', 'safety', 'limitation', 'applicability'}
_QUESTIONS = {
    'study_scope': 'What study design and population are described, and which relationships need review?',
    'primary_outcome': 'What primary outcome is described, with what population, arms, time point and qualifications?',
    'secondary_outcome': 'What secondary outcome is described, with what population, arms, time point and qualifications?',
    'comparative_outcome': 'What comparison is described, and are its arms, outcomes and time points explicitly linked?',
    'safety': 'What safety findings and ascertainment limitations are described?',
    'recommendation': 'What recommendation, strength, certainty and applicability are described?',
    'applicability': 'What limitations or applicability restrictions are described?',
    'procedure_selection': 'What source-supported procedure selection criteria are described?',
    'contraindication': 'What contraindications and qualifying conditions are described?',
    'diagnostic_selection': 'What diagnostic selection criteria and limitations are described?',
    'source_statement': 'What clinical question, if any, does this complete source passage support?',
}


def _span(region: SourceRegion, start: int, end: int) -> SpanRef:
    owned = region.ownership
    return SpanRef(buffer_id=owned.buffer_id, char_start=owned.char_start + start, char_end=owned.char_start + end)


def _roles(region: SourceRegion, text: str, limit: int) -> tuple[RoleCandidate, ...] | None:
    if len(region.cues) > limit:
        return None
    records = {(c.role, c.evidence.char_start, c.evidence.char_end):
        RoleCandidate(role=c.role, evidence=SpanRef.from_evidence(c.evidence)) for c in region.cues}
    if len(records) > limit:
        return None
    patterns = [( _ROLE_MAP.get(k, k), p) for k, p in _MENTIONS.items()]
    patterns += [('topic', _TOPIC), ('evidence_certainty', _CERTAINTY)] + list(_SELECTORS.items())
    for role, pattern in patterns:
        for match in pattern.finditer(text):
            evidence = _span(region, match.start(), match.end())
            records[role, evidence.char_start, evidence.char_end] = RoleCandidate(role=role, evidence=evidence)
            if len(records) > limit:
                return None
    return tuple(records[key] for key in sorted(records, key=lambda k: (k[1], k[2], k[0])))


def _categories(region: SourceRegion, roles: tuple[RoleCandidate, ...]) -> list[Category]:
    kinds = {r.role for r in roles}
    categories = [q for q in ('primary_outcome', 'secondary_outcome', 'safety', 'recommendation') if q in kinds]
    if kinds.intersection({'limitation', 'applicability'}):
        categories.append('applicability')
    categories += [q for q in _SELECTORS if q in kinds]
    if not categories and {'comparator', 'outcome'} <= kinds:
        categories.append('comparative_outcome')
    if not categories and kinds.intersection({'study_design', 'population', 'intervention'}):
        categories.append('study_scope')
    return categories or (['source_statement'] if kinds else [])


def _counts(values: Counter) -> tuple[Count, ...]:
    return tuple(Count(name=k, value=values[k]) for k in sorted(values))


def propose_concepts(source: SourceDocument, inventory: DocumentConceptPlan, *,
                     limits: ProposalLimits | None = None) -> ProposalPlan:
    """Return evidence-bound, offline proposals. Never schedule generation or write.

    Topic/subtopic and roles are literal source span candidates, not verified
    clinical labels. Context is unbound and cannot transfer findings or roles.
    Input/source failures use redacted codes; partial budgets stay explicit.
    """
    limits = limits or ProposalLimits()
    if not isinstance(limits, ProposalLimits):
        raise ProposalError('INPUT_BUDGET_EXCEEDED')
    source, inventory, inventory_hash, auxiliary_hash = pin_inputs(source, inventory, limits)
    doc = source.evidence
    text = doc.buffers[doc.canonical_buffer_id].text
    concerns = source_concerns(source, inventory)
    by_id = {r.region_id: r for r in inventory.regions}
    canonical = sorted((r for r in inventory.regions if r.ownership),
        key=lambda r: (r.ownership.char_start, r.ownership.char_end, r.region_id))
    positions = {r.region_id: i for i, r in enumerate(canonical)}
    sections = {s.section_id: 'section:' + text_hash(s.section_id) for s in source.sections}
    section_kinds = {sections[s.section_id]: s.kind for s in source.sections}
    titles = defaultdict(set)
    nonclinical = set()
    section_regions = defaultdict(list)
    for s in source.sections:
        titles[s.title.casefold().strip().rstrip(':')].add(sections[s.section_id])
    for r in canonical:
        for sid in r.section_ids:
            section_regions[sid].append(r.region_id)
        if r.section_ids and all(section_kinds[sid] == 'references' for sid in r.section_ids):
            nonclinical.add(r.region_id)
    local_concerns, page_concerns = defaultdict(list), defaultdict(list)
    document_concerns = []
    for c in concerns:
        if c.region_ids:
            for rid in c.region_ids:
                local_concerns[rid].append(c)
        elif c.page_numbers:
            for page in c.page_numbers:
                page_concerns[page].append(c)
        else:
            document_concerns.append(c)

    def related(region):
        found = {c.concern_id: c for c in document_concerns + local_concerns[region.region_id]}
        for p in region.page_numbers:
            found.update((c.concern_id, c) for c in page_concerns[p])
        return found

    def context_candidates(region, value):
        contexts = {}
        reasons = []
        i = positions[region.region_id]
        for n in canonical[max(0, i - 1):i + 2]:
            if n.region_id == region.region_id or n.region_id in nonclinical:
                continue
            if {c.role for c in n.cues}.intersection(_QUALIFIERS):
                contexts[n.region_id] = ('adjacent_qualification_candidate', (SpanRef.from_evidence(n.ownership),))
            elif 'referent_requires_source_review' in region.reasons and n.cues:
                contexts[n.region_id] = ('adjacent_referent_candidate', (SpanRef.from_evidence(region.ownership),))
        for match in _REFERENCE.finditer(value):
            targets = titles[match[1].casefold()]
            if len(targets) != 1:
                reasons.append('missing_or_ambiguous_explicit_context_reference')
                continue
            sid = next(iter(targets))
            for rid in section_regions[sid]:
                if rid != region.region_id and rid not in nonclinical:
                    contexts[rid] = ('explicit_section_reference_candidate', (_span(region, match.start(), match.end()),))
        ordered = sorted(contexts, key=lambda rid: (by_id[rid].ownership.char_start, rid))
        characters = region.ownership.char_end - region.ownership.char_start + sum(
            by_id[rid].ownership.char_end - by_id[rid].ownership.char_start for rid in ordered)
        if len(ordered) > limits.max_context_regions or characters > limits.max_context_characters:
            return (), ContextOverflow(candidate_regions=len(ordered), required_characters=characters,
                candidate_set_sha256=digest([(rid, contexts[rid][0], [s.model_dump() for s in contexts[rid][1]]) for rid in ordered])), reasons
        records = tuple(ContextCandidate(region_id=rid, evidence=SpanRef.from_evidence(by_id[rid].ownership),
            page_numbers=tuple(by_id[rid].page_numbers), section_ids=tuple(by_id[rid].section_ids),
            kind=contexts[rid][0], link_evidence=contexts[rid][1]) for rid in ordered)
        return records, None, reasons

    seed = {'version': VERSION, 'source': inventory.evidence_fingerprint, 'inventory': inventory_hash,
        'auxiliary': auxiliary_hash, 'limits': limits.model_dump(mode='json')}
    proposals = []
    duplicates = {}
    attached, context_for, extra_reasons = defaultdict(list), defaultdict(list), defaultdict(list)
    metrics = Counter(input_regions=len(inventory.regions), source_validation_rebuilds=1,
        package_validation_rebuilds=int(source.source_package_plan is not None), proposal_region_scans=0,
        clinical_validation_rebuilds=int(source.clinical_plan is not None),
        multiview_validation_rebuilds=int(source.multiview_selection is not None),
        proposal_characters_scanned=0, candidate_questions=0, omitted_questions_by_limit=0,
        context_budget_holds=0, region_budget_holds=0, role_budget_holds=0, duplicate_proposals=0,
        concern_link_budget_holds=0)
    for r in canonical:
        if r.coverage in {'intentionally_excluded', 'supporting_context_only'} or r.region_id in nonclinical:
            continue
        size = r.ownership.char_end - r.ownership.char_start
        # Whole-region holds, never fixed-size splitting or partial passages.
        if size > limits.max_region_characters:
            roles, value = None, ''
            reasons = ['region_character_budget_exceeded']
            metrics['region_budget_holds'] += 1
        else:
            value = text[r.ownership.char_start:r.ownership.char_end]
            metrics['proposal_region_scans'] += 1
            metrics['proposal_characters_scanned'] += len(value)
            roles = _roles(r, value, limits.max_role_candidates)
            reasons = [] if roles is not None else ['role_candidate_budget_exceeded']
            metrics['role_budget_holds'] += int(roles is None)
        categories = _categories(r, roles) if roles is not None else ['source_statement']
        if not categories:
            extra_reasons[r.region_id].append('no_clinical_question_cue')
            continue
        metrics['candidate_questions'] += len(categories)
        if len(proposals) >= limits.max_proposals:
            metrics['omitted_questions_by_limit'] += len(categories)
            extra_reasons[r.region_id].append('proposal_count_budget_exceeded')
            continue
        contexts, overflow, context_reasons = context_candidates(r, value)
        found = related(r)
        for c in contexts:
            found.update(related(by_id[c.region_id]))
        unresolved = set(r.reasons + reasons + context_reasons + ['clinical_relationships_not_verified'])
        kinds = {m.role for m in roles or ()}
        if contexts:
            unresolved.add('context_relationships_unresolved')
        if any(set(c.page_numbers) != set(r.page_numbers) for c in contexts):
            unresolved.add('cross_page_relationship_unresolved')
        if {'primary_outcome', 'secondary_outcome'} <= kinds:
            unresolved.add('multiple_endpoint_relationships_unresolved')
        if 'numerical_finding' in kinds:
            unresolved.add('numeric_associations_unresolved')
        if _ATTRIBUTION.search(value):
            unresolved.add('article_vs_cited_study_attribution_unresolved')
        if overflow:
            unresolved.add('context_budget_exceeded')
        concern_overflow = len(found) if len(found) > limits.max_concern_links else 0
        if concern_overflow:
            unresolved.add('concern_link_budget_exceeded')
        topic_spans = tuple(dict.fromkeys(m.evidence for m in roles or () if m.role == 'topic'))
        population_spans = tuple(dict.fromkeys(m.evidence for m in roles or () if m.role == 'population'))
        if len(topic_spans) != 1:
            unresolved.add('topic_label_unresolved')
        if len(population_spans) != 1:
            unresolved.add('subtopic_label_unresolved')
        for category in categories:
            if len(proposals) >= limits.max_proposals:
                metrics['omitted_questions_by_limit'] += 1
                extra_reasons[r.region_id].append('proposal_count_budget_exceeded')
                continue
            pid = 'proposal:' + digest(dict(seed, category=category, ownership=r.ownership.model_dump(),
                region_id=r.region_id, context=[c.model_dump(mode='json') for c in contexts],
                overflow=overflow.model_dump() if overflow else None))
            # Exact local repetitions only. A changed population, source page,
            # section, context or uncertainty is never semantically deduplicated.
            key = digest([category, text_hash(value.strip()), r.page_numbers, r.section_ids,
                [c.model_dump(mode='json') for c in contexts], sorted(unresolved)]) if value and roles else None
            duplicate_of = duplicates.get(key) if key else None
            blocked = r.coverage == 'held_for_source_review' or any(c.blocking for c in found.values())
            status = 'needs_relationship_review'
            if blocked or overflow or reasons or concern_overflow:
                status = 'blocked_source_evidence'
            elif duplicate_of:
                status = 'duplicate_proposal'
            elif kinds <= {'numerical_finding', 'qualification', 'time_point'}:
                status = 'insufficient_evidence'
            elif category == 'study_scope' and kinds <= {'study_design'}:
                status = 'proposed'
            proposal = ConceptProposal(proposal_id=pid, category=category, proposed_question=_QUESTIONS[category],
                candidate_topic=topic_spans[0] if len(topic_spans) == 1 else None,
                candidate_subtopic=population_spans[0] if len(population_spans) == 1 else None,
                source_region_ids=(r.region_id,), ownership=(SpanRef.from_evidence(r.ownership),),
                page_numbers=tuple(r.page_numbers), section_ids=tuple(r.section_ids), potential_roles=roles or (),
                supporting_context=contexts, context_overflow=overflow,
                concern_ids=() if concern_overflow else tuple(sorted(found)), concern_overflow_count=concern_overflow,
                unresolved_relationships=tuple(sorted(unresolved)), status=status, duplicate_of=duplicate_of)
            proposals.append(proposal)
            if key and key not in duplicates:
                duplicates[key] = pid
            metrics['duplicate_proposals'] += int(duplicate_of is not None)
            metrics['context_budget_holds'] += int(overflow is not None)
            metrics['concern_link_budget_holds'] += int(bool(concern_overflow))
            attached[r.region_id].append(pid)
            for c in contexts:
                context_for[c.region_id].append(pid)

    accounting = []
    for r in inventory.regions:
        state = 'not_yet_assessed'
        reasons = list(r.reasons) + extra_reasons[r.region_id]
        if r.coverage == 'held_for_source_review' or any(c.blocking for c in related(r).values()):
            state = 'blocked_source_region'
        elif r.coverage == 'intentionally_excluded' or r.region_id in nonclinical:
            state = 'excluded_or_nonclinical'
            if r.region_id in nonclinical:
                reasons.append('source_reference_section_no_article_relationship_assumed')
        elif attached[r.region_id]:
            state = 'proposal_attached'
        elif r.coverage == 'supporting_context_only' or context_for[r.region_id]:
            state = 'supporting_context_only'
        accounting.append(RegionAccounting(region_id=r.region_id, original_coverage=r.coverage, state=state,
            ownership=SpanRef.from_evidence(r.ownership) if r.ownership else None,
            proposal_ids=tuple(attached[r.region_id]), context_for=tuple(context_for[r.region_id]), reasons=tuple(sorted(set(reasons)))))
    metrics['proposals_emitted'] = len(proposals)
    metrics['unresolved_candidate_relationships'] = sum(len(p.unresolved_relationships) for p in proposals)
    region_counts = Counter(r.state for r in accounting)
    character_counts = Counter()
    for r in accounting:
        if r.ownership:
            character_counts[r.state] += r.ownership.char_end - r.ownership.char_start
    if sum(character_counts.values()) != len(text):
        raise ProposalError('SOURCE_IDENTITY_INVALID')
    payload = dict(seed, proposals=[p.model_dump(mode='json') for p in proposals],
        accounting=[a.model_dump(mode='json') for a in accounting])
    return ProposalPlan(plan_id='proposal-plan:' + digest(payload), pdf_sha256=doc.pdf_sha256,
        evidence_document_id=doc.document_id, canonical_buffer_id=doc.canonical_buffer_id,
        evidence_fingerprint=inventory.evidence_fingerprint, inventory_sha256=inventory_hash,
        auxiliary_source_sha256=auxiliary_hash, limits=limits, proposals=tuple(proposals), source_concerns=concerns,
        regions=tuple(accounting), region_counts=_counts(region_counts), canonical_character_counts=_counts(character_counts),
        diagnostics=_counts(metrics), source_warning_ids=tuple(inventory.source_warning_ids))


def validate_proposal_plan(plan: ProposalPlan, source: SourceDocument, inventory: DocumentConceptPlan) -> None:
    """Rebuild against current source/configuration; never trust a saved identity."""
    if not isinstance(plan, ProposalPlan):
        raise ProposalError('PROPOSAL_IDENTITY_INVALID')
    expected = propose_concepts(source, inventory, limits=plan.limits)
    if expected != plan:
        raise ProposalError('PROPOSAL_IDENTITY_INVALID')

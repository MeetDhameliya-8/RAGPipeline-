"""Source-anchored candidate planning. Literal evidence, not inferred medicine."""
from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import re
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel, ConfigDict, Field

from .evidence import (EvidenceDocument, EvidenceSpan, EvidenceTrace, TraceFragment,
    _check_span, span, text_hash, validate_evidence, warning_blocks_coverage)
from .draft_support import NativeSupportIndex, NativeSupportProof

if TYPE_CHECKING:
    from .source_map import SourceDocument

VERSION = "source-concepts-v2-local-drafts"
CLINICAL = re.compile(r"\b(patients?|adults?|children|participants?|treatment|intervention|endpoint|outcome|response|recommend\w*|should|mortality|survival|remission|adverse|dose|randomi[sz]\w*|received|groups?|arms?|therapy|disease|procedure|trial)\b", re.I)
REFERENCE = re.compile(r"\b(table|figure|fig\.)\s*(\d+[A-Za-z]?)", re.I)
ANAPHORA = re.compile(r"\b(these patients|these participants|this treatment|those patients|as (?:shown|described) above|respectively|the former|the latter)\b", re.I)
MENTIONS = {
    "population": r"\b(?:patients|participants|adults|children)(?:\s+with\s+[^.;,\n]+)?",
    "intervention": r"\b(?:received|treated with|assigned to|randomized to)\s+[^.;,\n]+",
    "group": r"\b(?:group|arm)\s+[A-Za-z0-9][\w-]*",
    "timeframe": r"\b\d+(?:\.\d+)?\s*(?:days?|weeks?|months?|years?)\b",
    "endpoint": r"\b(?:primary|secondary)\s+(?:endpoint|outcome)\b[^.;\n]*",
}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMention(Contract):
    kind: str
    evidence: EvidenceSpan
    relationship: Literal["source_literal_only"] = "source_literal_only"


class EvidenceDependency(Contract):
    kind: Literal["table", "figure", "referent", "neighbor_context"]
    status: Literal["available_context", "unresolved", "missing"]
    source: EvidenceSpan
    supporting_ids: list[str] = Field(default_factory=list)


class WarningDecision(Contract):
    warning_id: str
    decision: Literal["confirmed_affected", "uncertain_dependency"]
    scope: Literal["owned_evidence", "supporting_context", "page", "document"]


class ClinicalConcept(Contract):
    concept_id: str
    ownership: list[EvidenceSpan]
    section_ids: list[str]
    page_numbers: list[int]
    evidence_ids: list[str] = Field(default_factory=list)
    section_relationship: Literal["raw_overlap_candidates"] = "raw_overlap_candidates"
    source_precision: Literal["exact", "region", "unmapped"] = "unmapped"
    role: Literal["clinical_proposition", "source_heading", "table_caption", "figure_caption", "table_note", "unclassified_source"]
    supporting_ids: list[str] = Field(default_factory=list)
    mentions: list[SourceMention] = Field(default_factory=list)
    dependencies: list[EvidenceDependency] = Field(default_factory=list)
    qualifications: list[EvidenceSpan] = Field(default_factory=list)
    unresolved_relationships: list[str] = Field(default_factory=list)
    warning_decisions: list[WarningDecision] = Field(default_factory=list)
    draft_support: NativeSupportProof | None = None
    independent_warning_ids: list[str] = Field(default_factory=list)
    structural_context_ids: list[str] = Field(default_factory=list)
    generation_eligibility: Literal["eligible_draft", "blocked", "context_only"]
    block_reasons: list[str] = Field(default_factory=list)
    coverage_status: Literal["uncovered", "context_only", "requires_adjudication"]


class ClinicalUnit(Contract):
    unit_id: str
    concept_ids: list[str]
    context_ids: list[str]
    ownership: list[EvidenceSpan]
    generation_eligibility: Literal["eligible_draft", "blocked"]
    dependency_decision: Literal["confirmed_affected", "uncertain_dependency", "independently_supported"]
    block_reasons: list[str]
    warning_decisions: list[WarningDecision]
    request_characters: int = 0


class ClinicalPlan(Contract):
    version: str = VERSION
    plan_id: str
    evidence_document_id: str
    evidence_config_hash: str
    buffer_id: str
    concepts: list[ClinicalConcept]
    units: list[ClinicalUnit]
    max_unit_chars: int
    document_complete: bool = False
    document_warning_ids: list[str]
    source_accounting: dict[str, int]
    blockers: list[str] = Field(default_factory=list)
    deduplicated_occurrences: int = 0


class EvidenceIndex:
    """One reusable graph index; no evidence graph or buffers copied per unit."""
    def __init__(self, doc: EvidenceDocument):
        self.doc = doc
        self.maps = {}
        self.ends = {}
        for key, mappings in doc.mappings.items():
            records = [m for m in mappings if m.target.char_end > m.target.char_start]
            self.maps[key] = records
            self.ends[key] = [m.target.char_end for m in records]

    def trace(self, owned: EvidenceSpan) -> EvidenceTrace:
        result = EvidenceTrace()
        def walk(current, precision, visited):
            _check_span(self.doc, current)
            if current.buffer_id in visited:
                raise ValueError("Cyclic evidence mapping.")
            if current.char_start == current.char_end:
                return
            records = self.maps.get(current.buffer_id)
            if records is None:
                if self.doc.buffers[current.buffer_id].stage == "raw":
                    result.fragments.append(TraceFragment(source=current, precision=precision))
                else:
                    result.unmapped_spans.append(current)
                return
            position = bisect_right(self.ends[current.buffer_id], current.char_start)
            while position < len(records):
                mapping = records[position]
                if mapping.target.char_start >= current.char_end:
                    break
                a, b = max(current.char_start, mapping.target.char_start), min(current.char_end, mapping.target.char_end)
                if a < b:
                    if mapping.source is None:
                        result.synthetic_spans.append(span(current.buffer_id, a, b))
                    else:
                        source = mapping.source
                        if mapping.relation == "exact":
                            offset = a - mapping.target.char_start
                            source = span(source.buffer_id, source.char_start+offset, source.char_start+offset+b-a)
                        walk(source, "region" if mapping.relation != "exact" else precision, visited | {current.buffer_id})
                position += 1
        walk(owned, "exact", frozenset())
        unique = {(f.source.buffer_id, f.source.char_start, f.source.char_end, f.precision): f for f in result.fragments}
        result.fragments = list(unique.values())
        return result

    def pages(self, trace: EvidenceTrace) -> list[int]:
        return sorted({p for f in trace.fragments if (p := self.doc.buffers[f.source.buffer_id].page_number) is not None})


def _overlap(a: EvidenceSpan, b: EvidenceSpan) -> bool:
    return a.buffer_id == b.buffer_id and a.char_start < b.char_end and b.char_start < a.char_end


def _atoms(text: str):
    """Partition every character, including separators; never split table rows."""
    starts = [0] + [m.end() for m in re.finditer(r"\n\s*\n+", text)]
    if starts[-1] != len(text):
        starts.append(len(text))
    for a,b in zip(starts, starts[1:]):
        paragraph = text[a:b]
        if re.match(r"\s*(?:table|figure|fig\.)\s*\d|\s*[*†‡]|\s*notes?\s*:", paragraph, re.I):
            yield a,b
            continue
        cuts = [a]
        for m in re.finditer(r"[.!?][\"')]*\s+(?=[A-Z])", paragraph):
            preceding = re.search(r"([A-Za-z]+)\.$", paragraph[:m.start()+1])
            if preceding and len(preceding.group(1)) <= 3:
                continue  # Keep abbreviated names/comparators with their qualification.
            cuts.append(a+m.end())
        if cuts[-1] != b:
            cuts.append(b)
        yield from zip(cuts, cuts[1:])


def _role(value: str) -> str:
    from .source_map import _looks_like_heading
    value = value.strip()
    match = REFERENCE.match(value)
    if match:
        return "table_caption" if match.group(1).lower() == "table" else "figure_caption"
    if re.match(r"[*†‡]|notes?\s*:", value, re.I):
        return "table_note"
    if _looks_like_heading(value):
        return "source_heading"
    return "clinical_proposition" if CLINICAL.search(value) else "unclassified_source"


def build_clinical_plan(extraction: dict, source_doc: SourceDocument, *, max_unit_chars: int = 20000, max_units: int = 80) -> ClinicalPlan:
    doc = source_doc.evidence
    if doc is None:
        raise ValueError("A source map with evidence is required; build_source_map supports legacy inputs.")
    validate_evidence(doc)
    text = doc.buffers[doc.canonical_buffer_id].text
    if text != str(extraction.get("full_text") or ""):
        raise ValueError("Clinical plan source differs from canonical evidence.")
    if max_unit_chars < 200 or max_units < 1:
        raise ValueError("Invalid clinical planning budget.")
    index = EvidenceIndex(doc)
    native_support = NativeSupportIndex(doc)
    # Section bounds are candidate associations only, not fabricated semantic links.
    section_ranges = defaultdict(list)
    for section in source_doc.sections:
        by_buffer = defaultdict(list)
        for item in section.source_spans:
            by_buffer[item.buffer_id].append(item)
        for key, items in by_buffer.items():
            section_ranges[key].append((min(s.char_start for s in items), max(s.char_end for s in items), section.section_id))
    warnings = [w for w in doc.warnings if warning_blocks_coverage(w)]
    warning_info = []
    for w in warnings:
        raw = index.trace(w.span) if w.span and w.span.char_end > w.span.char_start else EvidenceTrace()
        pages = {w.page_number} if w.page_number else set(index.pages(raw))
        warning_info.append((w,raw,pages))
    concepts, traces, domains, contexts = [], {}, {}, {}
    heading = None
    domain = "document"
    standard = {"methods", "results", "discussion", "conclusions", "conclusion", "background", "introduction", "abstract", "recommendations"}
    donors = defaultdict(dict)
    group_definitions=defaultdict(lambda:defaultdict(list))
    for a,b in _atoms(text):
        value = text[a:b]
        role = _role(value)
        cid = "concept:" + text_hash(f"{VERSION}:{doc.document_id}:{doc.config_hash}:{doc.canonical_buffer_id}:{a}:{b}:{role}")
        owned = span(doc.canonical_buffer_id,a,b)
        trace = index.trace(owned)
        traces[cid] = trace
        sections = set()
        for fragment in trace.fragments:
            item = fragment.source
            for x,y,sid in section_ranges.get(item.buffer_id, []):
                if item.char_start < y and x < item.char_end:
                    sections.add(sid)
        if role == "source_heading":
            heading = cid
            if value.strip().lower().rstrip(":") not in standard:
                domain = cid
        elif role == "unclassified_source" and len(value.split()) <= 10 and not re.search(r"[.!?]",value) and re.search(r"[A-Za-z]",value):
            # Possible layout heading: isolate its domain, but never declare
            # unknown short material nonclinical or completed coverage.
            domain=cid;heading=None
        mentions = [SourceMention(kind=kind, evidence=span(doc.canonical_buffer_id,a+m.start(),a+m.end()))
                    for kind, expression in MENTIONS.items() for m in re.finditer(expression,value,re.I)]
        context_ids = [heading] if heading and heading != cid else []
        scope_reasons=[]
        scope_dependencies=[]
        if role == "clinical_proposition":
            own_kinds = {m.kind for m in mentions}
            group_references=[m for m in mentions if m.kind=="group"] if "intervention" not in own_kinds else []
            for mention in group_references:
                label=text[mention.evidence.char_start:mention.evidence.char_end].lower()
                definitions=group_definitions[domain][label]
                scope_dependencies.append(EvidenceDependency(kind="referent",status="available_context" if len(definitions)==1 else "unresolved",
                    source=mention.evidence,supporting_ids=list(definitions)))
                context_ids.extend(x for x in definitions if x not in context_ids)
                if len(definitions)!=1:scope_reasons.append("group_definition_unresolved")
            for kind in ("population", "intervention", "endpoint"):
                if group_references and kind in {"population","intervention"}:continue
                donor = donors[domain].get(kind)
                if kind not in own_kinds and donor and donor not in context_ids:
                    context_ids.append(donor)
            if concepts and domains.get(concepts[-1].concept_id)==domain and re.match(r"\s*(however|in contrast|conversely|although|except|only)\b",value,re.I):
                if concepts[-1].concept_id not in context_ids:context_ids.append(concepts[-1].concept_id)
        reasons = list(scope_reasons)
        if doc.identity_quality != "pdf_bytes":
            reasons.append("pdf_identity_unverified")
        if not trace.fragments or trace.unmapped_spans:
            reasons.append("source_mapping_unresolved")
        if not sections:
            reasons.append("section_association_unresolved")
        if any(f.precision == "region" and doc.buffers[f.source.buffer_id].text[f.source.char_start:f.source.char_end].strip() for f in trace.fragments):
            reasons.append("nonexact_source_region")
        dependencies = list(scope_dependencies)
        if ANAPHORA.search(value):
            dependencies.append(EvidenceDependency(kind="referent",status="unresolved",source=owned))
            reasons.append("unresolved_referent")
        if role in {"table_caption", "figure_caption", "table_note"}:
            reasons.append("unresolved_structured_evidence")
        if role == "unclassified_source":
            reasons.append("clinical_role_unclassified")
        concept = ClinicalConcept(concept_id=cid,ownership=[owned],section_ids=sorted(sections),page_numbers=index.pages(trace),
            evidence_ids=list(dict.fromkeys(f.source.buffer_id for f in trace.fragments)),
            source_precision="unmapped" if not trace.fragments or trace.unmapped_spans else "region" if any(f.precision=="region" for f in trace.fragments) else "exact",
            role=role, supporting_ids=list(context_ids),mentions=mentions,dependencies=dependencies,
            qualifications=[owned] if re.search(r"\b(only|except|not|may|suggest|certainty|conditional|limited|excluding)\b",value,re.I) else [],
            generation_eligibility="context_only" if role=="source_heading" else "blocked" if reasons else "eligible_draft",
            block_reasons=reasons,coverage_status="context_only" if role=="source_heading" else "requires_adjudication" if reasons else "uncovered")
        concepts.append(concept)
        contexts[cid] = context_ids
        domains[cid] = domain
        if role == "clinical_proposition":
            for mention in mentions:
                if mention.kind in {"population","intervention","endpoint"}:
                    donors[domain][mention.kind] = cid
            if any(m.kind=="intervention" for m in mentions):
                for mention in mentions:
                    if mention.kind=="group":
                        group_definitions[domain][text[mention.evidence.char_start:mention.evidence.char_end].lower()].append(cid)
    by_id = {c.concept_id:c for c in concepts}
    captions = defaultdict(list)
    for c in concepts:
        if c.role in {"table_caption","figure_caption"}:
            m = REFERENCE.match(text[c.ownership[0].char_start:c.ownership[0].char_end].strip())
            if m:
                captions[("table" if m.group(1).lower()=="table" else "figure", m.group(2).lower())].append(c.concept_id)
    for position,c in enumerate(concepts):
        owned = c.ownership[0]
        value = text[owned.char_start:owned.char_end]
        for m in REFERENCE.finditer(value):
            kind = "table" if m.group(1).lower()=="table" else "figure"
            supporting = list(captions[(kind,m.group(2).lower())])
            for cap_id in list(supporting):
                cap_pos = next(i for i,x in enumerate(concepts) if x.concept_id==cap_id)
                for note in concepts[cap_pos+1:]:
                    if note.role != "table_note":
                        break
                    supporting.append(note.concept_id)
            c.dependencies.append(EvidenceDependency(kind=kind,status="unresolved" if supporting else "missing",
                source=span(doc.canonical_buffer_id,owned.char_start+m.start(),owned.char_start+m.end()),supporting_ids=supporting))
            c.block_reasons.append(f"{kind}_relationship_unresolved")
            contexts[c.concept_id].extend(x for x in supporting if x != c.concept_id and x not in contexts[c.concept_id])
        local_proof = native_support.prove(owned, traces[c.concept_id]) if c.role == "clinical_proposition" and not c.block_reasons else None
        if local_proof:
            # Generic structural headings are classification hints, not missing
            # patient/arm definitions for an explicitly self-contained statement.
            c.structural_context_ids = [x for x in contexts[c.concept_id] if by_id[x].role == "source_heading" and
                text[by_id[x].ownership[0].char_start:by_id[x].ownership[0].char_end].strip().lower().rstrip(":") in standard]
            contexts[c.concept_id] = [x for x in contexts[c.concept_id] if x not in c.structural_context_ids]
        # Include the complete known context closure once, never just the nearest
        # donor while omitting that donor's qualifications or definitions.
        closure=set()
        pending_context=list(contexts[c.concept_id])
        while pending_context:
            target=pending_context.pop()
            if target==c.concept_id or target in closure:continue
            closure.add(target);pending_context.extend(contexts[target])
        contexts[c.concept_id]=sorted(closure,key=lambda x:by_id[x].ownership[0].char_start)
        # Context is required source support, so its corruption propagates.
        support_traces = [(c.concept_id,traces[c.concept_id])] + [(x,traces[x]) for x in contexts[c.concept_id]]
        support_pages = set(c.page_numbers)
        for x,_ in support_traces:
            support_pages.update(by_id[x].page_numbers)
        # An additive local certificate can overcome coarse page uncertainty,
        # never existing clinical/context/provenance block reasons.
        proof = local_proof
        required = [by_id[x] for x in contexts[c.concept_id] if by_id[x].role != "source_heading"]
        support_proofs = [native_support.prove(x.ownership[0], traces[x.concept_id]) for x in required]
        for warning,raw,pages in warning_info:
            direct = warning.span is not None and any(_overlap(warning.span,s) for s in c.ownership)
            linked_context = warning.span is not None and any(_overlap(warning.span,s) for x in contexts[c.concept_id] for s in by_id[x].ownership)
            if not direct and not linked_context and raw.fragments:
                for owner,trace in support_traces:
                    if any(_overlap(a.source,b.source) for a in trace.fragments for b in raw.fragments):
                        if owner==c.concept_id: direct=True
                        else: linked_context=True
                        break
            if direct or linked_context:
                c.warning_decisions.append(WarningDecision(warning_id=warning.warning_id,decision="confirmed_affected",
                    scope="owned_evidence" if direct else "supporting_context"))
            elif pages.intersection(support_pages):
                if proof and all(support_proofs) and all(native_support.warning_independent(p, warning, raw) for p in [proof]+support_proofs):
                    c.draft_support = proof
                    c.independent_warning_ids.append(warning.warning_id)
                else:
                    c.warning_decisions.append(WarningDecision(warning_id=warning.warning_id,decision="uncertain_dependency",scope="page"))
            elif not pages:
                if proof and all(support_proofs) and all(native_support.warning_independent(p, warning, raw) for p in [proof]+support_proofs):
                    c.draft_support = proof
                    c.independent_warning_ids.append(warning.warning_id)
                else:
                    c.warning_decisions.append(WarningDecision(warning_id=warning.warning_id,decision="uncertain_dependency",scope="document"))
        c.supporting_ids = list(dict.fromkeys(contexts[c.concept_id]))
        populations=lambda ids:{text[m.evidence.char_start:m.evidence.char_end].lower() for x in ids for m in by_id[x].mentions if m.kind=="population"}
        own_population=populations([c.concept_id]);context_population=populations(c.supporting_ids)
        if own_population and context_population and not own_population.intersection(context_population):
            c.block_reasons.append("population_context_ambiguous")
        if c.supporting_ids:
            c.dependencies.append(EvidenceDependency(kind="neighbor_context",status="available_context",source=owned,supporting_ids=c.supporting_ids))
        c.unresolved_relationships = sorted({d.kind for d in c.dependencies if d.status != "available_context"})
        if c.warning_decisions:
            c.block_reasons.append("unresolved_evidence_warning")
        c.block_reasons = list(dict.fromkeys(c.block_reasons))
        if c.role != "source_heading" and c.block_reasons:
            c.generation_eligibility="blocked"
            c.coverage_status="requires_adjudication"
    # Context-only material never licenses an unresolved referent or lost qualifier.
    # Context edges normally point backwards; a fixed point also handles caption edges.
    changed=True
    while changed:
        changed=False
        for c in concepts:
            if c.role=="source_heading":continue
            if any(by_id[x].block_reasons for x in c.supporting_ids) and "unresolved_supporting_context" not in c.block_reasons:
                c.block_reasons.append("unresolved_supporting_context");changed=True
            scope_mentions=[m for x in [c.concept_id]+c.supporting_ids for m in by_id[x].mentions if m.kind in {"population","intervention"}]
            if c.role=="clinical_proposition" and not scope_mentions and "clinical_scope_unresolved" not in c.block_reasons:
                c.block_reasons.append("clinical_scope_unresolved");changed=True
            if c.block_reasons:
                c.generation_eligibility="blocked";c.coverage_status="requires_adjudication"
    # Deduplicate only exact supported propositions under identical source
    # section/context. Every original occurrence stays explicitly owned.
    signatures={};aliases={};kept=[];deduplicated=0
    for c in concepts:
        key=(tuple(c.section_ids),domains[c.concept_id],tuple(c.supporting_ids),
             re.sub(r"\s+"," ",text[c.ownership[0].char_start:c.ownership[0].char_end]).strip())
        primary=signatures.get(key) if c.role=="clinical_proposition" and c.generation_eligibility=="eligible_draft" else None
        if primary is not None:
            primary.ownership.extend(c.ownership);primary.mentions.extend(c.mentions);primary.qualifications.extend(c.qualifications)
            primary.dependencies.extend(c.dependencies)
            primary.evidence_ids=list(dict.fromkeys(primary.evidence_ids+c.evidence_ids))
            primary.page_numbers=sorted(set(primary.page_numbers+c.page_numbers))
            aliases[c.concept_id]=primary.concept_id;deduplicated+=1
        else:
            kept.append(c)
            if c.role=="clinical_proposition" and c.generation_eligibility=="eligible_draft":signatures[key]=c
    concepts=kept
    by_id={c.concept_id:c for c in concepts}
    for c in concepts:
        c.supporting_ids=list(dict.fromkeys(aliases.get(x,x) for x in c.supporting_ids if aliases.get(x,x)!=c.concept_id))
        for dependency in c.dependencies:
            dependency.supporting_ids=list(dict.fromkeys(aliases.get(x,x) for x in dependency.supporting_ids))
    plan = ClinicalPlan(plan_id="plan:"+text_hash(f"{VERSION}:{doc.document_id}:{doc.config_hash}:{doc.canonical_buffer_id}:{max_unit_chars}:{max_units}"),
        evidence_document_id=doc.document_id,evidence_config_hash=doc.config_hash,buffer_id=doc.canonical_buffer_id,
        concepts=concepts,units=[],max_unit_chars=max_unit_chars,document_warning_ids=[w.warning_id for w in warnings],
        source_accounting={"source_characters":len(text),"owned_characters":sum(s.char_end-s.char_start for c in concepts for s in c.ownership),
                           "unassigned_characters":0,"overlapping_characters":0},deduplicated_occurrences=deduplicated)
    def make_unit(items):
        ids = [x.concept_id for x in items]
        ctx = list(dict.fromkeys(x for c in items for x in c.supporting_ids if x not in ids))
        decisions = {d.warning_id+":"+d.scope:d for c in items for d in c.warning_decisions}
        reasons = list(dict.fromkeys(r for c in items for r in c.block_reasons))
        decision = "confirmed_affected" if any(d.decision=="confirmed_affected" for d in decisions.values()) else "uncertain_dependency" if decisions or reasons else "independently_supported"
        return ClinicalUnit(unit_id="unit:"+text_hash(plan.plan_id+":"+":".join(ids+ctx)),concept_ids=ids,context_ids=ctx,
            ownership=[s for c in items for s in c.ownership],generation_eligibility="blocked" if reasons else "eligible_draft",
            dependency_decision=decision,block_reasons=reasons,warning_decisions=list(decisions.values()))
    pending=[]
    key=None
    for c in concepts:
        if c.generation_eligibility=="context_only":
            continue
        new_key=(tuple(c.section_ids),domains[c.concept_id],c.generation_eligibility,tuple(c.supporting_ids))
        candidate=make_unit(pending+[c])
        if pending and (key!=new_key or len(materialize_unit(plan,candidate,doc))>max_unit_chars):
            plan.units.append(make_unit(pending));pending=[]
        pending.append(c);key=new_key
    if pending:plan.units.append(make_unit(pending))
    for unit in plan.units:
        unit.request_characters=len(materialize_unit(plan,unit,doc))
        if unit.request_characters>max_unit_chars:
            unit.block_reasons.append("required_context_exceeds_budget")
            unit.generation_eligibility="blocked"
            for cid in unit.concept_ids:
                by_id[cid].generation_eligibility="blocked"
                by_id[cid].block_reasons.append("required_context_exceeds_budget")
    if sum(u.generation_eligibility=="eligible_draft" for u in plan.units)>max_units:
        plan.blockers.append("Eligible unit count exceeds the configured request budget; no plan is silently truncated.")
    validate_clinical_plan(plan,doc)
    return plan


def validate_clinical_plan(plan: ClinicalPlan, evidence: EvidenceDocument) -> None:
    if (plan.evidence_document_id!=evidence.document_id or plan.evidence_config_hash!=evidence.config_hash
            or plan.buffer_id!=evidence.canonical_buffer_id):
        raise ValueError("Clinical plan evidence identity mismatch.")
    by_id={c.concept_id:c for c in plan.concepts}
    if len(by_id)!=len(plan.concepts):raise ValueError("Duplicate clinical concept identity.")
    owned=[]
    native_support = NativeSupportIndex(evidence)
    trace_index = EvidenceIndex(evidence)
    warning_lookup = {w.warning_id:w for w in evidence.warnings if warning_blocks_coverage(w)}
    for c in plan.concepts:
        if not c.ownership:raise ValueError("Concept has no owned evidence.")
        for item in c.ownership:
            _check_span(evidence,item)
            if item.buffer_id!=plan.buffer_id:raise ValueError("Concept owns a noncanonical buffer.")
            owned.append((item.char_start,item.char_end))
        normalize=lambda value:re.sub(r"\s+"," ",value).strip()
        first=c.ownership[0]
        primary=normalize(evidence.buffers[first.buffer_id].text[first.char_start:first.char_end])
        if any(normalize(evidence.buffers[s.buffer_id].text[s.char_start:s.char_end])!=primary for s in c.ownership[1:]):
            raise ValueError("Deduplicated occurrences differ in source text.")
        for mention in c.mentions:
            _check_span(evidence,mention.evidence)
            if not any(s.buffer_id==mention.evidence.buffer_id and s.char_start<=mention.evidence.char_start<=mention.evidence.char_end<=s.char_end for s in c.ownership):
                raise ValueError("Literal mention lies outside its owned source.")
        for qualification in c.qualifications:
            _check_span(evidence,qualification)
            if not any(s.buffer_id==qualification.buffer_id and s.char_start<=qualification.char_start<=qualification.char_end<=s.char_end for s in c.ownership):
                raise ValueError("Qualification lies outside its owned source.")
        if any(x not in by_id for x in c.supporting_ids) or any(x not in evidence.buffers for x in c.evidence_ids):
            raise ValueError("Unknown supporting evidence identity.")
        for dependency in c.dependencies:
            _check_span(evidence,dependency.source)
            if not any(s.buffer_id==dependency.source.buffer_id and s.char_start<=dependency.source.char_start<=dependency.source.char_end<=s.char_end for s in c.ownership):
                raise ValueError("Dependency anchor lies outside its owned source.")
            if any(x not in by_id for x in dependency.supporting_ids):raise ValueError("Unknown dependency evidence.")
        if c.generation_eligibility=="eligible_draft" and (c.block_reasons or c.warning_decisions or c.unresolved_relationships):
            raise ValueError("Unresolved clinical concept was promoted.")
        if c.generation_eligibility == "eligible_draft":
            source_ids = [c.concept_id] + c.supporting_ids
            source_spans = [s for cid in source_ids for s in by_id[cid].ownership]
            source_traces = [trace_index.trace(s) for s in source_spans]
            source_pages = {p for trace in source_traces for p in trace_index.pages(trace)}
            for wid, warning in warning_lookup.items():
                raw = trace_index.trace(warning.span) if warning.span else EvidenceTrace()
                pages = {warning.page_number} if warning.page_number else set(trace_index.pages(raw))
                direct = warning.span and any(_overlap(warning.span, s) for s in source_spans)
                direct = direct or any(_overlap(a.source, b.source) for trace in source_traces for a in trace.fragments for b in raw.fragments)
                if direct or ((not pages or pages.intersection(source_pages)) and wid not in c.independent_warning_ids):
                    raise ValueError("Eligible source has an unresolved evidence warning without independent support.")
        if c.draft_support or c.independent_warning_ids or c.structural_context_ids:
            proof = native_support.prove(c.ownership[0], trace_index.trace(c.ownership[0]))
            if not proof or (c.draft_support is not None and proof != c.draft_support):
                raise ValueError("Invalid native support certificate.")
            for cid in c.structural_context_ids:
                if cid not in by_id or by_id[cid].role != "source_heading" or cid in c.supporting_ids:
                    raise ValueError("Invalid structural context support.")
                value = evidence.buffers[plan.buffer_id].text[by_id[cid].ownership[0].char_start:by_id[cid].ownership[0].char_end].strip().lower().rstrip(":")
                if value not in {"methods", "results", "discussion", "conclusions", "conclusion", "background", "introduction", "abstract", "recommendations"}:
                    raise ValueError("Clinical context cannot become structural context.")
            support = [by_id[x] for x in c.supporting_ids if by_id[x].role != "source_heading"]
            proofs = [proof] + [native_support.prove(x.ownership[0], trace_index.trace(x.ownership[0])) for x in support]
            if not all(proofs) or set(c.independent_warning_ids).intersection(d.warning_id for d in c.warning_decisions):
                raise ValueError("Native support does not establish context independence.")
            for wid in c.independent_warning_ids:
                warning = warning_lookup.get(wid)
                if warning is None or not all(native_support.warning_independent(p, warning, trace_index.trace(warning.span) if warning.span else EvidenceTrace()) for p in proofs):
                    raise ValueError("Invalid independent warning support.")
    cursor=0
    for a,b in sorted(owned):
        if a!=cursor or b<=a:raise ValueError("Clinical ownership has a gap or overlap.")
        cursor=b
    length=len(evidence.buffers[plan.buffer_id].text)
    if cursor!=length or plan.source_accounting!={"source_characters":length,"owned_characters":length,"unassigned_characters":0,"overlapping_characters":0}:
        raise ValueError("Source coverage accounting mismatch.")
    expected_warnings={w.warning_id for w in evidence.warnings if warning_blocks_coverage(w)}
    if set(plan.document_warning_ids)!=expected_warnings or plan.document_complete:
        raise ValueError("Planning cannot clear document evidence warnings or assert completion.")
    units=set();assigned=[]
    for unit in plan.units:
        if unit.unit_id in units:raise ValueError("Duplicate generation unit identity.")
        units.add(unit.unit_id)
        if any(x not in by_id for x in unit.concept_ids+unit.context_ids):raise ValueError("Unknown unit concept.")
        if set(unit.concept_ids).intersection(unit.context_ids):raise ValueError("Owned evidence also counted as context.")
        if unit.ownership!=[s for x in unit.concept_ids for s in by_id[x].ownership]:raise ValueError("Unit source ownership mismatch.")
        expected_context = list(dict.fromkeys(x for cid in unit.concept_ids for x in by_id[cid].supporting_ids if x not in unit.concept_ids))
        if unit.context_ids != expected_context:
            raise ValueError("Unit required context differs from concept support.")
        assigned.extend(unit.concept_ids)
        if unit.generation_eligibility=="eligible_draft":
            if unit.block_reasons or unit.warning_decisions or any(by_id[x].generation_eligibility!="eligible_draft" for x in unit.concept_ids):
                raise ValueError("Blocked unit was promoted to generation.")
            if any(by_id[x].block_reasons for x in unit.context_ids) or unit.request_characters>plan.max_unit_chars:
                raise ValueError("Required context is unresolved or exceeds the budget.")
    expected={c.concept_id for c in plan.concepts if c.generation_eligibility!="context_only"}
    if len(assigned)!=len(set(assigned)) or set(assigned)!=expected:
        raise ValueError("Clinical concepts were duplicated or omitted from generation/adjudication units.")


def materialize_unit(plan: ClinicalPlan, unit: ClinicalUnit, evidence: EvidenceDocument) -> str:
    if evidence.document_id!=plan.evidence_document_id or evidence.canonical_buffer_id!=plan.buffer_id:
        raise ValueError("Generation unit belongs to different source evidence.")
    by_id={c.concept_id:c for c in plan.concepts}
    blocks=[]
    for label,ids in (("CONTEXT ONLY",unit.context_ids),("OWNED SOURCE",unit.concept_ids)):
        for cid in ids:
            c=by_id[cid]
            # Additional ownership spans represent proven exact duplicate
            # occurrences in the same context, not additional source claims.
            s=c.ownership[0]
            value=evidence.buffers[s.buffer_id].text[s.char_start:s.char_end]
            blocks.append(f"[{label} {cid}]\n{value}")
    return "\n".join(blocks)


def required_qualification_spans(plan: ClinicalPlan, concept: ClinicalConcept) -> list[EvidenceSpan]:
    """References only: preserve literal scope/qualifiers without crediting context as work."""
    by_id={c.concept_id:c for c in plan.concepts}
    required=list(concept.qualifications)
    for cid in concept.supporting_ids:
        context=by_id[cid]
        required.extend(context.qualifications)
        required.extend(m.evidence for m in context.mentions if m.kind in {"population","intervention","endpoint","group"})
    return required


def reconcile_concept_coverage(plan: ClinicalPlan, evidence: EvidenceDocument, generated: dict, report: dict, diagnostics: dict) -> dict:
    from .chunk_validator import eligible_chunk_numbers
    passed=eligible_chunk_numbers(report,{"PASS"})
    chunks={c.get("chunk_number",i):c for i,c in enumerate(generated.get("chunks",[]),1)}
    origins={r["chunk_number"]:set(r["unit_ids"]) for r in diagnostics.get("chunk_ownership",[])}
    owners=defaultdict(set)
    for unit in plan.units:
        if unit.generation_eligibility=="eligible_draft":
            for cid in unit.concept_ids:owners[cid].add(unit.unit_id)
    normalize=lambda value: re.sub(r"\s+"," ",value).strip()
    rows=[]
    sections=defaultdict(list)
    for c in plan.concepts:
        supported=[]
        if c.role!="source_heading" and c.generation_eligibility=="eligible_draft":
            needles=[normalize(evidence.buffers[s.buffer_id].text[s.char_start:s.char_end]) for s in c.ownership]
            needles.extend(normalize(evidence.buffers[s.buffer_id].text[s.char_start:s.char_end]) for s in required_qualification_spans(plan,c))
            for number in passed:
                if origins.get(number,set()).intersection(owners[c.concept_id]) and all(n and n in normalize(chunks.get(number,{}).get("original_text","")) for n in needles):
                    supported.append(number)
        status="context_only" if c.role=="source_heading" else "source_supported" if supported else "requires_adjudication" if c.generation_eligibility=="blocked" else "not_source_supported"
        rows.append({"concept_id":c.concept_id,"status":status,"chunk_numbers":supported})
        if c.role!="source_heading":
            for sid in c.section_ids:sections[sid].append(status)
    complete=any(r["status"]=="source_supported" for r in rows) and not plan.document_warning_ids and not plan.blockers and all(r["status"] in {"context_only","source_supported"} for r in rows)
    return {"plan_id":plan.plan_id,"complete":complete,"concepts":rows,
            "complete_section_ids":[sid for sid,states in sections.items() if states and all(s=="source_supported" for s in states)],
            "source_accounting":plan.source_accounting,"clinically_approved_concepts":0}

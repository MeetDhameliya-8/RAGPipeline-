"""Witnessed native prose for clinical drafting, never clinical source approval."""
from __future__ import annotations

import math
import re
from typing import Literal, TYPE_CHECKING

from pydantic import Field

from .concept_planner import Contract, CLINICAL, _atoms
from .draft_support import NativeLineSupport, NativeSupportIndex, boxes_overlap
from .evidence import EvidenceDocument, EvidenceSpan, span, text_hash, validate_evidence
from .visual_source import VisualSourcePage, PROMPT as VISUAL_PROMPT

if TYPE_CHECKING:
    from .source_map import SourceDocument

VERSION = "native-prose-packages-v2"
LEGACY_VERSION = "native-prose-packages-v1"
REFERENCE_BOUNDARY = re.compile(r"(?im)^[ \t]*(?:references|bibliography)[ \t]*\r?$")
DAMAGE = re.compile(r"\(cid:\d+\)|/(?:H\d{4,}|C\d{2,})|[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\u00fe]", re.I)
STRUCTURED = re.compile(r"\b(?:table|figure|fig\.)\s*\d", re.I)
BOILERPLATE = re.compile(r"correspondence|all rights reserved|copyright|department of|author affiliations|downloaded from|conflicts? of interest", re.I)


class NativeTextWitness(Contract):
    block_id: str
    page_number: int
    native_span: EvidenceSpan
    candidate_spans: list[EvidenceSpan]
    line_support: list[NativeLineSupport] = Field(default_factory=list)
    view_spans: list[EvidenceSpan] = Field(default_factory=list)


class SourcePackage(Contract):
    unit_id: str
    ownership: list[EvidenceSpan]
    witnesses: list[NativeTextWitness]
    section_hint: str
    page_numbers: list[int]
    source_words: int
    request_characters: int
    review_required: Literal[True] = True
    visual_source_ids: list[str] = Field(default_factory=list)


class SourceHold(Contract):
    block_id: str
    page_number: int
    reason: str
    source_span: EvidenceSpan | None = None


class SourcePackagePlan(Contract):
    version: Literal["native-prose-packages-v1", "native-prose-packages-v2"] = VERSION
    plan_id: str
    evidence_document_id: str
    evidence_config_hash: str
    pdf_sha256: str
    packages: list[SourcePackage]
    holds: list[SourceHold]
    document_warning_ids: list[str]
    max_unit_chars: int
    max_units: int
    min_words: int = 90
    blockers: list[str] = Field(default_factory=list)
    document_complete: Literal[False] = False
    visual_sources: list[VisualSourcePage] = Field(default_factory=list)


def _words(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))


def _text(doc: EvidenceDocument, owned: EvidenceSpan) -> str:
    return doc.buffers[owned.buffer_id].text[owned.char_start:owned.char_end]


def _witness(doc, page, block, native: NativeSupportIndex, owned: EvidenceSpan | None = None) -> NativeTextWitness | None:
    if (not block.span or not block.bbox or block.direction != (1., 0.) or
            page.rotation not in {None, 0} or not block.lines or
            not all(math.isfinite(v) for v in block.bbox)):
        return None
    if any(b.block_id != block.block_id and b.span and b.bbox and boxes_overlap(block.bbox, b.bbox) for b in page.blocks):
        return None
    owned = owned or block.span
    value = " ".join(_text(doc, owned).split())
    if not value or DAMAGE.search(value):
        return None
    last_y = -float("inf")
    for line in block.lines:
        if line.span.char_end <= owned.char_start or line.span.char_start >= owned.char_end:
            continue
        if (line.direction != (1., 0.) or line.bbox[1] <= last_y or
                not all(math.isfinite(v) for v in line.bbox) or
                line.bbox[0] < block.bbox[0]-.1 or line.bbox[1] < block.bbox[1]-.1 or
                line.bbox[2] > block.bbox[2]+.1 or line.bbox[3] > block.bbox[3]+.1):
            return None
        last_y = line.bbox[1]
    candidates = {}
    for candidate in page.candidates:
        if candidate.extractor in {"pypdf", "pymupdf"}:
            found = native.match(candidate.buffer_id, value)
            if found:
                candidates.setdefault(candidate.extractor, found[0])
    lines = []
    if set(candidates) != {"pypdf", "pymupdf"}:
        # This method only supports positively reconstructed pages, retains
        # exact line witnesses and accounts for every non-whitespace character.
        lines = native._line_support(page, block, owned)
        if not lines:
            return None
    view_ids = {doc.canonical_buffer_id}
    view_ids.update(v.original_canonical_id for v in doc.native_layout_views)
    views = [found[0] for key in sorted(view_ids) if (found := native.match(key, value))]
    return NativeTextWitness(block_id=block.block_id, page_number=page.page_number,
        native_span=owned, candidate_spans=[candidates[k] for k in sorted(candidates)] if not lines else
        [s for line in lines for s in line.candidate_spans], line_support=lines, view_spans=views)


def _build(doc: EvidenceDocument, max_unit_chars: int, max_units: int, min_words: int,
           *, reference_boundaries: bool = False) -> SourcePackagePlan:
    from .native_layout import _ordered
    from .source_map import _looks_like_heading
    validate_evidence(doc)
    if (doc.identity_quality != "pdf_bytes" or not doc.pdf_sha256 or
            not re.fullmatch(r"[a-f0-9]{64}", doc.pdf_sha256) or doc.document_id != "pdf:"+doc.pdf_sha256):
        raise ValueError("Source packages require verified PDF byte identity.")
    if max_unit_chars < 1000 or max_units < 1 or min_words < 90:
        raise ValueError("Invalid source package budget.")
    native = NativeSupportIndex(doc)
    packages, holds = [], []
    heading = "Source prose"
    references = False
    for page in doc.pages:
        blocks = [b for b in page.blocks if b.span]
        try:
            ordered = _ordered(blocks)
            order_verified = True
        except (ValueError, TypeError):
            ordered = sorted(blocks, key=lambda b: b.native_order)
            order_verified = False
        pending = []

        def flush():
            nonlocal pending
            if not pending:
                return
            text = "\n\n".join(_text(doc, w.native_span) for w in pending)
            reason = ("insufficient_coherent_source_length" if _words(text) < min_words else
                      "clinical_source_scope_unclassified" if not CLINICAL.search(text) else
                      "incomplete_native_paragraph" if not re.search(r"[.!?;:)][\"'’”]*(?:\s*\d{1,3})?\s*$", text) else
                      "source_package_exceeds_budget" if len(text) > max_unit_chars else "")
            if reason:
                holds.extend(SourceHold(block_id=w.block_id,page_number=w.page_number,reason=reason) for w in pending)
            else:
                ownership = [w.native_span for w in pending]
                # Identical source ownership retains its existing candidate ID.
                uid = "package:" + text_hash(f"{LEGACY_VERSION}:{doc.document_id}:{doc.config_hash}:{heading}:"+
                    ":".join(f"{s.buffer_id}:{s.char_start}:{s.char_end}" for s in ownership))
                packages.append(SourcePackage(unit_id=uid,ownership=ownership,witnesses=pending,
                    section_hint=heading,page_numbers=[page.page_number],source_words=_words(text),request_characters=len(text)))
            pending = []

        previous = None
        for block in ordered:
            text = _text(doc, block.span)
            stripped = " ".join(text.split())
            is_heading = _looks_like_heading(stripped) or (_words(stripped) <= 12 and not re.search(r"[.!?;]", stripped))
            if is_heading:
                flush();heading = stripped[:160]
                references = references or stripped.strip(" .:").casefold() in {"references", "bibliography"}
                holds.append(SourceHold(block_id=block.block_id,page_number=page.page_number,reason="source_heading_or_label"))
                previous = None
                continue
            boundary = REFERENCE_BOUNDARY.search(text) if reference_boundaries else None
            if boundary:
                regions = list(_atoms(text[:boundary.start()]))
                regions.append((boundary.start(), len(text)))
            else:
                regions = _atoms(text)
            for a,b in regions:
                if boundary and a >= boundary.start():
                    flush(); references = True
                region = text[a:b]
                owned = span(block.span.buffer_id,block.span.char_start+a,block.span.char_start+b)
                reason = ("reference_list" if references else "structured_or_visual_relationship" if STRUCTURED.search(region) or block.role != "text" else
                          "nonclinical_boilerplate" if BOILERPLATE.search(region) else
                          "damaged_native_text" if DAMAGE.search(region) else "")
                witness = None if reason else _witness(doc,page,block,native,owned)
                if not witness:
                    if pending and re.match(r"\s*(however|but|although|only|except|unless|provided|excluding)\b",region,re.I):
                        holds.extend(SourceHold(block_id=w.block_id,page_number=w.page_number,reason="unresolved_adjacent_qualification",source_span=w.native_span) for w in pending)
                        pending=[]
                    flush();holds.append(SourceHold(block_id=block.block_id,page_number=page.page_number,
                        reason=reason or "native_extraction_support_unresolved",source_span=owned))
                    previous=None
                    continue
                same_column = previous is not None and max(previous.bbox[0],block.bbox[0]) < min(previous.bbox[2],block.bbox[2])
                same_block = previous is not None and previous.block_id == block.block_id
                current_size=sum(len(_text(doc,w.native_span))+2 for w in pending)
                if pending and ((not order_verified and not same_block) or not same_column or current_size+len(region)>max_unit_chars or
                                sum(_words(_text(doc,w.native_span)) for w in pending)>=350):
                    flush()
                pending.append(witness);previous=block
            if not order_verified:
                flush();previous=None
        flush()
        if not blocks:
            holds.append(SourceHold(block_id=page.page_id,page_number=page.page_number,reason="no_machine_readable_text_requires_ocr"))
    version = VERSION if reference_boundaries else LEGACY_VERSION
    pid = "packages:"+text_hash(f"{version}:{doc.document_id}:{doc.config_hash}:{doc.canonical_buffer_id}:{max_unit_chars}:{max_units}:{min_words}")
    blockers = ["Selected source packages exceed the request budget; explicit configuration is required."] if len(packages)>max_units else []
    return SourcePackagePlan(version=version,plan_id=pid,evidence_document_id=doc.document_id,evidence_config_hash=doc.config_hash,
        pdf_sha256=doc.pdf_sha256,packages=packages,holds=holds,document_warning_ids=[w.warning_id for w in doc.warnings],
        max_unit_chars=max_unit_chars,max_units=max_units,min_words=min_words,blockers=blockers)


def build_source_package_plan(extraction: dict, source_doc: SourceDocument, *, max_unit_chars: int = 20000,
                              max_units: int = 80) -> SourcePackagePlan:
    doc = source_doc.evidence
    if doc is None or doc.buffers[doc.canonical_buffer_id].text != extraction["full_text"]:
        raise ValueError("Source package extraction identity mismatch.")
    base = _build(doc,max_unit_chars,max_units,90,reference_boundaries=True)
    visual = [VisualSourcePage.model_validate(v) for v in extraction.get("visual_source_pages",[])]
    return _attach_visual(base,visual,doc)


def _attach_visual(base: SourcePackagePlan, records: list[VisualSourcePage], doc: EvidenceDocument) -> SourcePackagePlan:
    if not records:
        return base
    if len({r.recovery_id for r in records})!=len(records):
        raise ValueError("Duplicate visual source representation.")
    packages=list(base.packages)
    for record in records:
        expected=text_hash(f"azure-visual-source-v1:{doc.pdf_sha256}:{record.page_number}:{record.image_sha256}:{record.provider_fingerprint}:{text_hash(VISUAL_PROMPT)}")
        if (record.pdf_sha256!=doc.pdf_sha256 or record.recovery_id!="visual:"+expected or
                not any(p.page_number==record.page_number for p in doc.pages) or not record.uncertainties or
                not re.fullmatch(r"[a-f0-9]{64}",record.image_sha256) or
                not re.fullmatch(r"[a-f0-9]{64}",record.provider_fingerprint)):
            raise ValueError("Visual source identity mismatch.")
        if not any(w.code=="visual_transcription_requires_review" and w.page_number==record.page_number and w.critical and not w.resolved for w in doc.warnings):
            raise ValueError("Visual source uncertainty was cleared.")
        for key,kind in ((record.first_buffer_id,"pass1"),(record.second_buffer_id,"pass2")):
            buffer=doc.buffers.get(key)
            if (buffer is None or buffer.stage!="raw" or buffer.page_number!=record.page_number or
                    f":visual:{expected}:{kind}:" not in key):
                raise ValueError("Visual source transcription buffer mismatch.")
        second=" ".join(doc.buffers[record.second_buffer_id].text.split())
        for owned in record.paragraph_spans:
            if owned.buffer_id!=record.first_buffer_id or owned.char_end>len(doc.buffers[owned.buffer_id].text):
                raise ValueError("Visual source paragraph span mismatch.")
            value=_text(doc,owned)
            if (_words(value)<90 or not CLINICAL.search(value) or second.count(" ".join(value.split()))!=1 or
                    " ".join(doc.buffers[record.first_buffer_id].text.split()).count(" ".join(value.split()))!=1 or
                    DAMAGE.search(value) or STRUCTURED.search(value) or BOILERPLATE.search(value)):
                raise ValueError("Visual source paragraph lacks exact two-pass corroboration.")
            if len(value)>base.max_unit_chars:
                continue
            packages.append(SourcePackage(unit_id="package:"+text_hash(f"{record.recovery_id}:{owned.model_dump_json()}"),
                ownership=[owned],witnesses=[],section_hint="Visually transcribed source prose — clinical review required",
                page_numbers=[record.page_number],source_words=_words(value),request_characters=len(value),visual_source_ids=[record.recovery_id]))
    blockers=list(base.blockers)
    if len(packages)>base.max_units and not blockers:
        blockers.append("Visual/native source packages exceed the request budget; explicit configuration is required.")
    return base.model_copy(update={"packages":packages,"visual_sources":records,"blockers":blockers,
        "plan_id":"packages:"+text_hash(base.plan_id+":"+":".join(r.recovery_id for r in records))})


def validate_source_package_plan(plan: SourcePackagePlan, evidence: EvidenceDocument) -> None:
    expected = _attach_visual(_build(evidence,plan.max_unit_chars,plan.max_units,plan.min_words,
        reference_boundaries=plan.version==VERSION),plan.visual_sources,evidence)
    if plan != expected:
        raise ValueError("Source package metadata differs from recomputed native evidence.")


def materialize_package(package: SourcePackage, evidence: EvidenceDocument) -> str:
    return "\n\n".join(_text(evidence,s) for s in package.ownership)


def package_validation_sources(plan: SourcePackagePlan, evidence: EvidenceDocument, diagnostics: dict) -> dict:
    validate_source_package_plan(plan,evidence)
    by_id = {p.unit_id:p for p in plan.packages}
    result = {}
    for record in diagnostics.get("chunk_ownership",[]):
        if not record["unit_ids"] or any(uid not in by_id for uid in record["unit_ids"]):
            raise ValueError("Unknown source package chunk origin.")
        result[record["chunk_number"]] = [{"text":materialize_package(by_id[uid],evidence),
            "evidence_document_id":evidence.document_id,"pdf_sha256":evidence.pdf_sha256,
            "unit_id":uid,"ownership":[s.model_dump() for s in by_id[uid].ownership],
            "page_numbers":by_id[uid].page_numbers,"view":"visual_transcription_draft" if by_id[uid].visual_source_ids else "witnessed_native_prose",
            "review_required":True,
            **({"visual_transcription":True,"native_numeric_source":"\n\n".join(evidence.buffers[p.selected_buffer_id].text
                for p in evidence.pages if p.page_number in by_id[uid].page_numbers),
                "visual_source_ids":by_id[uid].visual_source_ids} if by_id[uid].visual_source_ids else {})
            } for uid in record["unit_ids"]]
    return result

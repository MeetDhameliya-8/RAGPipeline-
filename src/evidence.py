"""Internal, source-bound evidence buffers. No clinical reconstruction or planner."""
from __future__ import annotations

from .diagnostics import timed

from difflib import SequenceMatcher
from hashlib import sha256
import json
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


EVIDENCE_VERSION = "p1a-evidence-v1"


def text_hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EvidenceSpan(Record):
    buffer_id: str
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)

    @model_validator(mode="after")
    def ordered(self) -> EvidenceSpan:
        if self.char_end < self.char_start:
            raise ValueError("Evidence span ends before it starts.")
        return self


class TextBuffer(Record):
    buffer_id: str
    stage: Literal["raw", "cleaned", "canonical", "section_map"]
    text: str
    text_sha256: str
    page_number: int | None = None
    extractor: str | None = None


class SpanMapping(Record):
    source: EvidenceSpan | None
    target: EvidenceSpan
    relation: Literal["exact", "transformed", "inserted", "deleted"]
    reason: str


WarningClassification = Literal["confirmed_missing", "clinical_unresolved", "possible_corruption",
                                "unclassified_visual", "supported_nonclinical", "legacy_unclassified"]


class EvidenceWarning(Record):
    warning_id: str
    code: str
    message: str
    critical: bool = False
    resolved: bool = False
    page_number: int | None = None
    span: EvidenceSpan | None = None
    classification: WarningClassification = "legacy_unclassified"
    classification_basis: str = ""
    bbox: tuple[float, float, float, float] | None = None
    block_id: str | None = None

    @model_validator(mode="after")
    def supported_classification(self) -> EvidenceWarning:
        if self.classification == "supported_nonclinical" and not self.classification_basis.strip():
            raise ValueError("Nonclinical classification requires independent supporting evidence.")
        return self


class EvidenceCandidate(Record):
    buffer_id: str
    extractor: str
    method: str
    quality_score: float | None = None


class EvidenceLine(Record):
    span: EvidenceSpan
    bbox: tuple[float, float, float, float]
    direction: tuple[float, float]


class EvidenceBlock(Record):
    block_id: str
    span: EvidenceSpan | None = None
    bbox: tuple[float, float, float, float] | None = None
    native_order: int
    role: Literal["text", "figure", "figure_caption", "table_caption", "footnote_candidate", "visual_region"] = "text"
    direction: tuple[float, float] | None = None
    associations: list[str] = Field(default_factory=list)
    signal_kind: Literal["image", "drawing", "unreported"] = "unreported"
    lines: list[EvidenceLine] = Field(default_factory=list)


class EvidenceRoute(Record):
    route_id: str
    version: Literal["local-evidence-routes-v1"] = "local-evidence-routes-v1"
    kind: Literal["native_text", "native_layout", "table_relationship_review", "figure_review", "visual_classification_review", "ocr_required"]
    status: Literal["retained_native", "proposed_native", "reconstructed", "held", "unavailable"]
    page_number: int
    bbox: tuple[float, float, float, float] | None
    block_ids: list[str] = Field(default_factory=list)
    source_spans: list[EvidenceSpan] = Field(default_factory=list)
    warning_ids: list[str] = Field(default_factory=list)
    reason_codes: list[str]
    original_buffer_id: str | None = None
    alternate_buffer_id: str | None = None
    comparison: dict[str, int] = Field(default_factory=dict)


class NativeLayoutView(Record):
    version: Literal["native-xycut-v1"] = "native-xycut-v1"
    view_id: str
    configuration: dict[str, int]
    config_hash: str
    original_canonical_id: str
    original_cleaned_id: str
    alternate_canonical_id: str
    alternate_cleaned_id: str
    page_buffer_ids: list[str]
    route_ids: list[str]


class EvidencePage(Record):
    page_id: str
    page_number: int
    width: float | None = None
    height: float | None = None
    rotation: int | None = None
    candidates: list[EvidenceCandidate]
    selected_buffer_id: str
    cleaned_buffer_id: str
    selection_reason: str
    blocks: list[EvidenceBlock] = Field(default_factory=list)
    native_order_verified: bool = False


class EvidenceDocument(Record):
    version: Literal["p1a-evidence-v1"] = EVIDENCE_VERSION
    document_id: str
    pdf_sha256: str | None = None
    identity_quality: Literal["pdf_bytes", "legacy_text_only"]
    extractor_versions: dict[str, str]
    config_hash: str
    configuration: dict[str, Any] = Field(default_factory=dict)
    canonical_buffer_id: str = ""
    cleaned_buffer_id: str = ""
    raw_buffer_id: str = ""
    buffers: dict[str, TextBuffer] = Field(default_factory=dict)
    mappings: dict[str, list[SpanMapping]] = Field(default_factory=dict)
    pages: list[EvidencePage] = Field(default_factory=list)
    warnings: list[EvidenceWarning] = Field(default_factory=list)
    processing_routes: list[EvidenceRoute] = Field(default_factory=list)
    native_layout_views: list[NativeLayoutView] = Field(default_factory=list)
    _warning_index: set[str] = PrivateAttr(default_factory=set)
    _warning_index_signature: tuple[int, int] | None = PrivateAttr(default=None)

    def __eq__(self, other: object) -> bool:
        # Derived builder indexes are not part of persisted source identity.
        if not isinstance(other,EvidenceDocument):return NotImplemented
        return all(getattr(self,name)==getattr(other,name) for name in type(self).model_fields)


class TraceFragment(Record):
    source: EvidenceSpan
    precision: Literal["exact", "region"]


class EvidenceTrace(Record):
    fragments: list[TraceFragment] = Field(default_factory=list)
    synthetic_spans: list[EvidenceSpan] = Field(default_factory=list)
    unmapped_spans: list[EvidenceSpan] = Field(default_factory=list)


def add_buffer(doc: EvidenceDocument, stage: str, label: str, text: str,
               page_number: int | None = None, extractor: str | None = None) -> str:
    digest = text_hash(text)
    buffer_id = ("full_text:sha256:" + digest if stage == "canonical" else
                 f"{doc.document_id}:{doc.config_hash[:12]}:{stage}:{label}:{digest}")
    doc.buffers[buffer_id] = TextBuffer(buffer_id=buffer_id, stage=stage, text=text, text_sha256=digest,
                                      page_number=page_number, extractor=extractor)
    return buffer_id


def span(buffer_id: str, start: int, end: int) -> EvidenceSpan:
    return EvidenceSpan(buffer_id=buffer_id, char_start=start, char_end=end)


def align_buffers(doc: EvidenceDocument, source_id: str, target_id: str, reason: str) -> None:
    """Describe monotone edits; replacements are region links, never exact claims.

    Whitespace-token alignment avoids pathological PDF layout padding at the
    character-matching level. Original characters and deletions are retained.
    """
    source = doc.buffers[source_id].text
    target = doc.buffers[target_id].text
    if source == target:
        doc.mappings[target_id] = [SpanMapping(source=span(source_id, 0, len(source)),
            target=span(target_id, 0, len(target)), relation="exact", reason=reason)] if target else []
        return
    a = list(re.finditer(r"\S+|\s+", source))
    b = list(re.finditer(r"\S+|\s+", target))
    def key(match: re.Match[str]) -> str:
        return " " if match.group().isspace() else match.group()
    def extent(tokens: list[re.Match[str]], first: int, last: int, length: int) -> tuple[int, int]:
        start = tokens[first].start() if first < len(tokens) else length
        return start, tokens[last - 1].end() if last > first else start
    mappings = []
    # Frequent padding/whitespace tokens must not create quadratic matching work.
    # Skipped anchors only coarsen transformed regions; exact links still compare characters.
    for tag, a0, a1, b0, b1 in SequenceMatcher(None, [key(m) for m in a], [key(m) for m in b], autojunk=True).get_opcodes():
        pairs = [(i, i+1, j, j+1) for i, j in zip(range(a0, a1), range(b0, b1))] if tag == "equal" else [(a0, a1, b0, b1)]
        for i0, i1, j0, j1 in pairs:
            x, y = extent(a, i0, i1, len(source))
            u, v = extent(b, j0, j1, len(target))
            relation = "inserted" if x == y else "deleted" if u == v else "exact" if source[x:y] == target[u:v] else "transformed"
            item = SpanMapping(source=span(source_id, x, y) if x != y else None,
                               target=span(target_id, u, v), relation=relation, reason=reason)
            if (mappings and relation == "exact" and mappings[-1].relation == "exact"
                    and mappings[-1].source.char_end == x and mappings[-1].target.char_end == u):
                mappings[-1].source.char_end = y
                mappings[-1].target.char_end = v
            else:
                mappings.append(item)
    doc.mappings[target_id] = mappings
    for mapping in mappings:
        if mapping.relation == "exact":
            continue
        original = source[mapping.source.char_start:mapping.source.char_end] if mapping.source else ""
        changed = target[mapping.target.char_start:mapping.target.char_end]
        if original.strip() or changed.strip():
            warn(doc, "text_transformation_unreviewed",
                 "Non-whitespace cleaning/section transformation retained for review; no clinical equivalence is certified.",
                 doc.buffers[target_id].page_number, True, mapping.source if mapping.relation == "deleted" else mapping.target)


def join_buffers(doc: EvidenceDocument, ids: list[str], stage: str, label: str) -> str:
    target_id = add_buffer(doc, stage, label, "\n\n".join(doc.buffers[i].text for i in ids))
    mappings = []
    offset = 0
    for index, source_id in enumerate(ids):
        if index:
            mappings.append(SpanMapping(source=None, target=span(target_id, offset, offset+2), relation="inserted", reason="page_separator"))
            offset += 2
        length = len(doc.buffers[source_id].text)
        if length:
            mappings.append(SpanMapping(source=span(source_id, 0, length), target=span(target_id, offset, offset+length), relation="exact", reason="page_join"))
        offset += length
    doc.mappings[target_id] = mappings
    return target_id


def warn(doc: EvidenceDocument, code: str, message: str, page_number: int | None = None,
         critical: bool = False, source_span: EvidenceSpan | None = None, *,
         bbox: tuple[float, float, float, float] | None = None, block_id: str | None = None) -> None:
    classification = {
        "missing_page_evidence": "confirmed_missing",
        "table_relationships_unresolved": "clinical_unresolved",
        "figure_evidence_unresolved": "clinical_unresolved",
        "text_transformation_unreviewed": "clinical_unresolved",
        "existing_extraction_blocker": "clinical_unresolved",
        "encoding_sign_unit_unresolved": "possible_corruption",
        "reading_order_unverified": "possible_corruption",
        "extractor_disagreement": "possible_corruption",
        "geometry_unavailable": "possible_corruption",
        "visual_inventory_unavailable": "possible_corruption",
        "visual_region_unclassified": "unclassified_visual",
    }.get(code, "legacy_unclassified")
    identity = json.dumps([doc.document_id, code, page_number, source_span.model_dump() if source_span else None, bbox, block_id], sort_keys=True)
    warning_id = "warning:" + text_hash(identity)
    signature = (id(doc.warnings),len(doc.warnings))
    if doc._warning_index_signature != signature:
        doc._warning_index = {w.warning_id for w in doc.warnings}
    if warning_id not in doc._warning_index or not any(w.warning_id==warning_id for w in doc.warnings):
        doc.warnings.append(EvidenceWarning(warning_id=warning_id, code=code, message=message,
                                           page_number=page_number, critical=critical, span=source_span,
                                           classification=classification, bbox=bbox, block_id=block_id))
        doc._warning_index.add(warning_id)
    doc._warning_index_signature = (id(doc.warnings),len(doc.warnings))


def warning_blocks_coverage(warning: EvidenceWarning) -> bool:
    """Uncertainty is distinct from confirmed loss, but neither proves completeness."""
    return not warning.resolved and (warning.critical or warning.classification in {
        "confirmed_missing", "clinical_unresolved", "possible_corruption", "unclassified_visual", "supported_nonclinical"})


def warning_pages(doc: EvidenceDocument, warning: EvidenceWarning) -> list[int]:
    if warning.page_number is not None:
        return [warning.page_number]
    if warning.span is None:
        return []
    trace = trace_to_raw(doc, warning.span)
    return sorted({number for f in trace.fragments if (number := doc.buffers[f.source.buffer_id].page_number) is not None})


@timed("evidence_construction")
def build_evidence_document(extraction: dict[str, Any], *, pdf_sha256: str | None = None,
                            candidates: list[dict[str, Any]] | None = None,
                            configuration: dict[str, Any] | None = None) -> EvidenceDocument:
    options = {"version": EVIDENCE_VERSION, "cleaner": "existing-p0", "selection": "existing-quality-score-v1",
               **(configuration or {})}
    versions = dict(extraction.get("extractor_versions") or {})
    config_hash = text_hash(json.dumps({"options": options, "versions": versions}, sort_keys=True))
    source_text = str(extraction.get("raw_full_text") or extraction.get("full_text") or "")
    identity = pdf_sha256 or text_hash(source_text)
    doc = EvidenceDocument(document_id=("pdf:" if pdf_sha256 else "legacy:") + identity, pdf_sha256=pdf_sha256,
                           identity_quality="pdf_bytes" if pdf_sha256 else "legacy_text_only",
                           extractor_versions=versions, config_hash=config_hash, configuration=options)
    pages = extraction.get("pages") or [{"page_number": 1, "raw_text": source_text, "text": source_text}]
    all_candidates = candidates or pages
    if not pdf_sha256:
        warn(doc, "pdf_identity_unknown", "Legacy text-only input has no verified PDF byte identity.", critical=True)
    for index, page in enumerate(pages, 1):
        number = int(page.get("page_number", index))
        page_id = f"{doc.document_id}:page:{number}"
        native = [c for c in all_candidates if int(c.get("page_number", index)) == number]
        native_candidates = []
        for candidate_index, c in enumerate(native):
            method = str(c.get("method") or "native_method_unreported")
            extractor = str(c.get("extractor") or "unreported")
            buffer_id = add_buffer(doc, "raw", f"page:{number}:candidate:{extractor}:{candidate_index}",
                                   str(c.get("raw_text") if c.get("raw_text") is not None else c.get("text") or ""), number, extractor)
            score = c.get("quality_score")
            native_candidates.append(EvidenceCandidate(buffer_id=buffer_id, extractor=extractor, method=method,
                quality_score=float(score) if isinstance(score, (int, float)) else None))
        selected_text = str(page.get("raw_text") if page.get("raw_text") is not None else page.get("text") or "")
        selected = next((c.buffer_id for c in native_candidates if doc.buffers[c.buffer_id].text == selected_text
                         and c.extractor == str(page.get("extractor") or "unreported")), None)
        if selected is None:
            selected = add_buffer(doc, "raw", f"page:{number}:selected", selected_text, number, str(page.get("extractor") or "unreported"))
            native_candidates.append(EvidenceCandidate(buffer_id=selected, extractor=str(page.get("extractor") or "unreported"), method="selected_method_unreported"))
        cleaned = add_buffer(doc, "cleaned", f"page:{number}", str(page.get("text") if page.get("text") is not None else selected_text), number)
        align_buffers(doc, selected, cleaned, "existing_page_cleaning")
        layout = next((c.get("layout") for c in native if isinstance(c.get("layout"), dict)), {}) or {}
        evidence_page = EvidencePage(page_id=page_id, page_number=number, candidates=native_candidates,
            selected_buffer_id=selected, cleaned_buffer_id=cleaned,
            width=layout.get("width"), height=layout.get("height"), rotation=layout.get("rotation"),
            selection_reason="Existing page quality comparison; selection is not certification of reading order or clinical fidelity.")
        _add_layout(doc, evidence_page, layout)
        doc.pages.append(evidence_page)
        if not selected_text.strip():
            warn(doc, "missing_page_evidence", "Page has no selected machine-readable text; no exclusion was approved.", number, True)
        candidate_text = "\n".join(doc.buffers[c.buffer_id].text for c in native_candidates)
        for candidate in native_candidates:
            match = re.search(r"[\ufffd\u00fe]|\(cid:\d+\)|/(?:H\d{4,}|C\d{2,})|[\x00-\x08\x0b\x0c\x0e-\x1f]", doc.buffers[candidate.buffer_id].text)
            if match:
                warn(doc, "encoding_sign_unit_unresolved", "Possible encoding/sign/unit corruption requires source review; first observed location per candidate, not an exhaustive glyph inventory.",
                     number, True, span(candidate.buffer_id, match.start(), match.end()))
        if re.search(r"\btable\s+\d", candidate_text, re.I):
            warn(doc, "table_relationships_unresolved", "Table mention/caption detected; cell/header/unit/footnote associations are not established.", number, True)
        if re.search(r"\b(?:figure|fig\.)\s*\d", candidate_text, re.I):
            warn(doc, "figure_evidence_unresolved", "Figure mention/caption does not establish its clinical contents or relationships.", number, True)
        if len(native_candidates) > 1:
            a, b = [re.findall(r"\S+", doc.buffers[c.buffer_id].text) for c in native_candidates[:2]]
            if SequenceMatcher(None, a, b, autojunk=True).ratio() < .85:
                warn(doc, "extractor_disagreement", "Native extractor sequence/content disagreement is unresolved.", number, True)
    doc.raw_buffer_id = join_buffers(doc, [p.selected_buffer_id for p in doc.pages], "raw", "document_join")
    doc.cleaned_buffer_id = join_buffers(doc, [p.cleaned_buffer_id for p in doc.pages if doc.buffers[p.cleaned_buffer_id].text.strip()], "cleaned", "document_join")
    doc.canonical_buffer_id = add_buffer(doc, "canonical", "full_text", str(extraction.get("full_text") or ""))
    align_buffers(doc, doc.cleaned_buffer_id, doc.canonical_buffer_id, "existing_article_cleaning")
    for message in extraction.get("extraction_warnings") or []:
        match = re.search(r"\bPage\s+(\d+)", str(message), re.I)
        warn(doc, "extractor_warning", str(message), int(match.group(1)) if match else None)
    for message in extraction.get("adaptive_blockers") or []:
        match = re.search(r"\bPage\s+(\d+)", str(message), re.I)
        warn(doc, "existing_extraction_blocker", str(message), int(match.group(1)) if match else None, True)
    validate_evidence(doc)
    return doc


def _add_layout(doc: EvidenceDocument, page: EvidencePage, layout: dict[str, Any]) -> None:
    for code in layout.get("warnings", []):
        warn(doc, code, "Part of the native visual inventory was unavailable; retained geometry is incomplete.", page.page_number, True)
    blocks = layout.get("blocks") or []
    for index, item in enumerate(blocks):
        text = str(item.get("text") or "")
        buffer_id = add_buffer(doc, "raw", f"page:{page.page_number}:native-block:{index}", text, page.page_number, "pymupdf_dict") if text else None
        role = item.get("role", "text")
        if re.match(r"\s*(?:figure|fig\.)\s*\d", text, re.I): role = "figure_caption"
        if re.match(r"\s*table\s+\d", text, re.I): role = "table_caption"
        page.blocks.append(EvidenceBlock(block_id=f"{page.page_id}:block:{index}",
            span=span(buffer_id, 0, len(text)) if buffer_id else None, bbox=item.get("bbox"),
            native_order=int(item.get("native_order", index)), direction=item.get("direction"), role=role,
            signal_kind=item.get("signal_kind", "unreported")))
        offset = 0
        for line in item.get("lines", []):
            value = str(line.get("text") or "")
            if buffer_id and line.get("bbox") is not None and line.get("direction") is not None:
                page.blocks[-1].lines.append(EvidenceLine(span=span(buffer_id, offset, offset+len(value)),
                    bbox=tuple(line["bbox"]), direction=tuple(line["direction"])))
            offset += len(value)+1
        if role == "figure":
            warn(doc, "figure_evidence_unresolved", "Native figure region is not interpreted or clinically verified.",
                 page.page_number, True, bbox=tuple(item["bbox"]) if item.get("bbox") else None, block_id=page.blocks[-1].block_id)
        elif role == "visual_region":
            warn(doc, "visual_region_unclassified", "Unclassified native image/drawing region; clinical relevance and decorative status are unknown.",
                 page.page_number, False, bbox=tuple(item["bbox"]) if item.get("bbox") else None, block_id=page.blocks[-1].block_id)
    if not layout:
        warn(doc, "geometry_unavailable", "Native geometry was unavailable; reading order is unverified.", page.page_number, True)
    for a in page.blocks:
        if a.direction and (abs(a.direction[0] - 1) > .01 or abs(a.direction[1]) > .01):
            warn(doc, "reading_order_unverified", "Rotated native text requires reading-order review.", page.page_number, True)
        if not a.bbox or a.span is None: continue
        for b in page.blocks:
            if not b.bbox or b.span is None: continue
            if a.bbox[2] < b.bbox[0] and min(a.bbox[3], b.bbox[3]) - max(a.bbox[1], b.bbox[1]) > 20:
                warn(doc, "reading_order_unverified", "Overlapping column regions have unverified reading order.", page.page_number, True)


def get_evidence(extraction: dict[str, Any]) -> EvidenceDocument:
    value = extraction.get("evidence")
    if value is None:
        return build_evidence_document(extraction)
    doc = EvidenceDocument.model_validate(value)
    validate_evidence(doc)
    if doc.buffers[doc.canonical_buffer_id].text != str(extraction.get("full_text") or ""):
        raise ValueError("Canonical evidence does not match the supplied generation source buffer.")
    return doc


def _check_span(doc: EvidenceDocument, item: EvidenceSpan) -> None:
    if (item.buffer_id not in doc.buffers or item.char_start < 0 or item.char_end < item.char_start
            or item.char_end > len(doc.buffers[item.buffer_id].text)):
        raise ValueError("Evidence span is outside its named buffer.")


def validate_evidence(doc: EvidenceDocument) -> None:
    doc._warning_index = {w.warning_id for w in doc.warnings}
    doc._warning_index_signature = (id(doc.warnings),len(doc.warnings))
    if doc.config_hash != text_hash(json.dumps({"options": doc.configuration, "versions": doc.extractor_versions}, sort_keys=True)):
        raise ValueError("Evidence configuration fingerprint mismatch.")
    for buffer_id in (doc.raw_buffer_id, doc.cleaned_buffer_id, doc.canonical_buffer_id):
        if buffer_id not in doc.buffers:
            raise ValueError("Document references an unknown evidence buffer.")
    for key, buffer in doc.buffers.items():
        if key != buffer.buffer_id or buffer.text_sha256 != text_hash(buffer.text) or not key.endswith(buffer.text_sha256):
            raise ValueError("Evidence buffer identity/content mismatch.")
        if buffer.stage != "raw" and key not in doc.mappings:
            raise ValueError("Derived evidence buffer is missing its mapping.")
    for target_id, mappings in doc.mappings.items():
        cursor = 0
        for mapping in mappings:
            _check_span(doc, mapping.target)
            if mapping.target.buffer_id != target_id:
                raise ValueError("Mapping targets a different named buffer.")
            if mapping.source:
                _check_span(doc, mapping.source)
            if mapping.relation == "exact":
                a, b = mapping.source, mapping.target
                if a is None or doc.buffers[a.buffer_id].text[a.char_start:a.char_end] != doc.buffers[b.buffer_id].text[b.char_start:b.char_end]:
                    raise ValueError("Exact mapping does not preserve source characters.")
            if mapping.target.char_start != mapping.target.char_end:
                if mapping.target.char_start != cursor:
                    raise ValueError("Evidence mapping has a gap or overlap.")
                cursor = mapping.target.char_end
        if cursor != len(doc.buffers[target_id].text):
            raise ValueError("Evidence mapping does not account for the complete target buffer.")
    for page in doc.pages:
        for buffer_id in [page.selected_buffer_id, page.cleaned_buffer_id] + [c.buffer_id for c in page.candidates]:
            if buffer_id not in doc.buffers:
                raise ValueError("Page references an unknown evidence buffer.")
        for block in page.blocks:
            if block.span: _check_span(doc, block.span)
            for line in block.lines:
                _check_span(doc, line.span)
                if not block.span or line.span.buffer_id != block.span.buffer_id or not (block.span.char_start <= line.span.char_start <= line.span.char_end <= block.span.char_end):
                    raise ValueError("Native line lies outside its source block.")
    for warning in doc.warnings:
        if warning.span: _check_span(doc, warning.span)
    if doc.processing_routes or doc.native_layout_views:
        from .native_layout import validate_layout_views
        validate_layout_views(doc)


def trace_to_raw(doc: EvidenceDocument, item: EvidenceSpan) -> EvidenceTrace:
    trace = EvidenceTrace()
    def walk(current: EvidenceSpan, precision: str, visited: frozenset[str]) -> None:
        _check_span(doc, current)
        if current.buffer_id in visited:
            raise ValueError("Cyclic evidence mapping.")
        if current.char_start == current.char_end:
            return
        mappings = doc.mappings.get(current.buffer_id)
        if mappings is None:
            if doc.buffers[current.buffer_id].stage == "raw":
                trace.fragments.append(TraceFragment(source=current, precision=precision))
            else:
                trace.unmapped_spans.append(current)
            return
        for mapping in mappings:
            start = max(current.char_start, mapping.target.char_start)
            end = min(current.char_end, mapping.target.char_end)
            if start >= end: continue
            if mapping.source is None:
                trace.synthetic_spans.append(span(current.buffer_id, start, end))
            else:
                source = mapping.source
                if mapping.relation == "exact":
                    offset = start - mapping.target.char_start
                    source = span(source.buffer_id, source.char_start+offset, source.char_start+offset+end-start)
                walk(source, "region" if mapping.relation != "exact" else precision, visited | {current.buffer_id})
    walk(item, "exact", frozenset())
    unique = {(f.source.buffer_id, f.source.char_start, f.source.char_end, f.precision): f for f in trace.fragments}
    trace.fragments = list(unique.values())
    return trace

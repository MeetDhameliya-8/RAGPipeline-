"""Opt-in native extraction views and unavailable external capability routes."""
from __future__ import annotations

from .diagnostics import timed

from collections import Counter
import json
import math
import re

from .evidence import (EvidenceDocument, EvidenceRoute, NativeLayoutView, add_buffer,
    get_evidence, join_buffers, span, text_hash, validate_evidence, warning_pages, _check_span)
from .draft_support import boxes_overlap


VERSION = "native-xycut-v1"
CONFIG = {"min_column_gap": 6, "min_row_gap": 3, "max_blocks": 1500, "max_depth": 64}


def _fingerprint(doc):
    return text_hash(json.dumps({"version": VERSION, "options": CONFIG, "extractors": doc.extractor_versions}, sort_keys=True))


def _comparison(original, alternate, count):
    before = Counter(re.findall(r"\S+", original));after = Counter(re.findall(r"\S+", alternate))
    return {"original_characters":len(original), "alternate_characters":len(alternate),
        "native_blocks":count, "unrepresented_native_blocks":0,
        "tokens_only_in_original":sum((before-after).values()), "tokens_only_in_alternate":sum((after-before).values())}


def _ordered(blocks, depth=0):
    if len(blocks) <= 1:
        return blocks
    if depth >= CONFIG["max_depth"]:
        raise ValueError("layout_depth_limit")
    for axis, minimum in ((0, CONFIG["min_column_gap"]), (1, CONFIG["min_row_gap"])):
        intervals = sorted((b.bbox[axis], b.bbox[axis+2]) for b in blocks)
        end = intervals[0][1];gaps = []
        for lo, hi in intervals[1:]:
            if lo-end >= minimum:gaps.append((lo-end, (lo+end)/2))
            end = max(end, hi)
        if gaps:
            _, cut = max(gaps)
            first = [b for b in blocks if b.bbox[axis+2] <= cut]
            second = [b for b in blocks if b.bbox[axis] >= cut]
            if first and second and len(first)+len(second) == len(blocks):
                return _ordered(first, depth+1)+_ordered(second, depth+1)
    raise ValueError("layout_no_unambiguous_whitespace_cut")


def _routes(doc):
    by_page = {}
    by_block = {}
    for w in doc.warnings:
        if w.block_id:
            by_block.setdefault(w.block_id,[]).append(w.warning_id)
        for number in warning_pages(doc, w):by_page.setdefault(number, []).append(w)
    result = []
    for page in doc.pages:
        page_box = (0., 0., page.width, page.height) if page.width and page.height else None
        page_warnings = by_page.get(page.page_number,[])
        page_wide = [w.warning_id for w in page_warnings if not w.block_id]
        def add(kind, status, reasons, blocks=(), box=None):
            ids = [b.block_id for b in blocks]
            if kind in {"native_text","native_layout","ocr_required"}:
                warning_ids = [w.warning_id for w in page_warnings]
            else:
                # A region owns its own warnings plus page-wide uncertainty.
                # All other localized warnings remain on their source regions
                # and in the document-wide coverage/review record.
                warning_ids = list(page_wide)
                warning_ids.extend(wid for block in blocks for wid in by_block.get(block.block_id,[]))
            if kind in {"visual_classification_review", "table_relationship_review", "figure_review"} and len(blocks)==1 and blocks[0].bbox is None:
                reasons = reasons+["native_region_geometry_unavailable"]
            record = EvidenceRoute(route_id="route:"+text_hash(f"{doc.document_id}:{VERSION}:{page.page_number}:{kind}:{ids}"),
                kind=kind, status=status, page_number=page.page_number, bbox=box or page_box,
                block_ids=ids, source_spans=[b.span for b in blocks if b.span], warning_ids=warning_ids,
                reason_codes=reasons, original_buffer_id=page.selected_buffer_id)
            result.append(record)
        text_blocks = [b for b in page.blocks if b.span]
        has_text = bool(doc.buffers[page.selected_buffer_id].text.strip())
        if not has_text:
            add("ocr_required", "unavailable", ["no_machine_readable_text", "capability_not_configured", "external_processing_not_authorized"])
        codes = {w.code for w in doc.warnings if w.page_number == page.page_number}
        add("native_layout" if "reading_order_unverified" in codes else "native_text",
            "held" if not has_text else "proposed_native" if "reading_order_unverified" in codes else "retained_native",
            ["native_text_empty"] if not has_text else ["native_order_requires_comparison"] if "reading_order_unverified" in codes else ["original_native_view_retained"], text_blocks)
        for kind, role, code in (("table_relationship_review", "table_caption", "table_relationships_unresolved"),
                                 ("figure_review", "figure_caption", "figure_evidence_unresolved")):
            regions = [b for b in page.blocks if b.role == role]
            for block in regions:add(kind, "held", ["relationships_not_established"], [block], block.bbox)
            if code in codes and not regions:add(kind, "held", ["dependency_detected_region_unlocalized"], text_blocks)
        for block in page.blocks:
            if block.role == "visual_region":
                add("visual_classification_review", "held", ["unclassified_"+block.signal_kind, "clinical_relevance_unknown", "visual_processing_not_authorized"], [block], block.bbox)
    return result


def inspect_pdf_routes(pdf_path):
    """Inspect even image-only evidence, without treating a failed extraction as text."""
    from .pdf_extractor import extract_pdf, PdfExtractionError
    try:
        extraction = extract_pdf(pdf_path)
    except PdfExtractionError as exc:
        if exc.extraction is None:raise
        extraction = exc.extraction
    doc = get_evidence(extraction)
    doc.processing_routes = _routes(doc)
    validate_evidence(doc)
    return dict(extraction, evidence=doc.model_dump(mode="json"), processing_outcome=
        "text_available_with_holds" if extraction["full_text"].strip() else "requires_external_capability")


@timed("native_layout")
def recover_native_layout(extraction: dict) -> dict:
    """Return a separate, source-mapped draft view. Never change the input view."""
    doc = get_evidence(extraction)
    if doc.native_layout_views:return extraction
    doc.processing_routes = _routes(doc)
    pages = [dict(p) for p in extraction.get("pages", [])]
    page_buffers = [];selected = []
    for page in doc.pages:
        route = next(r for r in doc.processing_routes if r.page_number == page.page_number and r.kind in {"native_text", "native_layout"})
        if route.kind != "native_layout":
            page_buffers.append(page.cleaned_buffer_id);continue
        blocks = [b for b in page.blocks if b.span]
        try:
            if not blocks or len(blocks) > CONFIG["max_blocks"]:raise ValueError("layout_block_limit_or_empty")
            if page.rotation not in {0, None}:raise ValueError("page_rotation_unresolved")
            if any(not b.bbox or not all(math.isfinite(v) for v in b.bbox) or b.bbox[0]>=b.bbox[2] or b.bbox[1]>=b.bbox[3] for b in blocks):
                raise ValueError("native_geometry_incomplete")
            if any(boxes_overlap(a.bbox, b.bbox) for i,a in enumerate(blocks) for b in blocks[i+1:]):
                raise ValueError("native_text_regions_overlap")
            if any(b.span.char_start != 0 or b.span.char_end != len(doc.buffers[b.span.buffer_id].text) for b in blocks):
                raise ValueError("native_block_extent_incomplete")
            ordered = _ordered(blocks)
        except ValueError as exc:
            route.status = "held";route.reason_codes.append(str(exc))
            page_buffers.append(page.cleaned_buffer_id);continue
        alternate = join_buffers(doc, [b.span.buffer_id for b in ordered], "cleaned", f"{VERSION}:page:{page.page_number}")
        route.status = "reconstructed";route.alternate_buffer_id = alternate
        route.block_ids = [b.block_id for b in ordered];route.source_spans = [b.span for b in ordered]
        original = doc.buffers[page.selected_buffer_id].text;alternate_text = doc.buffers[alternate].text
        route.comparison = _comparison(original, alternate_text, len(ordered))
        selected.append(route.route_id);page_buffers.append(alternate)
        target = next(p for p in pages if p["page_number"] == page.page_number)
        target.update(text=alternate_text, extractor="native_xycut_proposal")
    full_text = "\n\n".join(doc.buffers[i].text for i in page_buffers)
    if selected and full_text != extraction["full_text"]:
        original_id, original_cleaned = doc.canonical_buffer_id, doc.cleaned_buffer_id
        cleaned = join_buffers(doc, page_buffers, "cleaned", VERSION+":document")
        canonical = join_buffers(doc, [cleaned], "canonical", VERSION)
        doc.cleaned_buffer_id = cleaned;doc.canonical_buffer_id = canonical
        fingerprint = _fingerprint(doc)
        doc.native_layout_views.append(NativeLayoutView(view_id="view:"+text_hash(original_id+canonical+fingerprint),
            config_hash=fingerprint, configuration=CONFIG, original_canonical_id=original_id, original_cleaned_id=original_cleaned,
            alternate_canonical_id=canonical, alternate_cleaned_id=cleaned, page_buffer_ids=page_buffers, route_ids=selected))
        outcome = "native_layout_proposed"
    else:
        full_text = extraction["full_text"];outcome = "text_available_with_holds" if full_text.strip() else "requires_external_capability"
        pages = [dict(p) for p in extraction.get("pages", [])]
    validate_evidence(doc)
    return dict(extraction, pages=pages, full_text=full_text, evidence=doc.model_dump(mode="json"), processing_outcome=outcome)


def validate_layout_views(doc: EvidenceDocument) -> None:
    blocks = {b.block_id:b for p in doc.pages for b in p.blocks}
    pages = {p.page_number:p for p in doc.pages}
    page_blocks = {p.page_number:{b.block_id for b in p.blocks} for p in doc.pages}
    warnings = {w.warning_id for w in doc.warnings}
    routes = {r.route_id:r for r in doc.processing_routes}
    if len(routes) != len(doc.processing_routes):raise ValueError("Duplicate evidence route.")
    for route in doc.processing_routes:
        if route.page_number not in pages or any(i not in blocks for i in route.block_ids) or not set(route.warning_ids)<=warnings:
            raise ValueError("Invalid evidence route identity.")
        if not set(route.block_ids)<=page_blocks[route.page_number]:raise ValueError("Evidence route crosses pages.")
        for s in route.source_spans:_check_span(doc, s)
        if route.source_spans != [blocks[i].span for i in route.block_ids if blocks[i].span]:raise ValueError("Route source spans differ from native blocks.")
        for key in [route.original_buffer_id, route.alternate_buffer_id]:
            if key is not None and key not in doc.buffers:raise ValueError("Unknown route buffer.")
        if route.status == "reconstructed":
            all_blocks = [b for b in pages[route.page_number].blocks if b.span]
            if route.kind != "native_layout" or len(route.block_ids)!=len(all_blocks) or set(route.block_ids)!={b.block_id for b in all_blocks}:
                raise ValueError("Native layout omitted or duplicated a source block.")
            if route.block_ids != [b.block_id for b in _ordered(all_blocks)]:raise ValueError("Native layout ordering changed.")
            expected = "\n\n".join(doc.buffers[s.buffer_id].text[s.char_start:s.char_end] for s in route.source_spans)
            if route.alternate_buffer_id is None or doc.buffers[route.alternate_buffer_id].text != expected:
                raise ValueError("Native layout buffer differs from original blocks.")
            if route.comparison != _comparison(doc.buffers[route.original_buffer_id].text, expected, len(all_blocks)):
                raise ValueError("Native layout comparison differs from source buffers.")
    for view in doc.native_layout_views:
        ids = [view.original_canonical_id,view.original_cleaned_id,view.alternate_canonical_id,view.alternate_cleaned_id]+view.page_buffer_ids
        if any(i not in doc.buffers for i in ids) or view.configuration != CONFIG or view.config_hash != _fingerprint(doc):
            raise ValueError("Invalid native layout view identity/configuration.")
        if doc.canonical_buffer_id != view.alternate_canonical_id or doc.cleaned_buffer_id != view.alternate_cleaned_id or view.original_canonical_id == view.alternate_canonical_id:
            raise ValueError("Invalid selected native layout view.")
        if len(view.page_buffer_ids) != len(doc.pages) or not view.route_ids or any(i not in routes or routes[i].status!="reconstructed" for i in view.route_ids):
            raise ValueError("Incomplete native layout page view.")
        if len(set(view.route_ids)) != len(view.route_ids) or set(view.route_ids) != {r.route_id for r in doc.processing_routes if r.status=="reconstructed"}:
            raise ValueError("Native layout view has invalid route ownership.")
        for page, key in zip(doc.pages, view.page_buffer_ids):
            selected = [routes[rid] for rid in view.route_ids if routes[rid].page_number==page.page_number]
            if len(selected)>1 or key!=(selected[0].alternate_buffer_id if selected else page.cleaned_buffer_id):
                raise ValueError("Native layout page buffer differs from selected route.")
        expected = "\n\n".join(doc.buffers[i].text for i in view.page_buffer_ids)
        if doc.buffers[view.alternate_canonical_id].text!=expected or doc.buffers[view.alternate_cleaned_id].text!=expected:
            raise ValueError("Native layout view lost source text.")

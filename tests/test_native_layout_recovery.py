"""P1-C2 opt-in extraction views; external capabilities are unavailable."""
import importlib
from hashlib import sha256

import fitz
import pytest

from src.evidence import get_evidence, span, trace_to_raw, validate_evidence
from src.pdf_extractor import extract_pdf
from src.source_map import build_source_map, build_coverage_report, initial_processing_state
from src.concept_planner import build_clinical_plan, validate_clinical_plan


TEXT = "Adults with active colitis received treatment alpha for 8 weeks."


def api():
    return importlib.import_module("src.native_layout")


def pdf(tmp_path, kind="columns"):
    path = tmp_path / (kind + ".pdf")
    d = fitz.open();p = d.new_page(width=600, height=800)
    if kind == "scan":
        from PIL import Image
        import io
        out = io.BytesIO();Image.new("RGB", (300, 200), "gray").save(out, format="PNG")
        p.insert_image(fitz.Rect(50, 100, 550, 400), stream=out.getvalue())
    else:
        p.insert_text((40, 55), "Methods (cid:1)" if kind=="encoding" else "Methods", fontsize=12)
        p.insert_textbox(fitz.Rect(40, 100, 285, 350), TEXT.replace("8", "(cid:42)") if kind=="encoding" else TEXT, fontsize=11)
        p.insert_textbox(fitz.Rect(330, 100, 560, 350), "Supplementary material\nUnresolved source data.", fontsize=11)
        if kind == "visual":p.draw_rect(fitz.Rect(350, 400, 550, 650))
        if kind == "table":p.insert_text((40, 450), "Table 1. Treatment results", fontsize=10)
    d.save(path);d.close()
    return path


def plan(e):
    mapped = build_source_map(e)
    mapped.clinical_plan = build_clinical_plan(e, mapped)
    return mapped, mapped.clinical_plan


def eligible(p):
    return [u for u in p.units if u.generation_eligibility == "eligible_draft"]


def test_previous_interleaving_recovers_with_both_views_and_exact_native_offsets(tmp_path):
    path = pdf(tmp_path);original_hash = sha256(path.read_bytes()).hexdigest()
    before = extract_pdf(path);before_json = get_evidence(before).model_dump_json()
    assert not eligible(plan(before)[1])
    after = api().recover_native_layout(before)
    assert get_evidence(before).model_dump_json() == before_json
    assert after["full_text"] != before["full_text"]
    assert TEXT == " ".join(after["full_text"].split())[8:8+len(TEXT)]
    d = get_evidence(after);view = d.native_layout_views[-1]
    assert d.buffers[view.original_canonical_id].text == before["full_text"]
    assert d.buffers[view.alternate_canonical_id].text == after["full_text"]
    assert {w.warning_id for w in get_evidence(before).warnings} <= {w.warning_id for w in d.warnings}
    trace = trace_to_raw(d, span(d.canonical_buffer_id, 0, len(after["full_text"])))
    assert not trace.unmapped_spans and all(f.precision == "exact" for f in trace.fragments)
    assert all(not d.buffers[s.buffer_id].text[s.char_start:s.char_end].strip() for s in trace.synthetic_spans)
    mapped, p = plan(after)
    assert eligible(p)
    c = next(c for c in p.concepts if c.generation_eligibility == "eligible_draft")
    assert c.draft_support.version == "native-block-lines-v2"
    assert c.draft_support.line_support
    validate_clinical_plan(p, mapped.evidence)
    assert not build_coverage_report(mapped, initial_processing_state(mapped))["source_coverage_complete"]
    assert sha256(path.read_bytes()).hexdigest() == original_hash


@pytest.mark.parametrize("kind,route", [("table", "table_relationship_review"), ("visual", "visual_classification_review"), ("scan", "ocr_required")])
def test_explicit_region_routes_do_not_activate_services(tmp_path, kind, route):
    inspected = api().inspect_pdf_routes(pdf(tmp_path, kind))
    d = get_evidence(inspected)
    found = [r for r in d.processing_routes if r.kind == route]
    assert found and all(r.page_number == 1 and r.bbox for r in found)
    assert all(r.status in {"held", "unavailable"} for r in found)
    assert all(r.reason_codes for r in found)
    if kind == "scan":
        assert inspected["processing_outcome"] == "requires_external_capability"
        assert not inspected["full_text"]
        assert not eligible(plan(inspected)[1])
        assert all(r.status=="held" for r in d.processing_routes if r.kind=="native_text")
        from src.chunk_generator import generate_chunks, ChunkGenerationError
        from unittest.mock import Mock
        provider = Mock()
        with pytest.raises(ChunkGenerationError, match="eligible draft units"):
            generate_chunks(inspected, provider=provider)
        assert not provider.generate.called


def test_no_missing_blocks_legacy_storage_and_tampered_view_rejects(tmp_path):
    from src.evidence_storage import save_source_map, load_source_map
    before = extract_pdf(pdf(tmp_path));after = api().recover_native_layout(before)
    d = get_evidence(after);view = d.native_layout_views[-1]
    route = next(r for r in d.processing_routes if r.kind == "native_layout" and r.status == "reconstructed")
    assert set(route.block_ids) == {b.block_id for b in d.pages[0].blocks if b.span}
    mapped, _ = plan(after);path = tmp_path / "review.json"
    save_source_map(path, mapped)
    assert load_source_map(path).model_dump_json() == mapped.model_dump_json()
    assert api().recover_native_layout(after) == after  # No stacked views or loops.
    d.native_layout_views[-1].alternate_canonical_id = view.original_canonical_id
    with pytest.raises(ValueError, match="view|layout"):
        validate_evidence(d)


@pytest.mark.parametrize("failure", ["overlap", "rotation", "direction", "line_disagreement", "unknown_geometry"])
def test_unsafe_layout_or_source_conflicts_stay_held(tmp_path, failure):
    from src.evidence import EvidenceBlock, add_buffer
    e = extract_pdf(pdf(tmp_path));d = get_evidence(e)
    if failure == "rotation":d.pages[0].rotation = 90
    if failure == "direction":d.pages[0].blocks[1].direction = (0, 1)
    if failure == "unknown_geometry":d.pages[0].blocks[1].bbox = None
    if failure == "overlap":
        d.pages[0].blocks.append(EvidenceBlock(block_id="overprint", native_order=9, bbox=d.pages[0].blocks[1].bbox,
            direction=(1, 0), span=span(add_buffer(d,"raw","overprint","Unrelated text",1),0,14)))
    if failure == "line_disagreement":
        c = next(c for c in d.pages[0].candidates if c.extractor == "pypdf")
        c.buffer_id = add_buffer(d, "raw", "bad-candidate", d.buffers[c.buffer_id].text.replace("8", "9"), 1, "pypdf")
    e["evidence"] = d.model_dump(mode="json")
    recovered = api().recover_native_layout(e)
    assert not eligible(plan(recovered)[1])


def test_narrow_positive_column_gutter_preserves_column_order():
    from types import SimpleNamespace as Block
    left = Block(bbox=(40, 100, 291, 600))
    right = Block(bbox=(299, 100, 550, 600))
    assert api()._ordered([right, left]) == [left, right]


def test_located_old_view_warning_requires_explicit_cross_view_support(tmp_path):
    from src.draft_support import NativeSupportIndex
    from src.evidence import EvidenceTrace, EvidenceWarning
    before = extract_pdf(pdf(tmp_path));after = api().recover_native_layout(before)
    mapped, p = plan(after)
    proof = next(c.draft_support for c in p.concepts if c.draft_support)
    old = get_evidence(before).canonical_buffer_id
    start = before["full_text"].index("Unresolved")
    warning = EvidenceWarning(warning_id="located-old",code="text_transformation_unreviewed",message="Old view change elsewhere",critical=True,
        span=span(old,start,start+10))
    assert NativeSupportIndex(mapped.evidence).warning_independent(proof, warning, EvidenceTrace())
    warning.span = None
    assert not NativeSupportIndex(mapped.evidence).warning_independent(proof, warning, EvidenceTrace())


@pytest.mark.parametrize("change", ["comparison", "duplicate_route"])
def test_view_comparison_and_route_identity_are_verified(tmp_path, change):
    d = get_evidence(api().recover_native_layout(extract_pdf(pdf(tmp_path))))
    if change == "comparison":
        r = next(r for r in d.processing_routes if r.status == "reconstructed")
        r.comparison["unrepresented_native_blocks"] = 1
    else:d.native_layout_views[-1].route_ids *= 2
    with pytest.raises(ValueError, match="comparison|route|view"):
        validate_evidence(d)


def test_unknown_visual_geometry_is_explicitly_unlocalized(tmp_path):
    d = get_evidence(extract_pdf(pdf(tmp_path, "visual")))
    next(b for b in d.pages[0].blocks if b.role=="visual_region").bbox = None
    route = next(r for r in api()._routes(d) if r.kind=="visual_classification_review")
    assert "native_region_geometry_unavailable" in route.reason_codes


def test_later_font_code_corruption_is_not_licensed_by_first_warning_elsewhere(tmp_path):
    recovered = api().recover_native_layout(extract_pdf(pdf(tmp_path, "encoding")))
    assert not eligible(plan(recovered)[1])

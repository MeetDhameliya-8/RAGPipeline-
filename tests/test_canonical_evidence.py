"""P1-A provenance tests: exact offsets, explicit uncertainty, no model calls."""
from hashlib import sha256
import importlib
import json

import fitz
import pytest

from src.pdf_extractor import extract_pdf, clean_article_text, normalize_page_text
from src.source_map import build_source_map, build_coverage_report, initial_processing_state
from src.adaptive_ingestion import build_ingestion_plan, AdaptiveConfig


def api():
    return importlib.import_module("src.evidence")


def source(raw_pages):
    pages = [{"page_number": n, "raw_text": text, "text": normalize_page_text(text), "extractor": "test-native"}
             for n, text in enumerate(raw_pages, 1)]
    return {"file_name": "unfamiliar.pdf", "page_count": len(pages), "pages": pages,
            "raw_full_text": "\n\n".join(raw_pages),
            "full_text": clean_article_text("\n\n".join(p["text"] for p in pages)), "extraction_warnings": []}


def make_pdf(path, columns=False, figure=False):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(40, 40, 260, 190), "Methods\n" + "Left column source sentence. " * 9, fontsize=10)
        if columns:
            page.insert_textbox(fitz.Rect(320, 40, 550, 190), "Results\n" + "Right column evidence sentence. " * 9, fontsize=10)
        if figure:
            page.insert_text((40, 230), "Figure 1. Outcome diagram.")
            page.draw_rect(fitz.Rect(40, 250, 260, 380))
        doc.save(path)


def test_raw_clean_canonical_buffers_have_exact_roundtrip_and_named_offsets():
    e = source(["Methods\n\nTreatment A continued for 8 weeks.\n\nDownloaded from https://example.test"])
    doc = api().get_evidence(e)
    assert doc.buffers[doc.canonical_buffer_id].text == e["full_text"]
    assert {b.stage for b in doc.buffers.values()} >= {"raw", "cleaned", "canonical"}
    api().validate_evidence(doc)
    text = doc.buffers[doc.canonical_buffer_id].text
    start = text.index("8 weeks")
    traced = api().trace_to_raw(doc, api().EvidenceSpan(buffer_id=doc.canonical_buffer_id, char_start=start, char_end=start+7))
    assert traced.fragments
    assert any("8 weeks" in doc.buffers[f.source.buffer_id].text[f.source.char_start:f.source.char_end] for f in traced.fragments)
    assert all(f.source.buffer_id in doc.buffers for f in traced.fragments)


def test_cross_page_trace_returns_both_original_pages_and_synthetic_separators():
    e = source(["Methods\n\nTreatment continued", "for 8 weeks before reassessment."])
    doc = api().get_evidence(e)
    trace = api().trace_to_raw(doc, api().EvidenceSpan(buffer_id=doc.canonical_buffer_id, char_start=0, char_end=len(e["full_text"])))
    assert {doc.buffers[f.source.buffer_id].page_number for f in trace.fragments} == {1, 2}
    assert trace.synthetic_spans
    assert not trace.unmapped_spans


def test_transform_and_deletion_are_recorded_without_invented_character_alignment():
    e = source(["Methods\n\nTreat-\nment continued.\n\nReferences\n\nCited source 2020."])
    doc = api().get_evidence(e)
    relations = {m.relation for mappings in doc.mappings.values() for m in mappings}
    assert "transformed" in relations
    assert "deleted" in relations
    for buffer in doc.buffers.values():
        assert buffer.text_sha256 == sha256(buffer.text.encode()).hexdigest()


def test_section_offsets_name_their_actual_buffer_not_generation_text():
    e = source(["Methods\n\nStudy evidence.\n\nReferences\n\n1. Cited study."])
    mapped = build_source_map(e)
    assert mapped.evidence is not None
    for section in mapped.sections:
        assert section.buffer_id == mapped.buffer_id
        assert mapped.evidence.buffers[section.buffer_id].text[section.char_start:section.char_end] == section.text
        assert section.source_spans
    assert mapped.buffer_id != mapped.evidence.canonical_buffer_id


def test_generation_units_gain_ownership_without_changing_text_or_boundaries():
    e = source([("A complete source sentence. " * 20 + "\n\n") * 15])
    plan = build_ingestion_plan(e, AdaptiveConfig(max_batch_chars=1000))
    assert ''.join(b.text for b in plan.batches) == e['full_text']
    for batch in plan.batches:
        assert batch.ownership[0].buffer_id == plan.buffer_id
        assert batch.ownership[0].char_start == batch.char_start
        assert batch.ownership[0].char_end == batch.char_end
        assert batch.context_dependencies == []
        assert batch.dependency_status == "not_planned"


def test_broken_encoding_and_unknown_signs_cannot_complete_source_coverage():
    e = source(["Results\n\nTreatment changed by \u00fe0.20% and /H11021 units.\n"])
    doc = api().get_evidence(e)
    assert any(w.critical and w.code == "encoding_sign_unit_unresolved" for w in doc.warnings)
    mapped = build_source_map(e)
    state = initial_processing_state(mapped)
    state.sections = [r.model_copy(update={"status": "processed"}) for r in state.sections]
    report = build_coverage_report(mapped, state)
    assert report["source_coverage_complete"] is False
    assert report["unresolved_critical_evidence"]


def test_native_multi_column_geometry_and_reading_order_uncertainty(tmp_path):
    path = tmp_path / "different-layout.pdf"
    make_pdf(path, columns=True)
    e = extract_pdf(path)
    doc = api().get_evidence(e)
    page = doc.pages[0]
    assert page.width > 0 and page.height > 0
    assert page.blocks and all(b.bbox is not None for b in page.blocks)
    assert any(w.code == "reading_order_unverified" and w.critical for w in doc.warnings)
    assert len(page.candidates) == 2
    assert page.selection_reason
    assert page.native_order_verified is False


def test_pdf_bytes_and_page_identity_are_independent_of_filename(tmp_path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "renamed.pdf"
    make_pdf(first)
    second.write_bytes(first.read_bytes())
    a = api().get_evidence(extract_pdf(first))
    b = api().get_evidence(extract_pdf(second))
    assert a.pdf_sha256 == b.pdf_sha256 == sha256(first.read_bytes()).hexdigest()
    assert a.document_id == b.document_id
    assert a.pages[0].page_id == b.pages[0].page_id
    assert a.config_hash and a.extractor_versions


def test_incomplete_figure_relationships_remain_visible(tmp_path):
    path = tmp_path / "figure-heavy.pdf"
    make_pdf(path, figure=True)
    doc = api().get_evidence(extract_pdf(path))
    assert any(w.code == "figure_evidence_unresolved" and w.critical for w in doc.warnings)
    assert any(b.role in {"figure", "figure_caption", "visual_region"} for b in doc.pages[0].blocks)


def test_extractor_disagreement_retains_both_candidates(tmp_path, monkeypatch):
    path = tmp_path / "source.pdf"
    make_pdf(path)
    original = __import__("src.pdf_extractor", fromlist=["x"])
    native = original._extract_pymupdf_pages
    def disagree(path, warnings):
        pages = native(path, warnings)
        pages[0]["raw_text"] = "A different reading sequence with unresolved source content. " * 12
        return pages
    monkeypatch.setattr(original, "_extract_pymupdf_pages", disagree)
    doc = api().get_evidence(extract_pdf(path))
    page = doc.pages[0]
    assert len(page.candidates) == 2
    assert any(w.code == "extractor_disagreement" for w in doc.warnings)
    assert len({doc.buffers[c.buffer_id].text for c in page.candidates}) == 2


def test_tampered_buffer_or_out_of_range_span_fails_closed():
    e = source(["Verified source sentence."])
    doc = api().get_evidence(e)
    broken = doc.model_copy(deep=True)
    broken.buffers[broken.canonical_buffer_id].text += "invented"
    with pytest.raises(ValueError):
        api().validate_evidence(broken)
    with pytest.raises(ValueError):
        api().trace_to_raw(doc, api().EvidenceSpan(buffer_id=doc.canonical_buffer_id, char_start=0, char_end=999999))


def test_legacy_text_callers_are_explicitly_unverified_not_claimed_pdf_identity():
    doc = api().get_evidence({"file_name": "legacy.pdf", "full_text": "Source text."})
    assert doc.pdf_sha256 is None
    assert doc.identity_quality == "legacy_text_only"
    assert doc.warnings
    assert json.loads(doc.model_dump_json())["canonical_buffer_id"]


def test_non_whitespace_loss_and_insertion_remain_critical():
    e = source(["Clinical evidence retained.\n\nA different clinical observation."])
    e["full_text"] = "Clinical evidence retained.\n\nInvented sentence."
    doc = api().get_evidence(e)
    assert any(w.code == "text_transformation_unreviewed" and w.critical for w in doc.warnings)


def test_extractor_configuration_changes_identity_without_changing_pdf_identity():
    e = source(["Original evidence."])
    a = api().build_evidence_document(e, pdf_sha256="a" * 64, configuration={"mode": "one"})
    b = api().build_evidence_document(e, pdf_sha256="a" * 64, configuration={"mode": "two"})
    assert a.document_id == b.document_id
    assert a.config_hash != b.config_hash
    assert a.pages[0].selected_buffer_id != b.pages[0].selected_buffer_id


def test_all_candidates_retain_encoding_uncertainty_even_when_not_selected():
    e = source(["The native evidence was retained."])
    candidates = [dict(e["pages"][0]), dict(e["pages"][0], extractor="alternate", raw_text="Table 1. Figure 1. Result /H11021 mg.")]
    doc = api().build_evidence_document(e, candidates=candidates)
    assert any(w.code == "encoding_sign_unit_unresolved" for w in doc.warnings)
    assert {"table_relationships_unresolved", "figure_evidence_unresolved"} <= {w.code for w in doc.warnings}


def test_legacy_raw_only_pages_trace_to_raw_without_fabricated_insertions():
    doc = api().get_evidence({"pages": [{"page_number": 1, "raw_text": "Original source."}], "full_text": "Original source."})
    trace = api().trace_to_raw(doc, api().span(doc.canonical_buffer_id, 0, 16))
    assert trace.fragments and not trace.synthetic_spans


def test_missing_derived_mapping_is_rejected():
    doc = api().get_evidence(source(["Original source."]))
    del doc.mappings[doc.canonical_buffer_id]
    with pytest.raises(ValueError, match="mapping"):
        api().validate_evidence(doc)


def test_source_map_roundtrip_keeps_trace_precision_and_legacy_defaults():
    from src.source_map import SourceDocument
    mapped = build_source_map(source(["Methods\n\nTreat-\nment continued."]))
    loaded = SourceDocument.model_validate_json(mapped.model_dump_json())
    assert loaded.sections[0].source_trace.fragments
    assert any(f.precision == "region" for s in loaded.sections for f in s.source_trace.fragments)
    old = mapped.model_dump(exclude={"evidence", "buffer_id"})
    for s in old["sections"]:
        for key in ("buffer_id", "source_spans", "source_trace"):
            s.pop(key, None)
    legacy = SourceDocument.model_validate(old)
    assert legacy.evidence is None
    assert build_coverage_report(legacy)["source_coverage_complete"] is False


def test_extracted_reference_spans_resolve_to_the_actual_native_buffer(tmp_path):
    path = tmp_path / "citation.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 40), "Journal 2020;12:200-210\ndoi:10.1234/source014")
        page.insert_textbox(fitz.Rect(40, 100, 500, 300), "Clinical source sentence. " * 30)
        pdf.save(path)
    extraction = extract_pdf(path)
    doc = api().get_evidence(extraction)
    assert extraction["reference_metadata"]["source_spans"]
    for item in extraction["reference_metadata"]["source_spans"]:
        assert item["buffer_id"] in doc.buffers
        assert doc.buffers[item["buffer_id"]].text[item["char_start"]:item["char_end"]] == item["text"]


def test_configuration_is_inspectable_and_integrity_checked():
    doc = api().get_evidence(source(["Evidence source."]))
    assert doc.configuration["version"] == doc.version
    doc.config_hash = "incorrect"
    with pytest.raises(ValueError, match="configuration"):
        api().validate_evidence(doc)


def test_mutated_span_cannot_bypass_bounds_checks():
    doc = api().get_evidence(source(["Evidence source."]))
    item = api().span(doc.canonical_buffer_id, 0, 5)
    item.char_start = -1
    with pytest.raises(ValueError):
        api().trace_to_raw(doc, item)


def test_unit_identity_includes_the_actual_owned_buffer_and_document_warning_ids():
    first = {"raw_full_text": "Same native evidence.", "full_text": "First source text."}
    second = dict(first, full_text="Other source text.")
    a, b = build_ingestion_plan(first), build_ingestion_plan(second)
    assert a.batches[0].unit_id != b.batches[0].unit_id
    assert a.batches[0].critical_warning_ids == a.critical_warning_ids


def test_geometry_survives_an_unavailable_visual_inventory(tmp_path, monkeypatch):
    path = tmp_path / "layout.pdf"
    make_pdf(path)
    def unavailable(*args, **kwargs):
        raise ValueError("Visual inventory unavailable")
    monkeypatch.setattr(fitz.Page, "get_image_info", unavailable)
    doc = api().get_evidence(extract_pdf(path))
    assert doc.pages[0].blocks and doc.pages[0].width > 0
    assert any(w.code == "visual_inventory_unavailable" and w.critical for w in doc.warnings)

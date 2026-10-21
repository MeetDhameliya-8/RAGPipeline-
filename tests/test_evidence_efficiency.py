"""P1-A.1 offline storage integrity and warning precision regressions."""
import importlib
import json

import pytest

from src.evidence import build_evidence_document, get_evidence, span, trace_to_raw, validate_evidence, EvidenceWarning, warn
from src.source_map import build_source_map, build_coverage_report, initial_processing_state
from src.adaptive_ingestion import build_ingestion_plan, AdaptiveConfig


def extraction(text="Methods\n\nTreat-\nment continued.\n\nReferences\n\nSource bibliography.", *, visual=False):
    from src.pdf_extractor import normalize_page_text, clean_article_text
    page = {"page_number": 1, "raw_text": text, "text": normalize_page_text(text), "extractor": "test"}
    result = {"file_name": "test.pdf", "page_count": 1, "pages": [page], "raw_full_text": text,
              "full_text": clean_article_text(page["text"])}
    layout = {"width": 600, "height": 800, "rotation": 0, "blocks": [
        {"text": text, "bbox": [40, 40, 550, 300], "native_order": 0}]}
    if visual:
        layout["blocks"].append({"bbox": [5, 750, 595, 751], "native_order": 1, "role": "visual_region"})
    doc = build_evidence_document(result, pdf_sha256="a"*64, candidates=[dict(page, layout=layout)])
    result["evidence"] = doc.model_dump(mode="json")
    return result


def storage():
    return importlib.import_module("src.evidence_storage")


def test_compressed_roundtrip_is_lossless_and_deterministic(tmp_path):
    original = build_source_map(extraction())
    paths = [tmp_path / "first.json", tmp_path / "second.json"]
    for path in paths:
        storage().save_source_map(path, original)
    assert paths[0].read_bytes() == paths[1].read_bytes()
    loaded = storage().load_source_map(paths[0])
    assert loaded.model_dump_json() == original.model_dump_json()
    validate_evidence(loaded.evidence)
    for section in loaded.sections:
        assert loaded.evidence.buffers[section.buffer_id].text[section.char_start:section.char_end] == section.text
        assert trace_to_raw(loaded.evidence, span(section.buffer_id, section.char_start, section.char_end)) == section.source_trace
    assert {m.relation for ms in loaded.evidence.mappings.values() for m in ms} >= {"transformed", "deleted"}


def test_legacy_json_readback_and_metadata_only_maps(tmp_path):
    doc = build_source_map(extraction())
    path = tmp_path / "legacy.json"
    path.write_text(doc.model_dump_json(indent=2), encoding="utf-8")
    assert storage().load_source_map(path).model_dump_json() == doc.model_dump_json()
    old = doc.model_dump(exclude={"evidence", "buffer_id"})
    for section in old["sections"]:
        for key in ("buffer_id", "source_spans", "source_trace"):
            section.pop(key)
    path.write_text(json.dumps(old), encoding="utf-8")
    assert storage().load_source_map(path).evidence is None


@pytest.mark.parametrize("field,value", [("sha256", "0"*64), ("format", "unknown"), ("uncompressed_bytes", 1), ("payload", "broken")])
def test_bad_envelope_fails_closed(tmp_path, field, value):
    path = tmp_path / "map.json"
    storage().save_source_map(path, build_source_map(extraction()))
    payload = json.loads(path.read_text())
    payload["_source_map_storage"][field] = value
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError):
        storage().load_source_map(path)


def test_storage_does_not_replace_existing_artifact(tmp_path):
    path = tmp_path / "old.json"
    path.write_text("Original artifact")
    with pytest.raises(FileExistsError):
        storage().save_source_map(path, build_source_map(extraction()))
    assert path.read_text() == "Original artifact"


def test_drawings_are_unclassified_signals_not_proven_clinical_figures():
    doc = get_evidence(extraction("Plain source evidence.", visual=True))
    warnings = [w for w in doc.warnings if w.classification == "unclassified_visual"]
    assert len(warnings) == 1 and not warnings[0].critical
    assert warnings[0].page_number == 1 and warnings[0].bbox == (5, 750, 595, 751)
    assert warnings[0].block_id
    assert not any(w.code == "figure_evidence_unresolved" for w in doc.warnings)


def test_uncertain_visual_still_prevents_false_complete_coverage():
    mapped = build_source_map(extraction("Plain source evidence.\n\nAdditional source evidence.", visual=True))
    state = initial_processing_state(mapped)
    state.sections = [r.model_copy(update={"status": "processed"}) for r in state.sections]
    report = build_coverage_report(mapped, state)
    assert not report["source_coverage_complete"]
    assert report["unresolved_uncertain_evidence"]
    assert not report["unresolved_critical_evidence"]


def test_critical_clinical_warning_is_not_downgraded_and_encoding_is_located():
    doc = get_evidence(extraction("Table 1. Figure 2. Dose /H11021 mg."))
    assert any(w.classification == "clinical_unresolved" and w.critical for w in doc.warnings)
    warning = next(w for w in doc.warnings if w.code == "encoding_sign_unit_unresolved")
    assert warning.classification == "possible_corruption"
    assert doc.buffers[warning.span.buffer_id].text[warning.span.char_start:warning.span.char_end] == "/H11021"


def test_legacy_critical_warning_remains_blocking_and_nonclinical_requires_basis():
    old = EvidenceWarning(warning_id="old", code="unknown_old_rule", message="Unreviewed", critical=True)
    assert old.classification == "legacy_unclassified"
    with pytest.raises(ValueError):
        EvidenceWarning(warning_id="bad", code="visual", message="Decorative", classification="supported_nonclinical")


def test_warning_source_overlap_is_associated_with_units_without_planning_recovery():
    e = extraction("A complete source sentence. "*200)
    doc = get_evidence(e)
    warn(doc, "test_clinical_gap", "Unresolved material result.", 1, True, span(doc.canonical_buffer_id, 0, 5))
    e["evidence"] = doc.model_dump(mode="json")
    plan = build_ingestion_plan(e, AdaptiveConfig(max_batch_chars=1000))
    assert plan.batches[0].warning_dependencies
    assert any(w.scope == "owned_span" for w in plan.batches[0].warning_dependencies)
    assert not plan.batches[-1].warning_dependencies
    assert all(b.dependency_status == "not_planned" for b in plan.batches)


def test_visual_inventory_is_excluded_from_text_column_detection():
    e = extraction("Left text column.")
    page = e["pages"][0]
    layout = {"width": 600, "height": 800, "blocks": [
        {"text": page["raw_text"], "bbox": [40, 40, 250, 300], "native_order": 0},
        {"bbox": [320, 40, 550, 300], "native_order": 1, "role": "visual_region"}]}
    doc = build_evidence_document(e, pdf_sha256="b"*64, candidates=[dict(page, layout=layout)])
    assert not any(w.code == "reading_order_unverified" for w in doc.warnings)


def test_supported_nonclinical_is_preserved_and_does_not_clear_a_critical_flag():
    from src.evidence import warning_blocks_coverage
    w = EvidenceWarning(warning_id="review-record", code="reviewed_region", message="Source region reviewed.",
        classification="supported_nonclinical", classification_basis="Independent source-region review record: local-review-1")
    # A basis string alone is not independent adjudication; explicit resolution
    # is still required through the existing review boundary.
    assert warning_blocks_coverage(w)
    assert not warning_blocks_coverage(w.model_copy(update={"resolved": True}))
    assert warning_blocks_coverage(w.model_copy(update={"critical": True}))


def test_confirmed_missing_stays_blocking_even_if_boolean_is_wrong():
    from src.evidence import warning_blocks_coverage
    w = EvidenceWarning(warning_id="missing", code="missing", message="No native page content.", classification="confirmed_missing")
    assert warning_blocks_coverage(w)


def test_decoding_is_size_bounded_and_envelope_identity_is_checked(tmp_path, monkeypatch):
    path = tmp_path / "map.json"
    storage().save_source_map(path, build_source_map(extraction()))
    payload = json.loads(path.read_text())
    payload["content_hash"] = "unrelated"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="identity"):
        storage().load_source_map(path)
    monkeypatch.setattr(storage(), "MAX_UNCOMPRESSED_BYTES", 1)
    with pytest.raises(ValueError, match="size"):
        storage().load_source_map(path)


def test_legacy_configuration_may_contain_the_storage_marker(tmp_path):
    e = extraction()
    doc = get_evidence(e)
    doc.configuration["_source_map_storage"] = "an ordinary legacy configuration key"
    from src.evidence import text_hash
    doc.config_hash = text_hash(json.dumps({"options": doc.configuration, "versions": doc.extractor_versions}, sort_keys=True))
    e["evidence"] = doc.model_dump(mode="json")
    mapped = build_source_map(e)
    path = tmp_path / "legacy-marker.json"
    path.write_text(mapped.model_dump_json())
    assert storage().load_source_map(path).model_dump_json() == mapped.model_dump_json()


@pytest.mark.parametrize("failure", [False, True])
def test_pipeline_persistence_uses_versioned_storage_and_keeps_hash_reader(tmp_path, failure):
    from src.pipeline import save_outputs, save_failure_report, read_output_source_content_hash
    doc = build_source_map(extraction())
    report = {"status": "FAILED", "summary": {}, "issues": []}
    if failure:
        save_failure_report(tmp_path, report, force=False, source_doc=doc)
    else:
        generated = {"article": {"source_file": "test.pdf", "topic": "Test", "subtopic": "Test",
                                 "reference": "Test", "number_of_chunks": 0}, "chunks": []}
        save_outputs(tmp_path, generated, report, source_doc=doc)
    assert "_source_map_storage" in json.loads((tmp_path / "source_map.json").read_text())
    assert read_output_source_content_hash(tmp_path) == doc.content_hash
    assert storage().load_source_map(tmp_path / "source_map.json").model_dump_json() == doc.model_dump_json()

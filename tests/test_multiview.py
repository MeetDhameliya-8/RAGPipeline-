"""Adaptive selection regressions; source fixtures are synthetic and offline."""
import pytest
from pathlib import Path
from src.multiview import build_multiview_selection, validate_multiview_selection
from src.source_map import build_source_map
from test_native_layout_recovery import pdf
from src.pdf_extractor import extract_pdf


def test_alternate_recovery_is_opt_in_and_keeps_original(tmp_path):
    e = extract_pdf(pdf(tmp_path))
    before = build_source_map(e)
    snapshot = before.evidence.model_dump_json()
    selection = build_multiview_selection(e, before)
    assert before.evidence.model_dump_json() == snapshot
    assert selection.selected
    assert all(r.view == "alternate" for r in selection.selected)
    validate_multiview_selection(selection, before)


def test_selection_metadata_cannot_promote_held_unit(tmp_path):
    e = extract_pdf(pdf(tmp_path, "encoding"));mapped = build_source_map(e)
    selection = build_multiview_selection(e, mapped)
    assert not selection.selected
    selection.decisions[0].selected = True
    with pytest.raises(ValueError):
        validate_multiview_selection(selection, mapped)


def mixed_pdf(tmp_path):
    import fitz
    path = pdf(tmp_path)
    d = fitz.open(path)
    p = d.new_page(width=600, height=800)
    p.insert_text((40, 55), "Methods", fontsize=12)
    p.insert_textbox(fitz.Rect(40, 100, 550, 350), "Children with inactive colitis received treatment beta for 4 weeks.", fontsize=11)
    target = tmp_path / "mixed.pdf"
    d.save(target);d.close()
    return target


def test_same_pdf_complementary_views_and_duplicate_native_ownership(tmp_path):
    e = extract_pdf(mixed_pdf(tmp_path));mapped = build_source_map(e)
    selection = build_multiview_selection(e, mapped)
    assert {r.view for r in selection.selected} == {"original", "alternate"}
    assert len(selection.selected) == 2
    assert any(r.outcome == "duplicate_representation" for r in selection.decisions)
    owned = [s for r in selection.selected for s in r.native_ownership]
    from src.draft_support import overlaps
    assert not any(overlaps(a, b) for i, a in enumerate(owned) for b in owned[i+1:])
    # Model a localized alternate-planner failure on the otherwise supported
    # original paragraph. It cannot discard independent original ownership or
    # the unrelated recovered paragraph in the same PDF.
    from src.multiview import _decide
    alternate = selection.alternate_plan.model_copy(deep=True)
    original_row = next(r for r in selection.selected if r.view == "original")
    damaged = next(r for r in selection.decisions if r.outcome == "duplicate_representation" and original_row.unit_id in r.related_unit_ids)
    unit = next(u for u in alternate.units if u.unit_id == damaged.unit_id)
    unit.generation_eligibility = "blocked";unit.block_reasons.append("unresolved_supporting_context")
    rows,_ = _decide(mapped.clinical_plan,mapped.evidence,alternate,selection.alternate_evidence,80)
    assert {r.view for r in rows if r.selected} == {"original", "alternate"}
    assert any(r.unit_id == original_row.unit_id and r.selected for r in rows)


def mock_azure(e, text):
    import json
    from types import SimpleNamespace as NS
    from unittest.mock import Mock
    from src.providers.azure import AzureChunkGenerator
    common = {"topic": "Chronic colitis", "subtopic": "Study protocol", "reference": e["reference_metadata"]["reference"]}
    response = {"article": dict(common, source_file=e["file_name"], number_of_chunks=1),
                "chunks": [dict(common, section="Study population and treatment protocol", original_text=text)]}
    client = Mock()
    client.chat.completions.create.return_value = NS(id="offline", choices=[NS(finish_reason="stop", message=NS(content=json.dumps(response), refusal=None))], usage=None)
    provider = AzureChunkGenerator("offline", "offline", "https://offline.openai.azure.com", "offline", client_factory=lambda **kw: client)
    return provider, client


def test_generation_uses_selected_view_actual_validator_and_unknown_origins(tmp_path):
    from src.adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
    from src.chunk_generator import generate_chunks
    from src.chunk_validator import validate_chunks
    from src.multiview import validation_sources, reconcile_multiview_coverage
    from test_native_layout_recovery import TEXT
    e = extract_pdf(pdf(tmp_path));mapped = build_source_map(e)
    # Reference comes only from the original source authority, never the selector.
    e.update(reference_source_text="doi:10.1234/fixture123", reference_metadata={"doi":"10.1234/fixture123", "reference":"doi:10.1234/fixture123"})
    plan = build_ingestion_plan(e, AdaptiveConfig(multiview=True), source_doc=mapped)
    provider, client = mock_azure(e, TEXT)
    audit = {};generated = generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=plan, diagnostics=audit)
    assert client.chat.completions.create.call_count == 1
    assert audit["chunk_ownership"][0]["view"] == "alternate"
    sources = validation_sources(plan.multiview_selection, mapped, audit)
    report = validate_chunks(generated, source_text=e["full_text"], chunk_sources=sources)
    assert report["status"] == "FAILED"
    assert any(i["rule"] == "WORD_COUNT" for i in report["issues"])
    assert not any(i["rule"] == "SOURCE_FIDELITY" for i in report["issues"])
    generated["chunks"][0]["original_text"] += " There were 999999999 participants."
    bad = validate_chunks(generated, chunk_sources=sources)
    assert any(i["rule"] == "SOURCE_FIDELITY" for i in bad["issues"])
    assert not reconcile_multiview_coverage(plan.multiview_selection, mapped, generated, bad, audit)["complete"]
    audit["chunk_ownership"][0]["unit_ids"] = ["unknown"]
    with pytest.raises(ValueError, match="origin"):
        validation_sources(plan.multiview_selection, mapped, audit)


@pytest.mark.parametrize("change", ["selected", "native_span", "proof", "pdf", "inventory", "warning", "context"])
def test_readback_recomputes_certificates_selection_and_preserved_inventory(tmp_path, change):
    from src.evidence_storage import save_source_map, load_source_map
    e = extract_pdf(pdf(tmp_path));mapped = build_source_map(e)
    selected = build_multiview_selection(e, mapped);mapped.multiview_selection = selected
    path = tmp_path/"new.json";save_source_map(path, mapped)
    assert load_source_map(path).model_dump_json() == mapped.model_dump_json()
    alt = selected.alternate_plan
    if change == "selected": selected.selected[0].selected = False
    if change == "native_span": selected.selected[0].native_ownership[0].char_start += 1
    if change == "proof": next(c for c in alt.concepts if c.draft_support).draft_support.block_id = "unknown"
    if change == "pdf": selected.pdf_sha256 = "b"*64
    if change == "inventory": selected.alternate_evidence.pages[0].rotation = 90
    if change == "warning": selected.alternate_evidence.warnings.pop(0)
    if change == "context": next(u for u in alt.units if u.generation_eligibility == "eligible_draft").context_ids.append(alt.concepts[0].concept_id)
    # Expanded files must be checked as strictly as compressed artifacts.
    path.write_text(mapped.model_dump_json(), encoding="utf-8")
    with pytest.raises(ValueError): load_source_map(path)


@pytest.mark.parametrize("case", ["table", "figure", "operator", "unlocalized", "rotation", "overlap", "conflicting_number", "unverified"])
def test_unsafe_source_conditions_are_not_released_by_multiview(tmp_path, case):
    from test_draft_eligibility import evidence, TEXT
    from src.evidence import get_evidence, warn
    options = {}
    if case == "table": options["text"] = TEXT[:-1] + " as shown in Table 1."
    if case == "operator": options["text"] = TEXT.replace("8", "(cid:42)")
    if case == "conflicting_number": options["alternate"] = "Methods\n\n" + TEXT.replace("8", "9")
    if case == "overlap": options["visual"] = (50, 100, 200, 300)
    e = evidence(**options);d = get_evidence(e)
    if case == "figure":
        d.warnings[0].bbox = None
    if case == "unlocalized": warn(d, "critical_gap", "Unlocalized missing clinical evidence", critical=True)
    if case == "rotation": d.pages[0].rotation = 90
    if case == "unverified": d.identity_quality = "legacy_text_only";d.pdf_sha256 = None
    e["evidence"] = d.model_dump(mode="json");mapped = build_source_map(e)
    if case == "unverified":
        with pytest.raises(ValueError, match="identity"): build_multiview_selection(e, mapped)
    else:
        selected = build_multiview_selection(e, mapped)
        assert not selected.selected


def test_legacy_expanded_and_compressed_maps_keep_empty_selection(tmp_path):
    from src.evidence_storage import load_source_map, save_source_map
    from test_draft_eligibility import evidence
    mapped = build_source_map(evidence());payload = mapped.model_dump();payload.pop("multiview_selection")
    import json
    old = tmp_path/"expanded.json";old.write_text(json.dumps(payload), encoding="utf-8")
    assert load_source_map(old).multiview_selection is None
    packed = tmp_path/"packed.json";save_source_map(packed, load_source_map(old))
    assert load_source_map(packed).multiview_selection is None


def test_image_only_explicit_unavailable_has_no_selected_unit(tmp_path):
    from src.native_layout import inspect_pdf_routes
    e = inspect_pdf_routes(pdf(tmp_path, "scan"));mapped = build_source_map(e)
    selected = build_multiview_selection(e, mapped)
    assert not selected.selected
    assert any(r.kind == "ocr_required" and r.status == "unavailable" for r in selected.alternate_evidence.processing_routes)


@pytest.mark.parametrize("name,digest,view,start,end", [
    ("PIIS0016508514012086.pdf", "d0d79c17a1c5e4bdab9b8ae9b665e1c7a96fac95fe594a559d6632566e3cdd80", "original", 5482, 5686),
    ("PIIS0016508515010860.pdf", "7b1e531d05683af67766e2d8b45ad0dbbdee84aa1be54c9b44c13aeeb7de934c", "alternate", 19419, 19799),
])
def test_both_protected_real_anchors_reach_mocked_azure_with_unchanged_proofs(name, digest, view, start, end):
    from pathlib import Path
    from hashlib import sha256
    from src.adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
    from src.chunk_generator import generate_chunks
    from src.chunk_validator import validate_chunks
    from src.multiview import validation_sources
    from src.native_layout import recover_native_layout
    from src.concept_planner import build_clinical_plan
    from src.source_map import build_coverage_report, initial_processing_state
    path = Path("input_pdfs")/name
    if not path.exists(): pytest.skip("Protected real source unavailable; release check required.")
    assert sha256(path.read_bytes()).hexdigest() == digest
    e = extract_pdf(path);mapped = build_source_map(e)
    ingestion = build_ingestion_plan(e, AdaptiveConfig(multiview=True), source_doc=mapped)
    selection = ingestion.multiview_selection
    assert len(selection.selected) == 1 and selection.selected[0].view == view
    evidence = mapped.evidence if view == "original" else selection.alternate_evidence
    plan = mapped.clinical_plan if view == "original" else selection.alternate_plan
    unit = next(u for u in plan.units if u.unit_id == selection.selected[0].unit_id)
    assert [(s.char_start, s.char_end) for s in unit.ownership] == [(start, end)]
    independent_e = e if view == "original" else recover_native_layout(e)
    independent = build_source_map(independent_e);independent.clinical_plan = build_clinical_plan(independent_e, independent)
    by_id = {c.concept_id:c for c in independent.clinical_plan.concepts}
    for c in plan.concepts:
        if c.concept_id in unit.concept_ids:
            assert c.draft_support == by_id[c.concept_id].draft_support
    text = " ".join(evidence.buffers[plan.buffer_id].text[start:end].split())
    provider, client = mock_azure(e, text);audit = {}
    generated = generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=ingestion, diagnostics=audit)
    assert client.chat.completions.create.call_count == 1
    sources = validation_sources(selection, mapped, audit)
    report = validate_chunks(generated, chunk_sources=sources)
    assert report["status"] == "FAILED" and {i["rule"] for i in report["issues"]} == {"WORD_COUNT"}
    generated["chunks"][0]["original_text"] += " There were 999999999 participants."
    assert any(i["rule"] == "SOURCE_FIDELITY" for i in validate_chunks(generated, chunk_sources=sources)["issues"])
    assert not build_coverage_report(mapped, initial_processing_state(mapped))["source_coverage_complete"]
    assert all(not w.resolved for w in evidence.warnings)


def test_overlapping_interpretations_with_different_context_are_held():
    from test_draft_eligibility import evidence
    from src.concept_planner import build_clinical_plan
    from src.multiview import _decide
    e = evidence();mapped = build_source_map(e)
    original = build_clinical_plan(e, mapped)
    alternate = original.model_copy(deep=True)
    # Different source ownership under the same native block cannot be safely
    # counted twice. A second representation carries a different unit identity.
    alternate.plan_id = "alternate-plan"
    for u in alternate.units: u.unit_id += "-alternate"
    c = next(c for c in alternate.concepts if c.generation_eligibility == "eligible_draft")
    c.ownership[0].char_start += len("Adults ")
    # This clipped clinical scope fails certification and stays unresolved.
    rows, _ = _decide(original, mapped.evidence, alternate, mapped.evidence, 80)
    assert not any(r.selected and r.view == "alternate" for r in rows)
    assert any(r.selected and r.view == "original" for r in rows)


def test_cross_page_and_multi_block_claims_remain_explicitly_held(tmp_path):
    import fitz
    d = fitz.open();p = d.new_page(width=600, height=800)
    p.insert_text((40,55), "Methods")
    p.insert_text((40,100), "Adults with colitis received treatment alpha")
    p = d.new_page(width=600, height=800)
    p.insert_text((40,100), "for 8 weeks under the study protocol.")
    path = tmp_path/"continued.pdf";d.save(path);d.close()
    e = extract_pdf(path);mapped = build_source_map(e)
    selected = build_multiview_selection(e, mapped)
    assert not selected.selected
    assert all(r.reasons for r in selected.decisions)


def test_same_text_in_distinct_native_contexts_is_not_cross_view_duplicate(tmp_path):
    import fitz
    d = fitz.open()
    for heading in ("Protocol A", "Protocol B"):
        p = d.new_page(width=600,height=800)
        p.insert_text((40,55), heading)
        p.insert_text((40,100), "Adults with colitis received treatment alpha for 8 weeks.")
    path = tmp_path/"repeated.pdf";d.save(path);d.close()
    e = extract_pdf(path);mapped = build_source_map(e)
    selected = build_multiview_selection(e,mapped)
    # Existing repeated-margin section mapping cannot independently associate
    # this identical short text. Preserve both occurrences as held source work;
    # text equality alone cannot license a duplicate decision across contexts.
    assert not selected.selected
    assert not any(r.outcome == "duplicate_representation" for r in selected.decisions)
    assert len({b.span.buffer_id for page in mapped.evidence.pages for b in page.blocks if b.span}) >= 4


@pytest.mark.parametrize("tamper", ["batch", "schedule"])
def test_generation_rejects_tampered_schedule_before_provider(tmp_path, tamper):
    from src.adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
    from src.chunk_generator import generate_chunks, ChunkGenerationError
    from unittest.mock import Mock
    e = extract_pdf(pdf(tmp_path));mapped = build_source_map(e)
    plan = build_ingestion_plan(e, AdaptiveConfig(multiview=True), source_doc=mapped)
    if tamper == "batch": plan.batches[0].ownership[0].char_start += 1
    else: plan.batches.append(plan.batches[0].model_copy(deep=True))
    provider = Mock()
    with pytest.raises((ValueError, ChunkGenerationError)):
        generate_chunks(e, source_doc=mapped, generation_plan=plan, provider=provider)
    provider.generate.assert_not_called()


def test_different_context_for_same_native_ownership_is_conflicting(tmp_path):
    from src.concept_planner import build_clinical_plan
    from src.multiview import _decide
    # Both complete propositions have independent native certificates. Adding
    # a different supporting proposition changes the clinical interpretation.
    import fitz
    d = fitz.open();p = d.new_page(width=600,height=800)
    p.insert_text((40,55), "Methods")
    p.insert_text((40,100), "Adults with colitis received treatment alpha for 8 weeks.")
    p.insert_text((40,200), "Children with colitis received treatment beta for 4 weeks.")
    path = tmp_path/"contexts.pdf";d.save(path);d.close()
    e = extract_pdf(path);mapped = build_source_map(e)
    original = build_clinical_plan(e,mapped,max_unit_chars=200)
    alternate = original.model_copy(deep=True)
    units = [u for u in alternate.units if u.generation_eligibility == "eligible_draft"]
    assert len(units) == 2
    for u in alternate.units: u.unit_id += "-alternate"
    units[0].context_ids = units[1].concept_ids
    rows,_ = _decide(original,mapped.evidence,alternate,mapped.evidence,80)
    conflicting = [r for r in rows if r.outcome == "conflicting_evidence"]
    assert len(conflicting) == 2
    assert all(not r.selected and r.related_unit_ids for r in conflicting)


def test_pipeline_opt_in_persists_selection_and_bound_validation(tmp_path,monkeypatch):
    import json,logging
    from src import pipeline
    from src.chunk_generator import generate_chunks
    from src.evidence_storage import load_source_map
    from test_native_layout_recovery import TEXT
    path=pdf(tmp_path);e=extract_pdf(path)
    e.update(reference_source_text="doi:10.1234/fixture123",reference_metadata={"doi":"10.1234/fixture123","reference":"doi:10.1234/fixture123"})
    provider,client=mock_azure(e,TEXT)
    monkeypatch.setattr(pipeline,"OUTPUT_DIR",tmp_path/"review")
    monkeypatch.setattr(pipeline,"extract_pdf",lambda _:e)
    monkeypatch.setattr(pipeline,"generate_chunks",lambda *args,**kwargs:generate_chunks(*args,provider=provider,**kwargs))
    result=pipeline.process_pdf(path,multiview=True,logger=logging.getLogger("test-multiview"))
    assert result["status"] == "failed" and result["failure_stage"] == "chunk_validation"
    output=Path(result["output_dir"])
    saved=load_source_map(output/"source_map.json")
    assert saved.multiview_selection.selected[0].view == "alternate"
    coverage=json.loads((output/"coverage_report.json").read_text())
    assert not coverage["source_coverage_complete"]
    report=json.loads((output/"validation_report.json").read_text())
    assert report["source_evidence"][0]["selected_source"]["view"] == "alternate"
    assert report["diagnostics"]["planned_request_characters"] > 0
    assert client.chat.completions.create.call_count == 1 and path.exists()


def test_opt_in_and_default_output_fingerprints_are_separate(tmp_path):
    from src.adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
    from src.pipeline import planned_output_dir,build_parser
    e=extract_pdf(pdf(tmp_path));a=build_source_map(e);b=build_source_map(e)
    build_ingestion_plan(e,source_doc=a)
    build_ingestion_plan(e,AdaptiveConfig(multiview=True),source_doc=b)
    assert planned_output_dir(tmp_path/"review",a) != planned_output_dir(tmp_path/"review",b)
    assert not build_parser().parse_args([]).multiview
    assert build_parser().parse_args(["--multiview"]).multiview


@pytest.mark.parametrize("value", ["9 weeks", "8 days", ">8 weeks", "-8 weeks"])
def test_literal_quantity_conflicts_have_explicit_hold_outcome(value):
    from test_draft_eligibility import evidence,TEXT
    e=evidence(alternate="Methods\n\n"+TEXT.replace("8 weeks",value));mapped=build_source_map(e)
    selected=build_multiview_selection(e,mapped)
    assert not selected.selected
    assert any(r.outcome=="conflicting_evidence" and "conflicting_native_quantity_readings_in_literal_context" in r.reasons for r in selected.decisions)

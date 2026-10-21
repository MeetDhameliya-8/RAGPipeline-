"""P1-C1 local independence proofs. Providers are always mocked."""
import json
from unittest.mock import Mock

import pytest

from src.evidence import build_evidence_document, get_evidence, span, warn
from src.source_map import build_source_map, build_coverage_report, initial_processing_state
from src.concept_planner import build_clinical_plan, validate_clinical_plan


TEXT = "Adults with active colitis received treatment alpha for 8 weeks."


def evidence(text=TEXT, *, visual=(350, 100, 550, 350), alternate=None):
    body = "Methods\n\n" + text
    page = {"page_number": 1, "text": body, "raw_text": body, "extractor": "pypdf"}
    layout = {"width": 600, "height": 800, "blocks": [
        {"text": text, "bbox": [40, 100, 290, 350], "native_order": 1, "direction": [1, 0]}]}
    if visual:
        layout["blocks"].append({"role": "visual_region", "signal_kind": "drawing", "bbox": visual, "native_order": 2})
    candidates = [dict(page), dict(page, extractor="pymupdf", raw_text=body if alternate is None else alternate, layout=layout)]
    e = {"file_name": "held-out.pdf", "page_count": 1, "pages": [page], "full_text": body, "raw_full_text": body,
         "reference_source_text": "doi:10.1234/fixture123", "reference_metadata": {"doi": "10.1234/fixture123", "reference": "doi:10.1234/fixture123"}}
    doc = build_evidence_document(e, pdf_sha256="a" * 64, candidates=candidates)
    e["evidence"] = doc.model_dump(mode="json")
    return e


def planned(e):
    mapped = build_source_map(e)
    mapped.clinical_plan = build_clinical_plan(e, mapped)
    return mapped, mapped.clinical_plan


def eligible(p):
    return [u for u in p.units if u.generation_eligibility == "eligible_draft"]


def test_disjoint_same_page_visual_is_retained_but_does_not_block_certified_source():
    e = evidence()
    mapped, p = planned(e)
    assert eligible(p)
    c = next(c for c in p.concepts if c.role == "clinical_proposition")
    assert c.draft_support and c.independent_warning_ids
    assert not c.warning_decisions and not c.block_reasons
    assert set(c.independent_warning_ids) <= set(p.document_warning_ids)
    assert all(not w.resolved for w in mapped.evidence.warnings)
    assert not build_coverage_report(mapped, initial_processing_state(mapped))["source_coverage_complete"]


def test_local_column_order_warning_requires_cross_extractor_block_certificate():
    e = evidence()
    d = get_evidence(e)
    warn(d, "reading_order_unverified", "Columns need review.", 1, True)
    e["evidence"] = d.model_dump(mode="json")
    mapped, p = planned(e)
    assert eligible(p)
    c = next(c for c in p.concepts if c.draft_support)
    proof = c.draft_support
    value = " ".join(TEXT.split())
    for s in proof.candidate_spans + [proof.native_block_span]:
        assert " ".join(mapped.evidence.buffers[s.buffer_id].text[s.char_start:s.char_end].split()) == value
    assert proof.page_number == 1 and len(proof.candidate_spans) == 2


@pytest.mark.parametrize("case", ["overlap", "disagree", "geometry", "direction", "unlocated", "local_sign", "missing_table", "missing_group", "anaphora", "truncated", "qualifier", "interleaved"])
def test_unsupported_evidence_remains_blocked(case):
    options = {}
    if case == "overlap": options["visual"] = (50, 150, 200, 300)
    if case == "disagree": options["alternate"] = TEXT.replace("8", "9")
    if case == "missing_table": options["text"] = TEXT[:-1] + " as shown in Table 2."
    if case == "missing_group": options["text"] = "Adults in group B had a response after 8 weeks."
    if case == "anaphora": options["text"] = "These patients received treatment alpha for 8 weeks."
    if case == "truncated": options["text"] = TEXT[:-1]
    if case == "interleaved": options["text"] = "Adults received treatment alpha unrelated column patients dose."
    e = evidence(**options)
    d = get_evidence(e)
    if case == "geometry": d.pages[0].blocks[0].bbox = None
    if case == "direction": d.pages[0].blocks[0].direction = (0, 1)
    if case == "unlocated": warn(d, "clinical_gap", "Missing material without a location.", critical=True)
    if case == "local_sign":
        a = e["full_text"].index("8")
        warn(d, "encoding_sign_unit_unresolved", "Damaged dose.", 1, True, span(d.canonical_buffer_id, a, a+1))
    if case == "qualifier":
        from src.evidence import add_buffer
        d.pages[0].blocks[0].span = span(add_buffer(d, "raw", "qualifier", TEXT + " However, only selected patients were included.", 1, "pymupdf_dict"), 0, len(TEXT + " However, only selected patients were included."))
    if case == "interleaved":
        from src.evidence import add_buffer
        b = d.pages[0].blocks[0]
        b.span = span(add_buffer(d, "raw", "column", "Adults received treatment alpha.\nUnrelated column patients dose.", 1, "pymupdf_dict"), 0, len("Adults received treatment alpha.\nUnrelated column patients dose."))
    e["evidence"] = d.model_dump(mode="json")
    _, p = planned(e)
    assert not eligible(p)


def test_certificate_and_independent_warning_tampering_rejects(tmp_path):
    from src.evidence_storage import save_source_map, load_source_map
    mapped, p = planned(evidence())
    assert eligible(p)
    path = tmp_path / "map.json"
    save_source_map(path, mapped)
    assert load_source_map(path).clinical_plan == p
    c = next(c for c in p.concepts if c.draft_support)
    c.draft_support.candidate_spans[0].char_end -= 1
    with pytest.raises(ValueError, match="support|certificate"):
        validate_clinical_plan(p, mapped.evidence)


def test_located_canonical_transformation_without_page_is_not_an_unlocated_gap():
    e = evidence(TEXT + "\n\nUnresolved appendix material.")
    d = get_evidence(e);a = e["full_text"].index("Unresolved")
    warn(d, "text_transformation_unreviewed", "A located transformation elsewhere.", None, True,
         span(d.canonical_buffer_id, a, len(e["full_text"])))
    e["evidence"] = d.model_dump(mode="json")
    _, p = planned(e)
    assert eligible(p)
    assert p.document_warning_ids and not p.document_complete


def test_stripping_certificate_and_warning_decisions_cannot_bypass_validation():
    mapped, p = planned(evidence())
    assert eligible(p)
    c = next(c for c in p.concepts if c.draft_support)
    c.draft_support = None;c.independent_warning_ids = [];c.structural_context_ids = []
    with pytest.raises(ValueError, match="warning|support"):
        validate_clinical_plan(p, mapped.evidence)


def test_mocked_generation_and_actual_validator_keep_clinical_and_coverage_gates():
    from src.adaptive_ingestion import build_ingestion_plan
    from src.chunk_generator import generate_chunks
    from src.chunk_validator import validate_chunks
    from src.concept_planner import reconcile_concept_coverage
    e = evidence()
    mapped = build_source_map(e)
    ingestion = build_ingestion_plan(e, source_doc=mapped)
    assert ingestion.batches
    response = {"article": {"source_file": e["file_name"], "topic": "Chronic colitis", "subtopic": "Induction protocol", "reference": "doi:10.1234/fixture123", "number_of_chunks": 1},
                "chunks": [{"topic": "Chronic colitis", "subtopic": "Induction protocol", "section": "Population and study treatment", "reference": "doi:10.1234/fixture123", "original_text": TEXT}]}
    provider = Mock(); provider.generate.return_value = json.dumps(response)
    diagnostics = {}
    generated = generate_chunks(e, provider=provider, source_doc=mapped, generation_plan=ingestion, diagnostics=diagnostics)
    assert provider.generate.call_count == 1 and diagnostics["chunk_ownership"]
    report = validate_chunks(generated, source_text=e["full_text"], source_pages=e["pages"])
    # Eligibility is not clinical validity: the existing short-chunk rule survives.
    assert report["status"] == "FAILED"
    assert any(i["rule"] == "WORD_COUNT" for i in report["issues"])
    bad = json.loads(json.dumps(generated));bad["chunks"][0]["original_text"] = TEXT.replace("8", "999")
    bad_report = validate_chunks(bad, source_text=e["full_text"])
    assert any(i["rule"] == "SOURCE_FIDELITY" for i in bad_report["issues"])
    coverage = reconcile_concept_coverage(ingestion.clinical_plan, mapped.evidence, generated, report, diagnostics)
    assert not coverage["complete"] and coverage["clinically_approved_concepts"] == 0


def test_coherent_source_reaches_mocked_azure_transport_and_real_validation():
    from types import SimpleNamespace as NS
    from src.providers.azure import AzureChunkGenerator
    from src.chunk_generator import generate_chunks
    from src.chunk_validator import validate_chunks
    from src.concept_planner import reconcile_concept_coverage
    text = (
        "Adults with active colitis received treatment alpha for 8 weeks under the study protocol; "
        "patients attended scheduled visits at the study center for collection of symptom diaries and "
        "assessment of medication use; the study team recorded abdominal symptoms and stool frequency "
        "using the same questionnaire at each visit; patients recorded daily symptoms before "
        "attending the clinic and brought the completed diary to the study team; study personnel "
        "documented concomitant medication use and reviewed the diary entries with each participant; "
        "the protocol specified that clinical assessments would be recorded on the study forms and "
        "retained for review; participants continued attending the scheduled study visits during the "
        "treatment period and provided information about tolerability and medication adherence; the "
        "clinical team documented the visit assessments together with the participant diary records "
        "and retained the source records for subsequent review by the study investigators; the protocol "
        "required study staff to record the date and location of each assessment on the source forms."
    )
    e = evidence(text)
    response = {"article": {"source_file": e["file_name"], "topic": "Chronic colitis", "subtopic": "Induction protocol", "reference": "doi:10.1234/fixture123", "number_of_chunks": 1},
                "chunks": [{"topic": "Chronic colitis", "subtopic": "Induction protocol", "section": "Population and study treatment", "reference": "doi:10.1234/fixture123", "original_text": text}]}
    client = Mock()
    client.chat.completions.create.return_value = NS(id="offline", choices=[NS(finish_reason="stop", message=NS(content=json.dumps(response), refusal=None))], usage=None)
    provider = AzureChunkGenerator("offline-deployment", "offline-key", "https://offline.openai.azure.com", "offline-version", client_factory=lambda **kwargs: client)
    mapped, p = planned(e)
    audit = {}
    generated = generate_chunks(e, provider=provider, source_doc=mapped, diagnostics=audit)
    report = validate_chunks(generated, source_text=e["full_text"], source_pages=e["pages"])
    assert client.chat.completions.create.call_count == 1
    assert report["status"] == "PASS", report["issues"]
    coverage = reconcile_concept_coverage(mapped.clinical_plan, mapped.evidence, generated, report, audit)
    assert any(c["status"] == "source_supported" for c in coverage["concepts"])
    assert not coverage["complete"] and coverage["clinically_approved_concepts"] == 0


@pytest.mark.parametrize("family", ["digital", "two_column", "table", "unrelated_figure", "scanned"])
def test_separate_local_pdf_document_families(tmp_path, family):
    import fitz
    from src.pdf_extractor import extract_pdf
    doc = fitz.open();page = doc.new_page(width=600, height=800)
    if family == "scanned":
        # An actual image-only PDF: the clinical-looking pixels have no text layer.
        from PIL import Image, ImageDraw
        import io
        pixels = Image.new("RGB", (500, 200), "white")
        ImageDraw.Draw(pixels).text((20, 20), TEXT, fill="black")
        stream = io.BytesIO();pixels.save(stream, format="PNG")
        page.insert_image(fitz.Rect(40, 100, 560, 350), stream=stream.getvalue())
    else:
        page.insert_text((40, 55), "Methods", fontsize=12)
        body = TEXT if family != "table" else "Adults received treatment alpha as shown in Table 1."
        page.insert_textbox(fitz.Rect(40, 100, 285, 350), body, fontsize=11)
        if family == "two_column":
            page.insert_textbox(fitz.Rect(330, 100, 560, 350), "Supplementary material\nUnresolved source data.", fontsize=11)
        if family in {"unrelated_figure", "table"}:
            page.draw_rect(fitz.Rect(350, 100, 550, 350))
            if family == "table":page.insert_text((350, 390), "Table 1. Treatment results", fontsize=10)
    path = tmp_path / (family + ".pdf");doc.save(path);doc.close()
    if family == "scanned":
        from src.pdf_extractor import PdfExtractionError
        with pytest.raises(PdfExtractionError, match="requires OCR"):
            extract_pdf(path)
        return
    e = extract_pdf(path);mapped, p = planned(e)
    if family in {"digital", "unrelated_figure"}:
        assert eligible(p)
    else:
        assert not eligible(p)
        if family == "table":
            assert any("table_relationship_unresolved" in c.block_reasons for c in p.concepts)
        else:
            # Both natives are retained; interleaved canonical text cannot be
            # licensed by geometry alone. Alternative text routing is P1-C2.
            assert any(w.code == "reading_order_unverified" for w in mapped.evidence.warnings)


def test_overprinted_native_text_does_not_certify():
    from src.evidence import add_buffer, EvidenceBlock
    e = evidence();d = get_evidence(e)
    b = d.pages[0].blocks[0]
    d.pages[0].blocks.append(EvidenceBlock(block_id="overprint", span=span(add_buffer(d, "raw", "overprint", "Unrelated text", 1), 0, 14),
        bbox=b.bbox, native_order=3, direction=(1, 0)))
    e["evidence"] = d.model_dump(mode="json")
    assert not eligible(planned(e)[1])


def test_nonwhitespace_insertions_do_not_certify_even_with_native_occurrence():
    from src.draft_support import NativeSupportIndex
    from src.concept_planner import EvidenceIndex
    mapped, p = planned(evidence())
    c = next(c for c in p.concepts if c.role == "clinical_proposition")
    trace = EvidenceIndex(mapped.evidence).trace(c.ownership[0])
    trace.synthetic_spans.append(span(p.buffer_id, c.ownership[0].char_start, c.ownership[0].char_start+6))
    assert NativeSupportIndex(mapped.evidence).prove(c.ownership[0], trace) is None

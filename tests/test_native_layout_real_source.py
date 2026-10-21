"""Reviewed real-PDF extraction anchors; no source/medical output stored here."""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock

import pytest

from src.pdf_extractor import extract_pdf
from src.native_layout import recover_native_layout
from src.source_map import build_source_map, initial_processing_state, build_coverage_report
from src.concept_planner import build_clinical_plan


@pytest.fixture(scope="module")
def actual():
    meta = json.loads((Path(__file__).parent/"fixtures/native_layout_anchors.json").read_text())
    path = Path("input_pdfs")/meta["source_file"]
    if not path.exists():pytest.skip("Real native-layout anchor PDF unavailable; release verification required.")
    assert sha256(path.read_bytes()).hexdigest() == meta["pdf_sha256"]
    before = extract_pdf(path);old = build_source_map(before);old.clinical_plan = build_clinical_plan(before, old)
    after = recover_native_layout(before);new = build_source_map(after);new.clinical_plan = build_clinical_plan(after, new)
    return meta, before, old, after, new


def test_reviewed_real_two_column_paragraph_recovers_exact_qualifiers(actual):
    meta, before, old, after, new = actual
    assert not any(u.generation_eligibility=="eligible_draft" for u in old.clinical_plan.units)
    assert sum(u.generation_eligibility=="eligible_draft" for u in new.clinical_plan.units)==1
    c = next(c for c in new.clinical_plan.concepts if c.generation_eligibility=="eligible_draft")
    assert c.ownership[0].model_dump() == meta["positive"]["canonical_span"]
    assert c.draft_support.version == "native-block-lines-v2"
    assert len(c.draft_support.line_support) == meta["positive"]["native_lines"]
    assert c.draft_support.native_block_span.model_dump() == meta["positive"]["native_span"]
    s = c.ownership[0];value = after["full_text"][s.char_start:s.char_end]
    assert sha256(value.encode()).hexdigest() == meta["positive"]["text_sha256"]
    assert " ".join(value.split()) not in " ".join(before["full_text"].split())
    assert c.qualifications  # The analysis restriction is part of owned evidence.
    state = initial_processing_state(new)
    assert not build_coverage_report(new,state)["source_coverage_complete"]


def test_reviewed_nearby_damaged_operator_cannot_be_repaired_by_reordering(actual):
    meta, _, _, after, new = actual
    target = meta["negative"]["canonical_span"]
    c = next(c for c in new.clinical_plan.concepts if c.ownership[0].model_dump()==target)
    s = c.ownership[0];value = after["full_text"][s.char_start:s.char_end]
    assert sha256(value.encode()).hexdigest() == meta["negative"]["text_sha256"]
    assert chr(meta["negative"]["expected_control_codepoint"]) in value
    assert c.generation_eligibility=="blocked" and c.warning_decisions
    assert all(not w.resolved for w in new.evidence.warnings)


def test_real_recovered_unit_mocked_azure_actual_validator_and_unsupported_claim(actual):
    from src.chunk_generator import generate_chunks
    from src.chunk_validator import validate_chunks
    from src.providers.azure import AzureChunkGenerator
    meta, _, _, after, new = actual
    c = next(c for c in new.clinical_plan.concepts if c.generation_eligibility=="eligible_draft")
    s = c.ownership[0];text = after["full_text"][s.char_start:s.char_end].strip()
    ref = after["reference_metadata"]["reference"]
    common = {"topic":"Irritable bowel syndrome","subtopic":"Dietary intervention analysis","reference":ref}
    response = {"article":dict(common, source_file=after["file_name"], number_of_chunks=1),
                "chunks":[dict(common, section="Analysis populations and missing outcomes", original_text=text)]}
    client = Mock();client.chat.completions.create.return_value = NS(id="offline",choices=[NS(finish_reason="stop",message=NS(content=json.dumps(response),refusal=None))],usage=None)
    provider = AzureChunkGenerator("offline","offline","https://offline.openai.azure.com","offline",client_factory=lambda **kw:client)
    audit = {};generated = generate_chunks(after,provider=provider,source_doc=new,diagnostics=audit)
    assert client.chat.completions.create.call_count==1 and audit["chunk_ownership"]
    assert generated["chunks"][0]["reference"]==ref
    report = validate_chunks(generated, source_text=after["full_text"],source_pages=after["pages"])
    # Source recovery never pads a short paragraph or changes the old length rule.
    assert report["status"]=="FAILED" and any(i["rule"]=="WORD_COUNT" for i in report["issues"])
    assert not any(i["rule"]=="SOURCE_FIDELITY" for i in report["issues"])
    generated["chunks"][0]["original_text"] += " There were 999999999 participants."
    unsupported = validate_chunks(generated,source_text=after["full_text"])
    assert unsupported["status"]=="FAILED" and any(i["rule"]=="SOURCE_FIDELITY" for i in unsupported["issues"])

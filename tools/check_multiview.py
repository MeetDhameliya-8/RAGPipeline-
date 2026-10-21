"""Offline corpus audit for opt-in native multi-view selection; never live inference."""
from __future__ import annotations
import argparse
from collections import Counter
import gc
from hashlib import sha256
import json
from pathlib import Path
import time
from types import SimpleNamespace as NS
from unittest.mock import Mock

from src.adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
from src.chunk_generator import generate_chunks
from src.chunk_validator import validate_chunks
from src.evidence_storage import save_source_map, load_source_map
from src.multiview import validation_sources
from src.pdf_extractor import extract_pdf
from src.providers.azure import AzureChunkGenerator
from src.source_map import build_source_map, build_coverage_report, initial_processing_state


def proof(e, mapped, plan):
    selection = plan.multiview_selection
    row = selection.selected[0]
    clinical, evidence = ((mapped.clinical_plan, mapped.evidence) if row.view == "original" else
                         (selection.alternate_plan, selection.alternate_evidence))
    unit = next(u for u in clinical.units if u.unit_id == row.unit_id)
    text = " ".join(" ".join(evidence.buffers[s.buffer_id].text[s.char_start:s.char_end].split()) for s in unit.ownership)
    ref = e["reference_metadata"]["reference"]
    common = {"topic":"Clinical study protocol", "subtopic":"Study analysis population", "reference":ref}
    response = {"article":dict(common, source_file=e["file_name"], number_of_chunks=1),
                "chunks":[dict(common, section="Study population and analysis protocol", original_text=text)]}
    client = Mock();client.chat.completions.create.return_value = NS(id="offline", choices=[NS(finish_reason="stop", message=NS(content=json.dumps(response), refusal=None))], usage=None)
    provider = AzureChunkGenerator("offline", "offline", "https://offline.openai.azure.com", "offline", client_factory=lambda **kw:client)
    diagnostics = {};generated = generate_chunks(e, source_doc=mapped, generation_plan=plan, provider=provider, diagnostics=diagnostics)
    sources = validation_sources(selection, mapped, diagnostics)
    report = validate_chunks(generated, chunk_sources=sources)
    generated["chunks"][0]["original_text"] += " There were 999999999 participants."
    bad = validate_chunks(generated, chunk_sources=sources)
    assert any(i["rule"]=="SOURCE_FIDELITY" for i in bad["issues"])
    assert not any(i["rule"]=="SOURCE_FIDELITY" for i in report["issues"])
    assert generated["chunks"][0]["reference"] == ref
    return {"view":row.view, "mock_sdk_calls":client.chat.completions.create.call_count,
        "validator_status":report["status"], "validator_rules":sorted({i["rule"] for i in report["issues"]}),
        "unsupported_number_rejected":True, "reference_matches":True, "words":len(text.split()),
        "text_sha256":sha256(text.encode()).hexdigest(), "native_ownership":[s.model_dump() for s in row.native_ownership]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    args = parser.parse_args();args.directory.mkdir(exist_ok=False)
    protected = {str(p.resolve()):sha256(p.read_bytes()).hexdigest() for root in ("input_pdfs", "processed", "output")
                 for p in Path(root).rglob("*") if p.is_file()}
    (args.directory/"protected.json").write_text(json.dumps(protected))
    rows = [];start = time.perf_counter()
    paths = sorted(Path("input_pdfs").glob("*.pdf"))
    prior = json.loads((args.prior/"metrics.json").read_text())["rows"]
    for i, path in enumerate(paths):
        t = time.perf_counter();e = extract_pdf(path);mapped = build_source_map(e)
        original_json = mapped.evidence.model_dump_json()
        plan = build_ingestion_plan(e, AdaptiveConfig(multiview=True), source_doc=mapped)
        selection = plan.multiview_selection
        assert original_json == mapped.evidence.model_dump_json()
        before = sum(u.generation_eligibility=="eligible_draft" for u in mapped.clinical_plan.units)
        alternate = sum(u.generation_eligibility=="eligible_draft" for u in selection.alternate_plan.units)
        baseline = next(r for r in prior if r["pdf_sha256"] == selection.pdf_sha256)
        assert (before, alternate) == (baseline["before_eligible"], baseline["after_eligible"])
        assert not build_coverage_report(mapped, initial_processing_state(mapped))["source_coverage_complete"]
        artifact = args.directory/f"source-{i:02}.json";save_source_map(artifact, mapped)
        loaded = load_source_map(artifact)
        assert loaded.model_dump_json() == mapped.model_dump_json()
        row = {"file":path.name, "pdf_sha256":selection.pdf_sha256, "pages":e["page_count"],
            "original_eligible":before, "alternate_eligible":alternate, "unique_selected":len(selection.selected),
            "outcomes":dict(Counter(d.outcome for d in selection.decisions)),
            "original_warnings":len(mapped.evidence.warnings), "alternate_warnings":len(selection.alternate_evidence.warnings),
            "source_integrity":True, "roundtrip_equal":True, "complete":False,
            "seconds":time.perf_counter()-t, "artifact_bytes":artifact.stat().st_size}
        if selection.selected: row["mock_proof"] = proof(e, mapped, plan)
        rows.append(row)
        (args.directory/"metrics.json").write_text(json.dumps({"rows":rows},indent=2))
        print(f"{i+1}/{len(paths)}: original={before}, alternate={alternate}, unique={len(selection.selected)}",flush=True)
        del e, mapped, plan, selection, loaded;gc.collect()
    changed = [p for p,h in protected.items() if not Path(p).exists() or sha256(Path(p).read_bytes()).hexdigest()!=h]
    result = {"rows":rows, "seconds":time.perf_counter()-start, "protected_files":len(protected), "protected_changed":changed,
        "live_calls":0, "clinical_approvals":0}
    (args.directory/"metrics.json").write_text(json.dumps(result,indent=2))
    return int(bool(changed))

if __name__ == "__main__": raise SystemExit(main())

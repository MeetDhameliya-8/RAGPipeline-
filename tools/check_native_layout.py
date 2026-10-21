"""Offline native-layout comparison. Writes new review maps, never generation."""
from __future__ import annotations

import argparse
from collections import Counter
import gc
from hashlib import sha256
import json
from pathlib import Path
import time

from src.pdf_extractor import extract_pdf
from src.native_layout import recover_native_layout
from src.source_map import build_source_map, initial_processing_state, build_coverage_report
from src.concept_planner import build_clinical_plan
from src.evidence_storage import save_source_map, load_source_map


def check(path, original, directory, index):
    start = time.perf_counter();e = extract_pdf(path)
    before = build_source_map(e);before.clinical_plan = build_clinical_plan(e, before)
    before_seconds = time.perf_counter()-start
    start = time.perf_counter()
    after_e = recover_native_layout(dict(e, evidence=before.evidence.model_dump(mode="json")))
    after = build_source_map(after_e);after.clinical_plan = build_clinical_plan(after_e, after)
    recovery_seconds = time.perf_counter()-start
    old, new = before.evidence, after.evidence
    assert all(new.buffers[k] == v for k,v in old.buffers.items())
    assert all(new.mappings[k] == v for k,v in old.mappings.items())
    assert all(w in new.warnings for w in old.warnings)
    p, q = before.clinical_plan, after.clinical_plan
    artifact = directory/f"source-{index:02}.json"
    save_source_map(artifact, after)
    loaded = load_source_map(artifact)
    assert loaded.model_dump_json() == after.model_dump_json()
    state = initial_processing_state(after)
    state.sections = [s.model_copy(update={"status":"processed"}) for s in state.sections]
    coverage = build_coverage_report(after, state)
    assert not coverage["source_coverage_complete"]
    baseline_differences = [key for key, value in {
        "pdf_sha256":sha256(path.read_bytes()).hexdigest(),
        "full_text_sha256":sha256(e["full_text"].encode()).hexdigest(),
        "raw_text_sha256":sha256(e["raw_full_text"].encode()).hexdigest(),
        "reference":e["reference_metadata"].get("reference", ""), "sections":len(before.sections)}.items() if original[key] != value]
    anchors = []
    for c in q.concepts:
        if c.generation_eligibility != "eligible_draft":continue
        s = c.ownership[0];value = new.buffers[s.buffer_id].text[s.char_start:s.char_end]
        anchors.append({"page_numbers":c.page_numbers,"canonical_span":s.model_dump(),"text_sha256":sha256(value.encode()).hexdigest(),
            "proof_version":c.draft_support.version if c.draft_support else None,
            "block_id":c.draft_support.block_id if c.draft_support else None,
            "was_contiguous_in_original":" ".join(value.split()) in " ".join(e["full_text"].split())})
    return {"pdf_sha256":sha256(path.read_bytes()).hexdigest(),"pages":e["page_count"],"baseline_differences":baseline_differences,
        "before_units":len(p.units),"after_units":len(q.units),
        "before_eligible":sum(u.generation_eligibility=="eligible_draft" for u in p.units),
        "after_eligible":sum(u.generation_eligibility=="eligible_draft" for u in q.units),"request_budget_blockers":q.blockers,
        "before_warnings":len(old.warnings),"after_warnings":len(new.warnings),"original_buffers_retained":len(old.buffers),
        "reconstructed_pages":sum(r.status=="reconstructed" for r in new.processing_routes),
        "route_kinds":dict(Counter(r.kind for r in new.processing_routes)),
        "held_native_reasons":dict(Counter(code for r in new.processing_routes if r.kind=="native_layout" and r.status=="held" for code in r.reason_codes)),
        "before_reasons":dict(Counter(r for c in p.concepts for r in c.block_reasons)),
        "after_reasons":dict(Counter(r for c in q.concepts for r in c.block_reasons)),
        "source_accounting":q.source_accounting,"eligible_anchors":anchors,"roundtrip_equal":True,"source_coverage_complete":False,
        "before_seconds":before_seconds,"recovery_seconds":recovery_seconds,"artifact_bytes":artifact.stat().st_size}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline",type=Path,required=True)
    parser.add_argument("--protected",type=Path,required=True)
    parser.add_argument("--directory",type=Path,required=True)
    args = parser.parse_args();args.directory.mkdir(exist_ok=False)
    rows = [];start = time.perf_counter()
    for i, original in enumerate(json.loads(args.baseline.read_text())):
        row = check(Path("input_pdfs")/original["file"], original, args.directory, i)
        rows.append(row)
        print(f"{i+1}: eligible {row['before_eligible']} -> {row['after_eligible']}, native pages={row['reconstructed_pages']}",flush=True)
        (args.directory/"metrics.json").write_text(json.dumps({"rows":rows},indent=2));gc.collect()
    protected = json.loads(args.protected.read_text())
    changed = [p for p,h in protected.items() if not Path(p).exists() or sha256(Path(p).read_bytes()).hexdigest()!=h]
    (args.directory/"metrics.json").write_text(json.dumps({"rows":rows,"seconds":time.perf_counter()-start,
        "protected_files":len(protected),"protected_changed":changed,"live_requests":0,"clinical_approvals":0},indent=2))
    return int(bool(changed) or any(r["baseline_differences"] for r in rows))


if __name__ == "__main__":raise SystemExit(main())

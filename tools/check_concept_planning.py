"""Offline P1-B1 source/concept/resource audit. No providers or historical writes.

Requires existing baseline/hash manifests and a NEW audit directory. Artifacts
contain source evidence and must remain outside source control.
"""
from __future__ import annotations

import argparse
from collections import Counter
import gc
from hashlib import sha256
import json
from pathlib import Path
import time
import tracemalloc

from src.adaptive_ingestion import build_ingestion_plan
from src.concept_planner import build_clinical_plan, validate_clinical_plan, EvidenceIndex
from src.evidence import span, trace_to_raw
from src.evidence_storage import save_source_map, load_source_map
from src.pdf_extractor import extract_pdf
from src.source_map import build_source_map, build_coverage_report, initial_processing_state


def check(pdf: Path, baseline: dict, artifact: Path, profile: bool) -> dict:
    started=time.perf_counter()
    extraction=extract_pdf(pdf)
    source=build_source_map(extraction)
    extract_map_seconds=time.perf_counter()-started
    before=build_ingestion_plan(extraction)
    state=initial_processing_state(source)
    state.sections=[r.model_copy(update={"status":"processed"}) for r in state.sections]
    before_complete=build_coverage_report(source,state)["source_coverage_complete"]
    started=time.perf_counter()
    ingestion=build_ingestion_plan(extraction,source_doc=source)
    plan_seconds=time.perf_counter()-started
    plan=ingestion.clinical_plan
    peak=None
    if profile:
        gc.collect();tracemalloc.start()
        repeated=build_clinical_plan(extraction,source,max_unit_chars=plan.max_unit_chars)
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        if repeated.model_dump_json()!=plan.model_dump_json():raise ValueError("Plan is nondeterministic")
        del repeated
    validate_clinical_plan(plan,source.evidence)
    index=EvidenceIndex(source.evidence)
    full=span(plan.buffer_id,0,len(extraction["full_text"]))
    indexed=index.trace(full)
    original=trace_to_raw(source.evidence,full)
    if indexed!=original:raise ValueError("Indexed evidence trace differs from original")
    current={"pdf_sha256":sha256(pdf.read_bytes()).hexdigest(),"pages":extraction["page_count"],
        "full_text_sha256":sha256(extraction["full_text"].encode()).hexdigest(),
        "raw_text_sha256":sha256(extraction["raw_full_text"].encode()).hexdigest(),
        "sections":len(source.sections),"batch_ranges":[[b.char_start,b.char_end] for b in before.batches],
        "blockers":before.blockers,"reference":extraction["reference_metadata"].get("reference","")}
    differences=[key for key,value in current.items() if baseline[key]!=value]
    started=time.perf_counter();save_source_map(artifact,source);write_seconds=time.perf_counter()-started
    started=time.perf_counter();loaded=load_source_map(artifact);read_seconds=time.perf_counter()-started
    equal=loaded.model_dump_json()==source.model_dump_json()
    after_complete=build_coverage_report(source,state)["source_coverage_complete"]
    reference_records=[]
    def references(metadata):
        for key in ("source_spans","citation_candidates"):reference_records.extend(metadata.get(key,[]))
        for candidate in metadata.get("conflicting_candidates",[]):references(candidate)
    references(extraction["reference_metadata"])
    reference_matches=sum(item["buffer_id"] in source.evidence.buffers and
        source.evidence.buffers[item["buffer_id"]].text[item["char_start"]:item["char_end"]]==item["text"] for item in reference_records)
    return {"pages":extraction["page_count"],"sections":len(source.sections),"legacy_differences":differences,
        "section_matches":sum(source.evidence.buffers[s.buffer_id].text[s.char_start:s.char_end]==s.text for s in source.sections),
        "reference_spans":len(reference_records),"reference_matches":reference_matches,
        "concepts":len(plan.concepts),"deduplicated_occurrences":plan.deduplicated_occurrences,
        "concept_roles":dict(Counter(c.role for c in plan.concepts)),
        "blocked_reasons":dict(Counter(r for c in plan.concepts for r in c.block_reasons)),
        "units":len(plan.units),"eligible_units":len(ingestion.batches),
        "unit_decisions":dict(Counter(u.dependency_decision for u in plan.units)),
        "document_warning_ids":len(plan.document_warning_ids),"source_accounting":plan.source_accounting,
        "before_complete_all_processed":before_complete,"after_complete_all_processed":after_complete,
        "unmapped":len(indexed.unmapped_spans),"indexed_trace_equal":indexed==original,
        "manifest_bytes":len(plan.model_dump_json().encode()),"artifact_bytes":artifact.stat().st_size,
        "planning_peak_traced_bytes":peak,"extract_map_seconds":extract_map_seconds,"planning_seconds":plan_seconds,
        "write_seconds":write_seconds,"read_seconds":read_seconds,"roundtrip_equal":equal,
        "live_requests":0,"clinically_approved":0}


def main() -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline",type=Path,required=True)
    parser.add_argument("--protected",type=Path,required=True)
    parser.add_argument("--directory",type=Path,required=True)
    parser.add_argument("--profile",action="store_true")
    args=parser.parse_args()
    args.directory.mkdir(exist_ok=False)
    baseline=json.loads(args.baseline.read_text())
    rows=[];started=time.perf_counter()
    for index,old in enumerate(baseline):
        row=check(Path("input_pdfs")/old["file"],old,args.directory/f"source-{index:02}.json",args.profile)
        rows.append(row)
        print(f"{index+1}/{len(baseline)} concepts={row['concepts']} eligible={row['eligible_units']}/{row['units']} differences={row['legacy_differences']}",flush=True)
        (args.directory/'metrics.json').write_text(json.dumps({"rows":rows},indent=2))
        gc.collect()
    protected=json.loads(args.protected.read_text())
    changed=[p for p,h in protected.items() if not Path(p).exists() or sha256(Path(p).read_bytes()).hexdigest()!=h]
    result={"rows":rows,"elapsed_seconds":time.perf_counter()-started,"protected_files":len(protected),"protected_changed":changed,
        "live_requests":0,"clinically_approved":0}
    (args.directory/'metrics.json').write_text(json.dumps(result,indent=2))
    return int(bool(changed) or any(r['legacy_differences'] or r['unmapped'] or not r['roundtrip_equal']
        or r['after_complete_all_processed'] or r['section_matches']!=r['sections']
        or r['reference_matches']!=r['reference_spans'] for r in rows))


if __name__=="__main__":raise SystemExit(main())

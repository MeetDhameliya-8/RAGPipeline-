"""Read-only P1-A corpus verification; no provider, clinical output or PDF writes.

python -m tools.check_evidence_provenance --baseline <before.json> --protected <hashes.json> --metrics <new.json>
Baseline records describe P0 text hashes, page/section counts, batch ranges,
blockers and reference identity. Runtime metrics belong outside source control.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import time

from src.adaptive_ingestion import build_ingestion_plan
from src.evidence import get_evidence, span, trace_to_raw, validate_evidence, warning_blocks_coverage
from src.pdf_extractor import extract_pdf
from src.source_map import build_coverage_report, build_source_map, initial_processing_state


def check(pdf: Path, baseline: dict, artifact_path: Path | None = None) -> dict:
    extraction = extract_pdf(pdf)
    evidence = get_evidence(extraction)
    source_doc = build_source_map(extraction)
    plan = build_ingestion_plan(extraction)
    validate_evidence(source_doc.evidence)
    current = {
        "pdf_sha256": sha256(pdf.read_bytes()).hexdigest(),
        "full_text_sha256": sha256(extraction["full_text"].encode()).hexdigest(),
        "raw_text_sha256": sha256(extraction["raw_full_text"].encode()).hexdigest(),
        "pages": extraction["page_count"], "sections": len(source_doc.sections),
        "batch_ranges": [[b.char_start, b.char_end] for b in plan.batches],
        "blockers": plan.blockers, "reference": extraction["reference_metadata"].get("reference", ""),
    }
    trace = trace_to_raw(evidence, span(evidence.canonical_buffer_id, 0, len(extraction["full_text"])))
    state = initial_processing_state(source_doc)
    state.sections = [r.model_copy(update={"status": "processed"}) for r in state.sections]
    coverage = build_coverage_report(source_doc, state)
    reference_spans = []
    def collect_reference_spans(metadata: dict) -> None:
        for key in ("source_spans", "citation_candidates"):
            reference_spans.extend(metadata.get(key, []))
        for candidate in metadata.get("conflicting_candidates", []):
            collect_reference_spans(candidate)
    collect_reference_spans(extraction["reference_metadata"])
    reference_matches = sum(item["buffer_id"] in evidence.buffers and
                            evidence.buffers[item["buffer_id"]].text[item["char_start"]:item["char_end"]] == item["text"]
                            for item in reference_spans)
    storage_checks = {}
    if artifact_path is not None:
        from src.evidence_storage import save_source_map, load_source_map
        save_source_map(artifact_path, source_doc)
        loaded = load_source_map(artifact_path)
        storage_checks = {"persisted_bytes": artifact_path.stat().st_size,
                          "storage_roundtrip_equal": loaded.model_dump_json() == source_doc.model_dump_json()}
    return {
        "pages": len(evidence.pages), "sections": len(source_doc.sections),
        "offset_matches": sum(source_doc.evidence.buffers[s.buffer_id].text[s.char_start:s.char_end] == s.text for s in source_doc.sections),
        "differences": [k for k, value in current.items() if value != baseline[k]],
        "raw_trace_fragments": len(trace.fragments), "unmapped": len(trace.unmapped_spans),
        "trace_precision": dict(Counter(f.precision for f in trace.fragments)),
        "reference_spans": len(reference_spans), "reference_matches": reference_matches,
        "source_map_json_bytes": len(source_doc.model_dump_json().encode("utf-8")),
        "blocks": sum(len(p.blocks) for p in evidence.pages),
        "candidates": sum(len(p.candidates) for p in evidence.pages),
        "critical_warnings": dict(Counter(w.code for w in source_doc.evidence.warnings if w.critical and not w.resolved)),
        "blocking_warning_classifications": dict(Counter(w.classification for w in source_doc.evidence.warnings if warning_blocks_coverage(w))),
        "coverage_complete_with_all_processed": coverage["source_coverage_complete"],
        **storage_checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--protected", type=Path, required=True)
    parser.add_argument("--metrics", type=Path, required=True)
    parser.add_argument("--pdf-dir", type=Path, default=Path("input_pdfs"))
    parser.add_argument("--artifact-dir", type=Path, help="Optional NEW audit directory for compressed persistence/read-back checks.")
    args = parser.parse_args()
    if args.metrics.exists():
        parser.error("Choose a new metrics path; existing files are preserved.")
    if args.artifact_dir is not None:
        args.artifact_dir.mkdir(exist_ok=False)
    baseline = json.loads(args.baseline.read_text())
    started = time.monotonic()
    rows = []
    for index, old in enumerate(baseline, 1):
        row = check(args.pdf_dir / old["file"], old,
                    args.artifact_dir / f"source-{index:02}.json" if args.artifact_dir else None)
        rows.append(row)
        print(f"{index}/{len(baseline)} offsets={row['offset_matches']}/{row['sections']} differences={row['differences']}", flush=True)
    protected = json.loads(args.protected.read_text())
    changed = [p for p, digest in protected.items() if not Path(p).exists() or sha256(Path(p).read_bytes()).hexdigest() != digest]
    result = {"rows": rows, "elapsed_seconds": round(time.monotonic()-started, 2),
              "protected_files": len(protected), "protected_changed": changed,
              "baseline_offset_matches": sum(r["old_offset_matches"] for r in baseline),
              "baseline_sections": sum(r["sections"] for r in baseline), "live_requests": 0}
    with args.metrics.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}), flush=True)
    return int(bool(changed) or any(r["differences"] or r["unmapped"] or r["offset_matches"] != r["sections"]
                                  or r["reference_spans"] != r["reference_matches"]
                                  or not r.get("storage_roundtrip_equal", True)
                                  or (r["blocking_warning_classifications"] and r["coverage_complete_with_all_processed"]) for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())

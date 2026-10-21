"""Read-only PDF/candidate replay. Never calls providers or saves clinical drafts.

Run from the project root: python -m tools.replay_reliability --metrics <new-json-path>
Metrics separate historical-candidate replay from extraction routes; neither is
a claim about current live generation or human approval.
"""
from __future__ import annotations

import argparse
from collections import Counter
from hashlib import sha256
import json
from pathlib import Path
import time
from typing import Any

from src.adaptive_ingestion import build_ingestion_plan
from src.chunk_generator import normalize_generation_result, validate_generation_content, ChunkGenerationError
from src.chunk_validator import validate_chunks
from src.pdf_extractor import extract_pdf


def replay(pdf: Path, packages: Path) -> dict[str, Any]:
    started = time.monotonic()
    row: dict[str, Any] = {"file": pdf.name, "pdf_sha256": sha256(pdf.read_bytes()).hexdigest(),
                           "live_requests": 0, "clinically_approved": 0}
    extraction = extract_pdf(pdf)
    plan = build_ingestion_plan(extraction)
    row.update(pages=extraction["page_count"], extraction_blockers=plan.blockers,
               planned_requests=len(plan.batches), identity_status=extraction["reference_metadata"].get("identity_status"),
               source_reference=extraction["reference_metadata"].get("reference"))
    candidate_path = packages / pdf.stem.lower() / "chunks.json"
    if not candidate_path.exists():
        row["candidate_stage"] = "no_saved_candidates"
        return row
    data = json.loads(candidate_path.read_text(encoding="utf-8"))
    try:
        generated = normalize_generation_result(data)
    except ChunkGenerationError as exc:
        row.update(candidate_stage="raw_schema", failure_type=type(exc).__name__)
        return row
    identity_extraction = dict(extraction, reference_source_text="\n\n".join(p["raw_text"] for p in extraction["pages"][:2]))
    try:
        validate_generation_content(generated, identity_extraction)
        row["candidate_reference_stage"] = "source_supported_not_approved"
    except ChunkGenerationError:
        row["candidate_reference_stage"] = "reference_identity_blocked"
    # Evaluate clinical checks separately even if a saved citation is rejected,
    # so improved extraction is not confused with acceptance of stale references.
    report = validate_chunks(generated, source_text=extraction["full_text"], source_pages=extraction["pages"])
    row.update(candidate_stage="clinical_validation", status=report["status"], summary=report["summary"],
               issue_counts=dict(Counter(f"{i['severity']}:{i['rule']}" for i in report["issues"])),
               seconds=round(time.monotonic() - started, 3))
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf-dir", type=Path, default=Path("input_pdfs"))
    parser.add_argument("--packages", type=Path, default=Path("output"))
    parser.add_argument("--metrics", type=Path, required=True, help="New metrics-only JSON file; existing files are never replaced.")
    args = parser.parse_args()
    if args.metrics.exists():
        parser.error("Metrics destination already exists; choose a new file to preserve prior runs.")
    rows = []
    for pdf in sorted(args.pdf_dir.glob("*.pdf")):
        row = replay(pdf, args.packages)
        rows.append(row)
        print(pdf.name, row.get("status", row.get("candidate_stage")), flush=True)
    if not rows:
        parser.error("No PDF fixtures found.")
    result = {"kind": "offline_historical_candidate_replay", "live_requests": 0, "rows": rows,
              "documents": len(rows), "pages": sum(r["pages"] for r in rows),
              "status_counts": dict(Counter(r.get("status", "not_evaluated") for r in rows))}
    args.metrics.parent.mkdir(parents=True, exist_ok=True)
    with args.metrics.open("x", encoding="utf-8") as handle:
        json.dump(result, handle, indent=2, ensure_ascii=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

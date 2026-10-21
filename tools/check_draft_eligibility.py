"""Offline P1-C1 comparison with preserved P1-B1 maps; never calls providers."""
from __future__ import annotations

import argparse
import gc
from hashlib import sha256
import json
from pathlib import Path
import time

from src.evidence_storage import load_source_map
from tools.check_concept_planning import check


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--protected", type=Path, required=True)
    parser.add_argument("--prior", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--profile", action="store_true")
    args = parser.parse_args()
    args.directory.mkdir(exist_ok=False)
    baseline = json.loads(args.baseline.read_text())
    rows = [];started = time.perf_counter()
    for i, original in enumerate(baseline):
        artifact = args.directory / f"source-{i:02}.json"
        row = check(Path("input_pdfs") / original["file"], original, artifact, args.profile)
        prior = load_source_map(args.prior / artifact.name)
        after = load_source_map(artifact)
        before_plan, plan = prior.clinical_plan, after.clinical_plan
        row.update(before_units=len(before_plan.units),
            before_eligible_units=sum(u.generation_eligibility == "eligible_draft" for u in before_plan.units),
            warning_ids_unchanged=before_plan.document_warning_ids == plan.document_warning_ids,
            evidence_unchanged=prior.evidence.model_dump_json() == after.evidence.model_dump_json(),
            certified_concepts=sum(bool(c.draft_support) for c in plan.concepts),
            independent_warning_decisions=sum(len(c.independent_warning_ids) for c in plan.concepts),
            source_accounting_unchanged=before_plan.source_accounting == plan.source_accounting,
            eligible_source_ranges=[[s.char_start, s.char_end] for u in plan.units if u.generation_eligibility == "eligible_draft" for s in u.ownership])
        rows.append(row)
        print(f"{i+1}/{len(baseline)} eligible {row['before_eligible_units']} -> {row['eligible_units']}; warnings retained={row['warning_ids_unchanged']}", flush=True)
        (args.directory / "metrics.json").write_text(json.dumps({"rows": rows}, indent=2))
        del prior, after;gc.collect()
    protected = json.loads(args.protected.read_text())
    changed = [p for p,h in protected.items() if not Path(p).exists() or sha256(Path(p).read_bytes()).hexdigest() != h]
    result = {"rows": rows, "elapsed_seconds": time.perf_counter()-started,
              "protected_files": len(protected), "protected_changed": changed,
              "live_requests": 0, "clinical_approvals": 0}
    (args.directory / "metrics.json").write_text(json.dumps(result, indent=2))
    return int(bool(changed) or any(r["legacy_differences"] or not r["warning_ids_unchanged"] or
        not r["evidence_unchanged"] or not r["source_accounting_unchanged"] or not r["roundtrip_equal"] or
        r["after_complete_all_processed"] or r["section_matches"] != r["sections"] or
        r["reference_matches"] != r["reference_spans"] for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())

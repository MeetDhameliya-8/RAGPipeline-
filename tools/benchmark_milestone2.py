"""Offline-only scheduler/CPU measurements on disposable synthetic fixtures.

Run with python -B tools/benchmark_milestone2.py. No runtime PDFs, DBs, Azure
clients or pipeline entrypoints are used. Results contain metrics/digests only.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack
from hashlib import sha256
import json
from pathlib import Path
from statistics import median
import sys
import tempfile
from threading import Lock
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT/"tests"))

from milestone2_support import fixed_groups, packet_index, packet_response, source
from src import diagnostics as telemetry, package_generation as generation
from src import evidence, evidence_storage, preparation_cache, pdf_extractor, source_packages, source_map
from src.chunk_generator import generate_chunks
from src.source_packages import package_validation_sources


def digest(model) -> str:
    data = model.model_dump(mode="json") if hasattr(model, "model_dump") else model
    return sha256(json.dumps(data, sort_keys=True, allow_nan=False).encode()).hexdigest()


class DelayedProvider:
    def __init__(self, extraction, delays):
        self.extraction, self.delays = extraction, delays
        self.active = self.maximum = self.calls = 0
        self.lock = Lock()

    def generate(self, text, source_file, prompt):
        index = packet_index(text)
        with self.lock:
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.calls += 1
        try:
            time.sleep(self.delays[index])
            return packet_response(self.extraction, index)
        finally:
            with self.lock:
                self.active -= 1


def scheduler_measurements(directory: Path, repeats: int) -> dict:
    extraction, mapped, plan = source(directory, 8)
    rolling = generation._run_source_schedule
    delays = [0.12, 0.01] * 4
    samples = {"fixed_groups": [], "rolling": []}
    expected = None
    for trial in range(repeats):
        # Alternate measurement order to reduce first-run/order bias.
        modes = ("fixed_groups", "rolling") if trial % 2 == 0 else ("rolling", "fixed_groups")
        for mode in modes:
            provider = DelayedProvider(extraction, delays)
            audit = {}
            with patch.object(generation, "_run_source_schedule", fixed_groups if mode == "fixed_groups" else rolling):
                started = time.perf_counter()
                generated = generate_chunks(extraction, source_doc=mapped, generation_plan=plan,
                    provider=provider, diagnostics=audit, package_concurrency=2)
                elapsed = time.perf_counter() - started
            signature = digest({"output":generated,"ownership":audit["chunk_ownership"],
                "rejections":audit["validation_rejections"],"failures":audit["unit_failures"]})
            expected = signature if expected is None else expected
            assert signature == expected
            assert provider.calls == 8 and provider.maximum == 2
            assert audit["units_resumed"] == 0
            samples[mode].append({"seconds":elapsed,"calls":provider.calls,"max_active_requests":provider.maximum,
                "output_signature":signature,"source_packages":len(plan.batches),
                "generation_stage":audit["observability"]["stages"]["generation"]})
    return {"workload":"prepared synthetic source; no candidate caches; injected delays only",
        "delays_seconds":delays,"samples":samples,
        "median_seconds":{mode:median(s["seconds"] for s in rows) for mode, rows in samples.items()},
        "identical_output_and_ownership":True}


def cpu_measurements(directory: Path, pages: int, repeats: int) -> dict:
    directory.mkdir()
    extraction, mapped, plan = source(directory, pages)
    identity_before = digest({"evidence":mapped.evidence.model_dump(mode="json"),
        "plan":plan.source_package_plan.model_dump(mode="json")})
    proof_seconds, key_seconds = [], []
    for _ in range(repeats):
        with telemetry.bind(telemetry.Observation()):
            with telemetry.stage("proof_validation"):
                source_packages.validate_source_package_plan(plan.source_package_plan, mapped.evidence)
            proof_seconds.append(telemetry.current().snapshot()["stages"]["proof_validation"]["inclusive_seconds"])
        started = time.perf_counter()
        digest({"evidence":mapped.evidence.model_dump(mode="json"), "plan":plan.source_package_plan.model_dump(mode="json"),
            "validation_implementation":generation.validation_fingerprint()})
        key_seconds.append(time.perf_counter()-started)

    original_proof = source_packages.validate_source_package_plan
    original_align = evidence.align_buffers
    original_read = Path.read_bytes
    page_calls = {"pypdf_layout":0,"pypdf_plain":0,"pymupdf_sorted":0,"pymupdf_geometry":0,"pymupdf_other":0}
    source_reads = 0
    observation = telemetry.Observation()
    import fitz
    from pypdf._page import PageObject
    original_pypdf, original_fitz = PageObject.extract_text, fitz.Page.get_text

    def pypdf_text(page, *args, **kwargs):
        page_calls["pypdf_layout" if kwargs.get("extraction_mode")=="layout" else "pypdf_plain"] += 1
        return original_pypdf(page, *args, **kwargs)

    def fitz_text(page, *args, **kwargs):
        key = "pymupdf_geometry" if args and args[0]=="dict" else "pymupdf_sorted" if kwargs.get("sort") else "pymupdf_other"
        page_calls[key] += 1
        return original_fitz(page, *args, **kwargs)

    def read(path):
        nonlocal source_reads
        if path.name == "source.json":
            source_reads += 1
        return original_read(path)

    with ExitStack() as stack:
        stack.enter_context(telemetry.bind(observation))
        proof = telemetry.timed("proof_validation")(original_proof)
        align = telemetry.timed("buffer_alignment")(original_align)
        for module in (source_packages, generation):
            stack.enter_context(patch.object(module, "validate_source_package_plan", proof))
        for module in (evidence, source_map):
            stack.enter_context(patch.object(module, "align_buffers", align))
        stack.enter_context(patch.object(PageObject, "extract_text", pypdf_text))
        stack.enter_context(patch.object(fitz.Page, "get_text", fitz_text))
        second = pdf_extractor.extract_pdf(directory/"synthetic.pdf")
        assert digest(second) == digest(pdf_extractor.extract_pdf(directory/"synthetic.pdf"))
        generation.validate_source_package_plan(plan.source_package_plan, mapped.evidence)
        package_validation_sources(plan.source_package_plan, mapped.evidence, {"chunk_ownership":[]})
        source_path = directory/"source-map.json"
        with telemetry.stage("evidence_storage_save"):
            evidence_storage.save_source_map(source_path, mapped)
        with telemetry.stage("evidence_storage_verify"):
            loaded = evidence_storage.load_source_map(source_path)
        assert digest(loaded) == digest(mapped)
        cache = directory/"prepared"
        with telemetry.stage("preparation_save"):
            preparation_cache.save_preparation(directory/"synthetic.pdf", extraction, mapped, cache)
        stack.enter_context(patch.object(Path, "read_bytes", read))
        prepared = preparation_cache.load_preparation(directory/"synthetic.pdf", cache)
        assert prepared is not None and digest(prepared[1]) == digest(mapped)
        checkpoint_reads = source_reads
        # Corrupt only the disposable benchmark cache and require a cache miss.
        next(cache.glob("*/extraction.json")).write_text("{}")
        assert preparation_cache.load_preparation(directory/"synthetic.pdf", cache) is None
    assert identity_before == digest({"evidence":mapped.evidence.model_dump(mode="json"),
        "plan":plan.source_package_plan.model_dump(mode="json")})
    return {"pages":pages,"proof_seconds":proof_seconds,"full_identity_key_seconds":key_seconds,
        "median_proof_seconds":median(proof_seconds),"median_full_identity_key_seconds":median(key_seconds),
        "stages":observation.snapshot()["stages"], "page_extraction_calls_for_two_runs":page_calls,
        "source_json_reads_per_preparation_load":checkpoint_reads,
        "structural_roundtrip_equal":True,"proof_source_and_plan_unchanged":True,
        "corrupted_preparation_rejected":True,"source_map_bytes":source_path.stat().st_size}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, default=ROOT/"tmp/milestone2/measurements.json")
    args = parser.parse_args()
    if not 1 <= args.repeats <= 20:
        parser.error("Use 1–20 repetitions.")
    work = ROOT/"tmp/milestone2"
    if ROOT.resolve() not in work.resolve().parents:
        parser.error("Benchmark workspace must remain inside this project.")
    if work.resolve() not in args.output.resolve().parents:
        parser.error("Benchmark metrics must stay under tmp/milestone2/.")
    work.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="offline-benchmark-",dir=work) as temporary:
        directory = Path(temporary)
        scheduling = directory/"scheduling"
        scheduling.mkdir()
        result = {"offline_only":True,"paid_requests":0,"scheduler":scheduler_measurements(scheduling,args.repeats),
            "cpu":[cpu_measurements(directory/f"cpu-{pages}",pages,args.repeats) for pages in (4,8,16)]}
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps({"offline_only":True,"scheduler_median_seconds":result["scheduler"]["median_seconds"],
        "cpu_median_proof_seconds":{row["pages"]:row["median_proof_seconds"] for row in result["cpu"]}}))


if __name__ == "__main__":
    main()

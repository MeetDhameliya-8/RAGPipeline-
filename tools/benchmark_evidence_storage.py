"""Offline sidecar resource benchmark. Writes only to a NEW explicit audit directory.

Python allocation peaks are incremental per operation. Windows working-set peaks
are sampled (5 ms), include native allocations, and are not exact allocation peaks.
No provider, production store or existing review artifact is modified.
"""
from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import gc
import gzip
from hashlib import sha256
import json
from pathlib import Path
import threading
import time
import tracemalloc

from src.pdf_extractor import extract_pdf
from src.source_map import SourceDocument, build_source_map, build_coverage_report, initial_processing_state
from src.evidence import validate_evidence, trace_to_raw, span


def working_set() -> int:
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [(n, ctypes.c_size_t) for n in
            ("peak", "working", "pool_peak", "pool", "nonpool_peak", "nonpool", "pagefile", "pagefile_peak")]
    value = Counters()
    value.cb = ctypes.sizeof(value)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    fn = ctypes.WinDLL("psapi", use_last_error=True).GetProcessMemoryInfo
    fn.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not fn(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise OSError("Cannot sample Windows working set")
    return value.working


def measured(operation):
    gc.collect()
    samples = [working_set()]
    stop = threading.Event()
    def sample():
        while not stop.wait(.005):
            samples.append(working_set())
    worker = threading.Thread(target=sample, daemon=True)
    worker.start()
    tracemalloc.start()
    started = time.perf_counter()
    try:
        result = operation()
        elapsed = time.perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        samples.append(working_set())
        stop.set()
        worker.join()
    return result, {"seconds": elapsed, "python_peak_bytes": peak, "working_set_start": samples[0],
                    "sampled_working_set_peak": max(samples), "sampled_working_set_increase": max(samples)-samples[0]}


def components(doc: SourceDocument) -> dict:
    # Include wrappers consistently; component sizes are attribution estimates,
    # not a summable alternate file format. No evidence text enters metrics.
    include = lambda value: len(doc.model_dump_json(include=value).encode())
    buffers = doc.evidence.buffers.values()
    texts = [b.text for b in buffers]
    return {"compact_bytes": len(doc.model_dump_json().encode()),
            "buffers_bytes": include({"evidence": {"buffers"}}),
            "mappings_bytes": include({"evidence": {"mappings"}}),
            "section_spans_bytes": include({"sections": {"__all__": {"source_spans"}}}),
            "section_traces_bytes": include({"sections": {"__all__": {"source_trace"}}}),
            "text_bytes": sum(len(s.encode()) for s in texts),
            "unique_text_bytes": sum(len(s.encode()) for s in set(texts)),
            "mappings": sum(len(v) for v in doc.evidence.mappings.values()),
            "section_fragments": sum(len(s.source_trace.fragments) for s in doc.sections if s.source_trace)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--mode", choices=("legacy", "compressed"), required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--legacy-directory", type=Path)
    args = parser.parse_args()
    args.directory.mkdir(exist_ok=False)
    rows = []
    baseline = json.loads(args.baseline.read_text())
    for index, old in enumerate(baseline):
        started = time.perf_counter()
        extraction = extract_pdf(Path("input_pdfs") / old["file"])
        doc = build_source_map(extraction)
        build_seconds = time.perf_counter()-started
        artifact = args.directory / f"source-{index:02}.json"
        if args.mode == "legacy":
            def write():
                with artifact.open("x", encoding="utf-8", newline="") as stream:
                    stream.write(doc.model_dump_json(indent=2))
            read = lambda: SourceDocument.model_validate_json(artifact.read_bytes())
        else:
            from src.evidence_storage import save_source_map, load_source_map
            write = lambda: save_source_map(artifact, doc)
            read = lambda: load_source_map(artifact)
        _, serialization = measured(write)
        loaded, loading = measured(read)
        validate_evidence(loaded.evidence)
        equal = loaded.model_dump_json() == doc.model_dump_json()
        trace = trace_to_raw(loaded.evidence, span(loaded.buffer_id, 0, len(loaded.evidence.buffers[loaded.buffer_id].text)))
        state = initial_processing_state(loaded)
        state.sections = [r.model_copy(update={"status": "processed"}) for r in state.sections]
        coverage = build_coverage_report(loaded, state)
        row = {"index": index, "pages": extraction["page_count"], "build_seconds": build_seconds,
               "artifact_bytes": artifact.stat().st_size, "serialization": serialization, "load": loading,
               "roundtrip_equal": equal, "unmapped": len(trace.unmapped_spans),
               "section_slices": sum(loaded.evidence.buffers[s.buffer_id].text[s.char_start:s.char_end] == s.text for s in loaded.sections),
               "sections": len(loaded.sections), "coverage_complete_all_processed": coverage["source_coverage_complete"],
               "pdf_unchanged": sha256((Path("input_pdfs") / old["file"]).read_bytes()).hexdigest() == old["pdf_sha256"],
               "generation_text_unchanged": sha256(extraction["full_text"].encode()).hexdigest() == old["full_text_sha256"],
               "components": components(doc)}
        if args.mode == "legacy":
            compact = doc.model_dump_json().encode()
            row["gzip_compact_bytes"] = len(gzip.compress(compact, compresslevel=6, mtime=0))
        if args.legacy_directory:
            legacy = load_source_map(args.legacy_directory/f"source-{index:02}.json")
            validate_evidence(legacy.evidence)
            row["legacy_load_valid"] = True
            del legacy
        rows.append(row)
        (args.directory / "metrics.json").write_text(json.dumps({"mode": args.mode, "rows": rows}, indent=2))
        print(f"{index+1}/{len(baseline)} bytes={row['artifact_bytes']} roundtrip={equal}", flush=True)
        del doc, loaded, trace, extraction
        gc.collect()
    return int(any(not r["roundtrip_equal"] or r["unmapped"] or not r["pdf_unchanged"] or not r["generation_text_unchanged"] or r["sections"] != r["section_slices"] for r in rows))


if __name__ == "__main__":
    raise SystemExit(main())

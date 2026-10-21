"""Synthetic fixtures and the old group-barrier baseline, for offline comparisons."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import fitz

from src.adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
from src.bounded_scheduler import Outcome, ScheduleRun
from src.native_layout import recover_native_layout
from src.pdf_extractor import extract_pdf
from src.source_map import build_source_map
from test_source_packages import TEXT, response

WORDS = ("alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta",
         "iota", "kappa", "lambda", "mu", "nu", "xi", "omicron", "pi")


def source(tmp_path: Path, pages: int = 6):
    path = tmp_path / "synthetic.pdf"
    with fitz.open() as pdf:
        for i in range(pages):
            page = pdf.new_page(width=600, height=800)
            page.insert_text((40, 55), "Methods")
            body = TEXT.replace("alpha", "protocol " + WORDS[i])
            assert page.insert_textbox(fitz.Rect(40, 100, 550, 650), body, fontsize=10) > 0
        pdf.save(path)
    extraction = recover_native_layout(extract_pdf(path))
    extraction.update(reference_source_text="doi:10.1234/fixture123",
        reference_metadata={"doi":"10.1234/fixture123", "reference":"doi:10.1234/fixture123"})
    mapped = build_source_map(extraction)
    plan = build_ingestion_plan(extraction, AdaptiveConfig(draft_packages=True), source_doc=mapped)
    assert len(plan.batches) == pages
    return extraction, mapped, plan


def packet_index(article_text: str) -> int:
    return next(i for i, word in enumerate(WORDS) if "protocol " + word + " for" in article_text)


def packet_response(extraction, index: int) -> str:
    return response(extraction, TEXT.replace("alpha", "protocol " + WORDS[index]))


def fixed_groups(worker, total, concurrency, control, progress=None):
    """Original scheduling policy, leaving the actual generation/validation shared."""
    outcomes = []
    maximum = 0
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        for offset in range(0, total, concurrency):
            if progress:
                progress(offset)
            with control.gate:
                if control.stopped.is_set():
                    break
                futures = [(i, pool.submit(worker, i)) for i in range(offset, min(total, offset+concurrency))]
            maximum = max(maximum, len(futures))
            for i, future in futures:
                try:
                    outcomes.append(Outcome(index=i, value=future.result()))
                except BaseException as exc:
                    outcomes.append(Outcome(index=i, error=exc))
                    control.stop()
            if control.stopped.is_set():
                break
    return ScheduleRun(tuple(outcomes), None, maximum)

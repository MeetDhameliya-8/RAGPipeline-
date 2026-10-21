from pathlib import Path
import tempfile
import unittest

from src.source_map import (
    SourceMapConfig,
    build_coverage_report,
    build_source_map,
    initial_processing_state,
    load_or_initialize_processing_state,
    mark_processed_sections_from_validation,
    save_processing_state,
    section_batches,
    source_sections_for_validation,
)


class SourceMapTests(unittest.TestCase):
    def test_build_source_map_preserves_multi_page_clinical_context(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())

        titles = [section.title for section in source_doc.sections]
        self.assertIn("1 Introduction", titles)
        self.assertIn("Recommendations", titles)
        self.assertIn("2 Eligibility Criteria", titles)

        recommendations = _section_by_title(source_doc.sections, "Recommendations")
        self.assertEqual(recommendations.page_start, 1)
        self.assertEqual(recommendations.page_end, 2)
        self.assertIn("continue induction therapy", recommendations.text)
        self.assertIn("clinical response should be reassessed", recommendations.text)
        self.assertNotIn("GastroAGI Guideline", recommendations.text)
        self.assertNotIn("Page 1", recommendations.text)

    def test_build_source_map_excludes_references_and_defers_tables(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())

        table = _section_by_title(source_doc.sections, "Table 1. Outcomes by treatment group")
        self.assertEqual(table.kind, "table_or_figure")
        self.assertEqual(table.status, "deferred")
        self.assertIn("table", table.reason)

        references = _section_by_title(source_doc.sections, "References")
        self.assertEqual(references.kind, "references")
        self.assertEqual(references.status, "excluded")
        self.assertIn("reference", references.reason)

        coverage = build_coverage_report(source_doc)
        self.assertGreaterEqual(coverage["summary"]["pending"], 3)
        self.assertEqual(coverage["summary"]["deferred"], 1)
        self.assertEqual(coverage["summary"]["excluded"], 1)

    def test_processing_state_resumes_and_invalidates_stale_checkpoints(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())
        state = initial_processing_state(source_doc)
        state = state.model_copy(
            update={
                "sections": [
                    record.model_copy(update={"status": "processed", "reason": "unit_test"})
                    if record.section_id == source_doc.sections[0].section_id
                    else record
                    for record in state.sections
                ]
            }
        )

        temp_root = Path(tempfile.mkdtemp(prefix="gastroagi-source-map-"))
        try:
            state_path = temp_root / "processing_state.json"
            save_processing_state(state_path, state)

            loaded = load_or_initialize_processing_state(state_path, source_doc)
            self.assertFalse(loaded.invalidated)
            self.assertEqual(loaded.sections[0].status, "processed")

            changed = build_source_map(_changed_guideline_extraction())
            invalidated = load_or_initialize_processing_state(state_path, changed)
            self.assertTrue(invalidated.invalidated)
            self.assertEqual(invalidated.invalidation_reason, "source_content_changed")
            self.assertTrue(all(record.status != "processed" for record in invalidated.sections))
        finally:
            import shutil

            shutil.rmtree(temp_root, ignore_errors=True)

    def test_coverage_report_marks_unlinked_sections_deferred_after_validation(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())
        state = initial_processing_state(source_doc)
        recommendation = _section_by_title(source_doc.sections, "Recommendations")
        validation_report = {
            "chunk_statuses": [
                {"chunk": 1, "status": "PASS"},
            ],
            "source_evidence": [
                {
                    "chunk": 1,
                    "source_section_ids": [recommendation.section_id],
                }
            ]
        }

        updated = mark_processed_sections_from_validation(source_doc, state, validation_report)
        coverage = build_coverage_report(source_doc, updated)
        statuses = {section["section_id"]: section for section in coverage["sections"]}

        self.assertEqual(statuses[recommendation.section_id]["status"], "processed")
        pending_body_sections = [
            section
            for section in coverage["sections"]
            if section["kind"] == "body" and section["section_id"] != recommendation.section_id
        ]
        self.assertTrue(any(section["status"] == "deferred" for section in pending_body_sections))

    def test_rejected_chunk_evidence_does_not_mark_section_processed(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())
        state = initial_processing_state(source_doc)
        recommendation = _section_by_title(source_doc.sections, "Recommendations")
        validation_report = {
            "chunk_statuses": [
                {"chunk": 1, "status": "REJECTED"},
            ],
            "source_evidence": [
                {
                    "chunk": 1,
                    "source_section_ids": [recommendation.section_id],
                }
            ],
        }

        updated = mark_processed_sections_from_validation(source_doc, state, validation_report)
        status_by_id = {record.section_id: record.status for record in updated.sections}

        self.assertEqual(status_by_id[recommendation.section_id], "deferred")

    def test_missing_chunk_status_fails_closed_for_section_processing(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())
        state = initial_processing_state(source_doc)
        recommendation = _section_by_title(source_doc.sections, "Recommendations")
        validation_report = {
            "source_evidence": [
                {
                    "chunk": 1,
                    "source_section_ids": [recommendation.section_id],
                }
            ],
        }

        updated = mark_processed_sections_from_validation(source_doc, state, validation_report)
        status_by_id = {record.section_id: record.status for record in updated.sections}

        self.assertEqual(status_by_id[recommendation.section_id], "deferred")

    def test_only_pass_chunks_contribute_to_processed_sections(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())
        state = initial_processing_state(source_doc)
        recommendation = _section_by_title(source_doc.sections, "Recommendations")
        eligibility = _section_by_title(source_doc.sections, "2 Eligibility Criteria")
        validation_report = {
            "chunk_statuses": [
                {"chunk": 1, "status": "REJECTED"},
                {"chunk": 2, "status": "PASS"},
            ],
            "source_evidence": [
                {
                    "chunk": 1,
                    "source_section_ids": [recommendation.section_id],
                },
                {
                    "chunk": 2,
                    "source_section_ids": [eligibility.section_id],
                },
            ],
        }

        updated = mark_processed_sections_from_validation(source_doc, state, validation_report)
        status_by_id = {record.section_id: record.status for record in updated.sections}

        self.assertEqual(status_by_id[recommendation.section_id], "deferred")
        self.assertEqual(status_by_id[eligibility.section_id], "processed")

    def test_section_batches_skip_deferred_and_excluded_sections(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction(), SourceMapConfig(max_section_words=70))

        batches = section_batches(source_doc, max_words=70)
        batched_ids = {section_id for batch in batches for section_id in batch}
        deferred_or_excluded = {
            section.section_id
            for section in source_doc.sections
            if section.status in {"deferred", "excluded", "failed"}
        }

        self.assertTrue(batches)
        self.assertFalse(batched_ids & deferred_or_excluded)

    def test_source_sections_for_validation_preserves_traceability(self) -> None:
        source_doc = build_source_map(_long_guideline_extraction())
        validation_sections = source_sections_for_validation(source_doc)
        first = validation_sections[0]

        self.assertEqual(first["document_id"], source_doc.document_id)
        self.assertEqual(first["source_file"], "guideline.pdf")
        self.assertIn("section_id", first)
        self.assertIn("page_start", first)
        self.assertIn("page_end", first)
        self.assertNotIn("References", [section["title"] for section in validation_sections])


def _section_by_title(sections, title: str):
    for section in sections:
        if section.title == title:
            return section
    raise AssertionError(f"Missing section: {title}")


def _long_guideline_extraction() -> dict[str, object]:
    return {
        "file_name": "guideline.pdf",
        "page_count": 4,
        "pages": [
            {
                "page_number": 1,
                "text": (
                    "GastroAGI Guideline\n\n"
                    "1 Introduction\n\n"
                    "Adults with moderate inflammatory bowel disease were evaluated in a guideline panel. "
                    "The panel considered induction therapy, maintenance therapy, safety outcomes, and evidence limitations.\n\n"
                    "Recommendations\n\n"
                    "Patients with moderate disease who have objective inflammation and no contraindication should"
                ),
            },
            {
                "page_number": 2,
                "text": (
                    "GastroAGI Guideline\n\n"
                    "continue induction therapy for 8 weeks, and clinical response should be reassessed before maintenance treatment. "
                    "The recommendation is conditional because the certainty of evidence was low and adverse events were incompletely reported.\n\n"
                    "2 Eligibility Criteria\n\n"
                    "Adults aged 18 years or older with confirmed disease activity were eligible. Patients with uncontrolled infection were excluded."
                ),
            },
            {
                "page_number": 3,
                "text": (
                    "GastroAGI Guideline\n\n"
                    "Table 1. Outcomes by treatment group\n\n"
                    "Remission 45% 31% Risk ratio 1.42 Confidence interval 1.01 to 2.00\n\n"
                    "3 Outcomes\n\n"
                    "Clinical remission, endoscopic response, serious infection, and treatment discontinuation were prespecified outcomes. "
                    "Numerical findings were interpreted with statistical uncertainty."
                ),
            },
            {
                "page_number": 4,
                "text": (
                    "GastroAGI Guideline\n\n"
                    "References\n\n"
                    "1. Example Author. Example trial. Gut. 2024.\n\n"
                    "Page 4"
                ),
            },
        ],
        "full_text": "",
        "extraction_warnings": ["Low text density detected on one page."],
    }


def _changed_guideline_extraction() -> dict[str, object]:
    extraction = _long_guideline_extraction()
    pages = list(extraction["pages"])
    pages[1] = {
        "page_number": 2,
        "text": str(pages[1]["text"]) + " Additional changed source sentence.",
    }
    extraction["pages"] = pages
    return extraction

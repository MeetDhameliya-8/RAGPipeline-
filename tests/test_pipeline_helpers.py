from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from src.chunk_generator import ChunkGenerationError
from src.source_map import build_coverage_report, build_source_map, initial_processing_state
from src.pipeline import (
    build_generation_diagnostics,
    build_retry_feedback,
    find_reusable_output_for_source,
    build_human_review_gate,
    main,
    build_llm_configuration_status,
    move_to_processed,
    process_pdf,
    render_review_package_markdown,
    save_outputs,
    select_output_dir_for_processing,
    validated_chunk_subset,
    safe_output_dir_name,
    should_skip_pdf,
)


class PipelineHelperTests(unittest.TestCase):
    def test_safe_output_directory_names_are_slugged(self) -> None:
        self.assertEqual(
            safe_output_dir_name("Gut Paper: Crohn's / Infliximab 2022"),
            "gut-paper-crohn-s-infliximab-2022",
        )

    def test_safe_output_directory_name_falls_back_for_symbols(self) -> None:
        self.assertEqual(safe_output_dir_name("///"), "article")

    def test_should_skip_existing_output_without_force(self) -> None:
        with self.subTest("existing reusable output"):
            temp_root = _make_temp_dir()
            try:
                pdf_path = temp_root / "paper.pdf"
                pdf_path.write_text("fake", encoding="utf-8")
                output_dir = temp_root / "output" / "paper"
                output_dir.mkdir(parents=True)
                (output_dir / "chunks.json").write_text("{\"chunks\": []}", encoding="utf-8")
                (output_dir / "validation_report.json").write_text(
                    "{\"status\": \"PASS\"}", encoding="utf-8"
                )
                processed_dir = temp_root / "processed"
                processed_dir.mkdir()

                self.assertTrue(
                    should_skip_pdf(
                        pdf_path, output_dir, force=False, processed_dir=processed_dir
                    )
                )
                self.assertFalse(
                    should_skip_pdf(
                        pdf_path, output_dir, force=True, processed_dir=processed_dir
                    )
                )
            finally:
                _remove_tree(temp_root)

    def test_should_not_skip_failed_or_incomplete_output_without_force(self) -> None:
        temp_root = _make_temp_dir()
        try:
            pdf_path = temp_root / "paper.pdf"
            pdf_path.write_text("fake", encoding="utf-8")
            processed_dir = temp_root / "processed"
            processed_dir.mkdir()

            incomplete_output = temp_root / "output" / "incomplete"
            incomplete_output.mkdir(parents=True)
            self.assertFalse(
                should_skip_pdf(
                    pdf_path, incomplete_output, force=False, processed_dir=processed_dir
                )
            )

            failed_output = temp_root / "output" / "failed"
            failed_output.mkdir(parents=True)
            (failed_output / "chunks.json").write_text("{\"chunks\": []}", encoding="utf-8")
            (failed_output / "validation_report.json").write_text(
                "{\"status\": \"FAILED\"}", encoding="utf-8"
            )
            self.assertFalse(
                should_skip_pdf(
                    pdf_path, failed_output, force=False, processed_dir=processed_dir
                )
            )
        finally:
            _remove_tree(temp_root)
    def test_should_not_skip_processed_file_name_without_valid_output(self) -> None:
        temp_root = _make_temp_dir()
        try:
            pdf_path = temp_root / "paper.pdf"
            pdf_path.write_text("fake", encoding="utf-8")
            output_dir = temp_root / "output" / "paper"
            processed_dir = temp_root / "processed"
            processed_dir.mkdir()
            (processed_dir / "paper.pdf").write_text("processed", encoding="utf-8")

            self.assertFalse(
                should_skip_pdf(
                    pdf_path, output_dir, force=False, processed_dir=processed_dir
                )
            )
        finally:
            _remove_tree(temp_root)

    def test_reusable_output_must_match_source_content_hash(self) -> None:
        temp_root = _make_temp_dir()
        try:
            source_doc = _source_doc("paper.pdf", "Clinical source text about ulcer prevention.")
            matching_output = temp_root / "output" / "paper"
            matching_output.mkdir(parents=True)
            (matching_output / "chunks.json").write_text("{\"chunks\": []}", encoding="utf-8")
            (matching_output / "validation_report.json").write_text(
                "{\"status\": \"PASS\"}", encoding="utf-8"
            )
            (matching_output / "source_map.json").write_text(
                source_doc.model_dump_json(indent=2), encoding="utf-8"
            )

            self.assertEqual(find_reusable_output_for_source(matching_output, source_doc), matching_output)

            other_doc = _source_doc("paper.pdf", "Different clinical article content.")
            self.assertIsNone(find_reusable_output_for_source(matching_output, other_doc))
        finally:
            _remove_tree(temp_root)

    def test_valid_output_from_different_content_uses_hash_suffixed_directory(self) -> None:
        temp_root = _make_temp_dir()
        try:
            old_doc = _source_doc("paper.pdf", "Original article content.")
            new_doc = _source_doc("paper.pdf", "Revised article content.")
            output_dir = temp_root / "output" / "paper"
            output_dir.mkdir(parents=True)
            (output_dir / "chunks.json").write_text("{\"chunks\": []}", encoding="utf-8")
            (output_dir / "validation_report.json").write_text(
                "{\"status\": \"PASS\"}", encoding="utf-8"
            )
            (output_dir / "source_map.json").write_text(
                old_doc.model_dump_json(indent=2), encoding="utf-8"
            )

            selected = select_output_dir_for_processing(output_dir, new_doc, force=False)

            self.assertNotEqual(selected, output_dir)
            self.assertTrue(selected.name.startswith("paper-"))
            self.assertIn(new_doc.content_hash[:8], selected.name)
            self.assertTrue(output_dir.exists())
        finally:
            _remove_tree(temp_root)
    def test_move_to_processed_avoids_overwrite(self) -> None:
        temp_root = _make_temp_dir()
        try:
            source = temp_root / "paper.pdf"
            source.write_text("new", encoding="utf-8")
            processed = temp_root / "processed"
            processed.mkdir()
            existing = processed / "paper.pdf"
            existing.write_text("old", encoding="utf-8")

            moved = move_to_processed(source, processed)

            self.assertEqual(moved.name, "paper-1.pdf")
            self.assertEqual(moved.read_text(encoding="utf-8"), "new")
            self.assertEqual(existing.read_text(encoding="utf-8"), "old")
        finally:
            _remove_tree(temp_root)


    def test_human_review_gate_blocks_ingestion_by_default(self) -> None:
        gate = build_human_review_gate({"status": "PASS"})

        self.assertTrue(gate["approval_required"])
        self.assertFalse(gate["approved"])
        self.assertFalse(gate["ingestion_allowed"])
        self.assertIn("Explicit clinician approval", gate["reason"])

    def test_review_package_includes_source_evidence_and_approval_gate(self) -> None:
        generated = {
            "article": {
                "source_file": "paper.pdf",
                "topic": "Ulcerative Colitis",
                "subtopic": "Adults With Active Disease",
                "reference": "Gut 2024;73:1-8. doi:10.1000/example",
                "number_of_chunks": 1,
            },
            "chunks": [
                {
                    "chunk_number": 1,
                    "topic": "Ulcerative Colitis",
                    "subtopic": "Adults With Active Disease",
                    "section": "Does Treatment Improve Remission?",
                    "reference": "Gut 2024;73:1-8. doi:10.1000/example",
                    "original_text": "Ulcerative colitis treatment evidence for review. " * 30,
                }
            ],
        }
        validation_report = {
            "status": "REVIEW_REQUIRED",
            "summary": {"chunks": 1, "passed": 0, "warnings": 1, "errors": 0},
            "issues": [
                {
                    "chunk": 1,
                    "severity": "WARNING",
                    "rule": "SOURCE_GROUNDING",
                    "message": "Clinical claim sentence is not closely supported.",
                }
            ],
            "source_evidence": [
                {
                    "chunk": 1,
                    "match_type": "exact",
                    "page_numbers": [3],
                    "source_section_ids": ["results"],
                    "text_span": {"start": 10, "end": 80},
                }
            ],
            "human_review": {
                "approval_required": True,
                "approved": False,
                "ingestion_allowed": False,
            },
        }

        markdown = render_review_package_markdown(generated, validation_report)

        self.assertIn("# GastroAGI Human Review Package", markdown)
        self.assertIn("Gut 2024;73:1-8. doi:10.1000/example", markdown)
        self.assertIn("Does Treatment Improve Remission?", markdown)
        self.assertIn("Match type: exact", markdown)
        self.assertIn("Page numbers: 3", markdown)
        self.assertIn("Validation status: REVIEW_REQUIRED", markdown)
        self.assertIn("WARNING SOURCE_GROUNDING", markdown)
        self.assertIn("Approved for ingestion: NO", markdown)


    def test_save_outputs_writes_validated_and_quarantine_chunk_subsets(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gastroagi-partial-output-") as temp_dir:
            output_dir = Path(temp_dir) / "paper"
            output_dir.mkdir()
            generated = {
                "article": {
                    "source_file": "paper.pdf",
                    "topic": "Ulcerative Colitis",
                    "subtopic": "Adults",
                    "reference": "doi:10.1000/example",
                    "number_of_chunks": 2,
                },
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Ulcerative Colitis",
                        "subtopic": "Adults",
                        "section": "Supported Chunk",
                        "reference": "doi:10.1000/example",
                        "original_text": "Supported clinical text. " * 20,
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Ulcerative Colitis",
                        "subtopic": "Adults",
                        "section": "Rejected Chunk",
                        "reference": "doi:10.1000/example",
                        "original_text": "Unsupported clinical text. " * 20,
                    },
                ],
            }
            report = {
                "status": "FAILED",
                "summary": {"chunks": 2, "passed": 1, "validated_chunks": 1, "rejected_chunks": 1, "warnings": 0, "errors": 1},
                "chunk_statuses": [{"chunk": 1, "status": "PASS"}, {"chunk": 2, "status": "REJECTED"}],
                "issues": [{"severity": "ERROR", "rule": "SOURCE_GROUNDING", "category": "SOURCE_EVIDENCE_MISSING", "chunk": 2, "message": "Unsupported."}],
            }

            save_outputs(output_dir, generated, report)

            validated = json.loads((output_dir / "validated_chunks.json").read_text(encoding="utf-8"))
            reviewable = json.loads((output_dir / "local_review_chunks.json").read_text(encoding="utf-8"))
            quarantined = json.loads((output_dir / "quarantine_chunks.json").read_text(encoding="utf-8"))
            subset = validated_chunk_subset(generated, report)

            self.assertEqual([chunk["chunk_number"] for chunk in validated["chunks"]], [1])
            self.assertEqual([chunk["chunk_number"] for chunk in reviewable["chunks"]], [1])
            self.assertEqual([chunk["chunk_number"] for chunk in quarantined["chunks"]], [2])
            self.assertEqual(subset["article"]["number_of_chunks"], 1)

    def test_save_outputs_writes_reviewable_subset_when_failed_report_has_review_chunks(self) -> None:
        with tempfile.TemporaryDirectory(prefix="gastroagi-review-output-") as temp_dir:
            output_dir = Path(temp_dir) / "paper"
            output_dir.mkdir()
            generated = {
                "article": {
                    "source_file": "paper.pdf",
                    "topic": "Pancreas",
                    "subtopic": "Trial",
                    "reference": "doi:10.1000/example",
                    "number_of_chunks": 2,
                },
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Pancreas",
                        "subtopic": "Trial",
                        "section": "Rejected Claim",
                        "reference": "doi:10.1000/example",
                        "original_text": "Rejected clinical text. " * 20,
                    },
                    {
                        "chunk_number": 2,
                        "topic": "Pancreas",
                        "subtopic": "Trial",
                        "section": "Review Claim",
                        "reference": "doi:10.1000/example",
                        "original_text": "Review-required clinical text. " * 20,
                    },
                ],
            }
            report = {
                "status": "FAILED",
                "summary": {"chunks": 2, "passed": 0, "validated_chunks": 0, "review_required_chunks": 1, "rejected_chunks": 1, "warnings": 1, "errors": 1},
                "chunk_statuses": [{"chunk": 1, "status": "REJECTED"}, {"chunk": 2, "status": "REVIEW_REQUIRED"}],
                "issues": [
                    {"severity": "ERROR", "rule": "SOURCE_GROUNDING", "category": "SOURCE_EVIDENCE_MISSING", "chunk": 1, "message": "Unsupported."},
                    {"severity": "WARNING", "rule": "SOURCE_GROUNDING", "category": "SOURCE_EVIDENCE_MISSING", "chunk": 2, "message": "Review."},
                ],
            }

            save_outputs(output_dir, generated, report)

            self.assertFalse((output_dir / "validated_chunks.json").exists())
            reviewable = json.loads((output_dir / "local_review_chunks.json").read_text(encoding="utf-8"))
            quarantined = json.loads((output_dir / "quarantine_chunks.json").read_text(encoding="utf-8"))
            self.assertEqual([chunk["chunk_number"] for chunk in reviewable["chunks"]], [2])
            self.assertEqual([chunk["chunk_number"] for chunk in quarantined["chunks"]], [1, 2])
    def test_save_outputs_writes_source_map_checkpoint_and_coverage(self) -> None:
        temp_root = _make_temp_dir()
        try:
            generated = {
                "article": {
                    "source_file": "paper.pdf",
                    "topic": "Ulcerative Colitis",
                    "subtopic": "Adults With Active Disease",
                    "reference": "doi:10.1000/example",
                    "number_of_chunks": 1,
                },
                "chunks": [
                    {
                        "chunk_number": 1,
                        "topic": "Ulcerative Colitis",
                        "subtopic": "Adults With Active Disease",
                        "section": "Does Treatment Improve Remission?",
                        "reference": "doi:10.1000/example",
                        "original_text": "Ulcerative colitis treatment evidence for review. " * 30,
                    }
                ],
            }
            report = {
                "status": "PASS",
                "summary": {"chunks": 1, "passed": 1, "warnings": 0, "errors": 0},
                "issues": [],
                "human_review": build_human_review_gate({"status": "PASS"}),
            }
            source_doc = build_source_map(
                {
                    "file_name": "paper.pdf",
                    "page_count": 1,
                    "pages": [
                        {
                            "page_number": 1,
                            "text": "Recommendations\n\nUlcerative colitis treatment evidence for review.",
                        }
                    ],
                }
            )
            state = initial_processing_state(source_doc)
            coverage = build_coverage_report(source_doc, state)
            output_dir = temp_root / "article"
            output_dir.mkdir()

            save_outputs(
                output_dir,
                generated,
                report,
                source_doc=source_doc,
                processing_state=state,
                coverage_report=coverage,
            )

            self.assertTrue((output_dir / "source_map.json").exists())
            self.assertTrue((output_dir / "processing_state.json").exists())
            self.assertTrue((output_dir / "coverage_report.json").exists())
            saved_coverage = json.loads((output_dir / "coverage_report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved_coverage["document_id"], source_doc.document_id)
        finally:
            _remove_tree(temp_root)

    def test_build_retry_feedback_summarizes_previous_failed_validation(self) -> None:
        temp_root = _make_temp_dir()
        try:
            output_dir = temp_root / "output" / "paper"
            output_dir.mkdir(parents=True)
            (output_dir / "validation_report.json").write_text(
                json.dumps(
                    {
                        "status": "FAILED",
                        "issues": [
                            {
                                "chunk": 2,
                                "severity": "ERROR",
                                "rule": "SOURCE_FIDELITY",
                                "message": "Number(s) not found in extracted source text: 99.9%.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            feedback = build_retry_feedback(output_dir)

            self.assertIn("Retry Feedback", feedback)
            self.assertIn("Previous chunk 2", feedback)
            self.assertIn("99.9%", feedback)
            self.assertIn("source text only", feedback)
        finally:
            _remove_tree(temp_root)


    def test_process_pdf_reports_llm_generation_failure_stage(self) -> None:
        temp_root = _make_temp_dir()
        try:
            pdf_path = temp_root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            output_root = temp_root / "output"
            processed_root = temp_root / "processed"
            with patch("src.pipeline.OUTPUT_DIR", output_root), patch(
                "src.pipeline.PROCESSED_DIR", processed_root
            ), patch("src.pipeline.extract_pdf", return_value=_valid_pipeline_extraction()), patch(
                "src.pipeline.generate_chunks", side_effect=ChunkGenerationError("Azure timeout")
            ):
                result = process_pdf(pdf_path, test_generation=True, logger=_NoopLogger())

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_stage"], "llm_generation")
            report = json.loads((Path(result["output_dir"]) / "validation_report.json").read_text(encoding="utf-8"))
            self.assertEqual(report["issues"][0]["rule"], "GENERATION_FAILED")
        finally:
            _remove_tree(temp_root)

    def test_process_pdf_reports_output_writing_failure_stage(self) -> None:
        temp_root = _make_temp_dir()
        try:
            pdf_path = temp_root / "paper.pdf"
            pdf_path.write_bytes(b"%PDF-1.4")
            output_root = temp_root / "output"
            processed_root = temp_root / "processed"
            with patch("src.pipeline.OUTPUT_DIR", output_root), patch(
                "src.pipeline.PROCESSED_DIR", processed_root
            ), patch("src.pipeline.extract_pdf", return_value=_valid_pipeline_extraction()), patch(
                "src.pipeline.generate_chunks", return_value=_valid_pipeline_generated()
            ), patch("src.pipeline.validate_chunks", return_value=_valid_pipeline_report()), patch(
                "src.pipeline.save_outputs", side_effect=OSError("disk full")
            ):
                result = process_pdf(pdf_path, test_generation=True, logger=_NoopLogger())

            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure_stage"], "output_writing")
            self.assertIn("Output persistence failed", result["reason"])
            self.assertNotIn("disk full", result["reason"])
        finally:
            _remove_tree(temp_root)
    def test_azure_configuration_status_is_sanitized(self) -> None:
        with patch.dict(os.environ, _azure_env(), clear=True):
            status = build_llm_configuration_status()

        self.assertTrue(status["ok"])
        self.assertTrue(status["api_key_configured"])
        self.assertEqual(status["provider"], "azure")
        self.assertEqual(status["model"], "gpt-5.4")
        self.assertEqual(status["base_url"], "https://gastroagi.openai.azure.com/")
        self.assertNotIn("azure-test-key", str(status))

    def test_azure_configuration_status_flags_missing_values(self) -> None:
        for missing_name, expected_issue in (
            ("AZURE_OPENAI_API_KEY", "AZURE_OPENAI_API_KEY is not configured."),
            ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_ENDPOINT is not configured."),
            ("AZURE_OPENAI_DEPLOYMENT", "AZURE_OPENAI_DEPLOYMENT is not configured."),
            ("AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_API_VERSION is not configured."),
        ):
            env = _azure_env()
            env[missing_name] = ""
            with self.subTest(missing=missing_name):
                with patch.dict(os.environ, env, clear=True):
                    status = build_llm_configuration_status()
                self.assertFalse(status["ok"])
                self.assertIn(expected_issue, status["issues"])

    def test_azure_configuration_status_flags_invalid_endpoint_shape(self) -> None:
        env = _azure_env()
        env["AZURE_OPENAI_ENDPOINT"] = "gastroagi.openai.azure.com"

        with patch.dict(os.environ, env, clear=True):
            status = build_llm_configuration_status()

        self.assertFalse(status["ok"])
        self.assertIn("absolute https URL", status["issues"][0])

    def test_azure_configuration_status_rejects_foundry_models_endpoint(self) -> None:
        env = _azure_env()
        env["AZURE_OPENAI_ENDPOINT"] = "https://gastroagi.services.ai.azure.com/models"

        with patch.dict(os.environ, env, clear=True):
            status = build_llm_configuration_status()

        self.assertFalse(status["ok"])
        self.assertIn("Azure AI Foundry Models endpoint", status["issues"][0])
    def test_unsupported_provider_status_fails_closed(self) -> None:
        with patch.dict(os.environ, {"GASTROAGI_LLM_PROVIDER": "openai"}, clear=True):
            status = build_llm_configuration_status()

        self.assertFalse(status["ok"])
        self.assertFalse(status["api_key_configured"])
        self.assertIn("Unsupported LLM provider `openai`.", status["issues"])

    def test_check_llm_command_blocks_live_request_when_config_missing(self) -> None:
        stdout = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch(
            "src.pipeline.load_dotenv", None
        ), patch("src.pipeline.check_llm_connectivity") as check, patch(
            "src.pipeline.process_pdf"
        ) as process_pdf, redirect_stdout(stdout):
            exit_code = main(["--check-llm"])

        self.assertEqual(exit_code, 1)
        check.assert_not_called()
        process_pdf.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertFalse(result["ok"])
        self.assertFalse(result["live_request_attempted"])
        self.assertEqual(result["status"], "configuration_error")
        self.assertIn("GASTROAGI_LLM_PROVIDER", result["configuration"]["issues"][0])

    def test_check_llm_command_runs_single_sanitized_connectivity_request(self) -> None:
        stdout = io.StringIO()
        live_result = {
            "provider": "azure",
            "model": "gpt-5.4",
            "endpoint": "https://gastroagi.openai.azure.com/",
            "api_version": "2024-12-01-preview",
            "ok": True,
            "status": "success",
            "response_id": "chatcmpl-test",
            "output_text_length": 2,
        }
        with patch.dict(os.environ, _azure_env(), clear=True), patch(
            "src.pipeline.load_dotenv", None
        ), patch("src.pipeline.check_llm_connectivity", return_value=live_result) as check, patch(
            "src.pipeline.process_pdf"
        ) as process_pdf, redirect_stdout(stdout):
            exit_code = main(["--check-llm"])

        self.assertEqual(exit_code, 0)
        check.assert_called_once_with()
        process_pdf.assert_not_called()
        result = json.loads(stdout.getvalue())
        self.assertTrue(result["ok"])
        self.assertTrue(result["live_request_attempted"])
        self.assertEqual(result["status"], "success")
        self.assertTrue(result["configuration"]["api_key_configured"])
        self.assertNotIn("azure-test-key", stdout.getvalue())
    def test_azure_generation_diagnostics_detect_real_llm_mode(self) -> None:
        with patch.dict(os.environ, _azure_env(), clear=True):
            diagnostics = build_generation_diagnostics(
                {
                    "file_name": "paper.pdf",
                    "raw_full_text": "article text",
                    "full_text": "article text",
                    "reference_metadata": {"reference": "doi:10.1000/example"},
                }
            )

        self.assertEqual(diagnostics["llm_provider"], "azure")
        self.assertEqual(diagnostics["model"], "gpt-5.4")
        self.assertEqual(diagnostics["generator_mode"], "REAL LLM")



def _valid_pipeline_extraction() -> dict[str, object]:
    source_text = "Methods\n\n" + "Ulcerative colitis treatment evidence for review. " * 40 + " doi:10.1000/example"
    return {
        "file_name": "paper.pdf",
        "page_count": 1,
        "raw_full_text": source_text,
        "full_text": source_text,
        "reference_metadata": {"doi": "10.1000/example", "reference": "doi:10.1000/example"},
        "pages": [{"page_number": 1, "text": source_text, "raw_text": source_text}],
        "extraction_warnings": [],
    }


def _valid_pipeline_generated() -> dict[str, object]:
    return {
        "article": {
            "source_file": "paper.pdf",
            "topic": "Ulcerative Colitis",
            "subtopic": "Adults With Active Disease",
            "reference": "doi:10.1000/example",
            "number_of_chunks": 1,
        },
        "chunks": [
            {
                "chunk_number": 1,
                "topic": "Ulcerative Colitis",
                "subtopic": "Adults With Active Disease",
                "section": "Does Treatment Improve Remission?",
                "reference": "doi:10.1000/example",
                "original_text": "Ulcerative colitis treatment evidence for review. " * 40,
            }
        ],
    }


def _valid_pipeline_report() -> dict[str, object]:
    return {
        "status": "PASS",
        "summary": {"chunks": 1, "passed": 1, "warnings": 0, "errors": 0},
        "issues": [],
    }


class _NoopLogger:
    def info(self, *args, **kwargs):
        pass

    def warning(self, *args, **kwargs):
        pass

    def error(self, *args, **kwargs):
        pass

    def exception(self, *args, **kwargs):
        pass
def _azure_env() -> dict[str, str]:
    return {
        "GASTROAGI_LLM_PROVIDER": "azure",
        "AZURE_OPENAI_API_KEY": "azure-test-key",
        "AZURE_OPENAI_ENDPOINT": "https://gastroagi.openai.azure.com/",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-5.4",
        "AZURE_OPENAI_API_VERSION": "2024-12-01-preview",
    }

def _source_doc(file_name: str, text: str):
    return build_source_map(
        {
            "file_name": file_name,
            "page_count": 1,
            "pages": [
                {
                    "page_number": 1,
                    "text": "Methods\n\n" + text,
                }
            ],
        }
    )

def _make_temp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="gastroagi-test-"))


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)















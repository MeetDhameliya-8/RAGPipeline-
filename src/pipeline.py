"""Main orchestration layer for the GastroAGI chunk pipeline."""

from __future__ import annotations

import argparse
from collections import Counter
import json
import time
import logging
import os
from pathlib import Path
import re
import shutil
import sys
import unicodedata
from typing import Any, Callable, TYPE_CHECKING
from . import diagnostics as telemetry

if TYPE_CHECKING:
    from .citation_recovery import ReviewedCitation

try:  # pragma: no cover - import mode depends on invocation style
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from .chunk_generator import (
    DEFAULT_PROMPT_PATH,
    ChunkGenerationError,
    build_generation_input,
    check_llm_connectivity,
    generate_chunks,
    load_generation_prompt,
)
from .chunk_validator import eligible_chunk_numbers, validate_chunks
from .pdf_extractor import PdfExtractionError, extract_pdf
from .providers.azure import validate_azure_endpoint
from .source_map import (
    SourceDocument,
    ProcessingState,
    build_coverage_report,
    build_source_map,
    initial_processing_state,
    load_or_initialize_processing_state,
    mark_processed_sections_from_validation,
    source_sections_for_validation,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "input_pdfs"
PROCESSED_DIR = PROJECT_ROOT / "processed"
OUTPUT_DIR = PROJECT_ROOT / "output"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "pipeline.log"


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")

    parser = build_parser()
    args = parser.parse_args(argv)
    logger = configure_logging()

    if args.check_llm:
        return run_llm_connectivity_check(logger)
    if args.check_llm_config:
        return run_llm_configuration_check(logger)

    if args.validate_only:
        return validate_existing_chunks(Path(args.validate_only), args.dry_run, logger)

    if args.file and args.all:
        parser.error("Use either --file or --all, not both.")
    if args.dry_run and args.test_generation:
        parser.error("Use either --dry-run or --test-generation, not both.")

    pdf_paths = [Path(args.file)] if args.file else sorted(INPUT_DIR.glob("*.pdf"))
    if not pdf_paths:
        logger.info("No PDFs found to process.")
        print(f"No PDFs found in {INPUT_DIR}.")
        return 0

    failures = 0
    processed = 0
    skipped = 0
    dry_runs = 0
    incomplete = 0
    for pdf_path in pdf_paths:
        result = process_pdf(
            pdf_path,
            force=args.force,
            dry_run=args.dry_run,
            test_generation=args.test_generation,
            logger=logger,
            **({"multiview": True} if args.multiview else {}),
            **({"draft_packages": True} if args.draft_packages else {}),
        )
        if result["status"] == "failed":
            failures += 1
        elif result["status"] == "processed":
            processed += 1
        elif result["status"] == "skipped":
            skipped += 1
        elif result["status"] == "dry_run":
            dry_runs += 1
        else:
            incomplete += 1

    summary = f"Processed: {processed}; Failures: {failures}; Skipped: {skipped}"
    if dry_runs:
        summary += f"; Dry runs: {dry_runs}"
    summary += f"; Partial/incomplete: {incomplete}"
    print(summary)
    return 1 if failures else 2 if incomplete else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", help="Process one PDF path.")
    parser.add_argument("--all", action="store_true", help="Process all PDFs in input_pdfs/.")
    parser.add_argument("--force", action="store_true", help="Overwrite an existing output folder.")
    parser.add_argument("--validate-only", help="Validate an existing chunks.json file.")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing outputs or moving PDFs.")
    parser.add_argument("--multiview", action="store_true", help="Opt in to source-certified native multi-view planning.")
    parser.add_argument("--draft-packages", action="store_true", help="Draft coherent witnessed source packages for local clinical review.")
    parser.add_argument(
        "--check-llm",
        action="store_true",
        help="Run a minimal non-clinical connectivity check for the configured LLM provider.",
    )
    parser.add_argument(
        "--check-llm-config",
        action="store_true",
        help="Print sanitized LLM provider configuration without making an API request.",
    )
    parser.add_argument(
        "--test-generation",
        action="store_true",
        help="Call the real LLM and write outputs without moving the source PDF.",
    )
    return parser


def run_llm_connectivity_check(logger: logging.Logger) -> int:
    """Run a minimal non-clinical LLM connectivity check."""
    configuration = build_llm_configuration_status()
    if not configuration.get("ok"):
        result = {
            "ok": False,
            "status": "configuration_error",
            "configuration": configuration,
            "live_request_attempted": False,
        }
    else:
        try:
            result = check_llm_connectivity()
        except ChunkGenerationError as exc:
            result = {
                "ok": False,
                "status": "configuration_error",
                "message": str(exc),
            }
        result["configuration"] = configuration
        result["live_request_attempted"] = True

    logger.info(
        "LLM connectivity check provider=%s model=%s status=%s live_request_attempted=%s",
        result.get("provider") or configuration.get("provider", ""),
        result.get("model") or configuration.get("model", ""),
        result.get("status", ""),
        result.get("live_request_attempted", False),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def run_llm_configuration_check(logger: logging.Logger) -> int:
    """Print sanitized LLM configuration without making a network request."""
    result = build_llm_configuration_status()
    logger.info(
        "LLM configuration check provider=%s model=%s api_key_configured=%s",
        result.get("provider", ""),
        result.get("model", ""),
        result.get("api_key_configured", False),
    )
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def build_llm_configuration_status() -> dict[str, Any]:
    """Return safe provider configuration status without exposing credentials."""
    provider = os.getenv("GASTROAGI_LLM_PROVIDER", "").strip().lower()
    if provider == "azure":
        endpoint = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        deployment = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
        api_key_configured = bool(os.getenv("AZURE_OPENAI_API_KEY", "").strip())
        issues = []
        if not api_key_configured:
            issues.append("AZURE_OPENAI_API_KEY is not configured.")
        if not endpoint:
            issues.append("AZURE_OPENAI_ENDPOINT is not configured.")
        else:
            issues.extend(validate_azure_endpoint(endpoint))
        if not deployment:
            issues.append("AZURE_OPENAI_DEPLOYMENT is not configured.")
        if not api_version:
            issues.append("AZURE_OPENAI_API_VERSION is not configured.")
        return {
            "provider": provider,
            "model": deployment,
            "base_url": endpoint,
            "api_version": api_version,
            "api_key_configured": api_key_configured,
            "ok": not issues,
            "issues": issues,
        }

    if provider == "":
        return {
            "provider": "",
            "model": "",
            "base_url": "",
            "api_key_configured": False,
            "ok": False,
            "issues": ["GASTROAGI_LLM_PROVIDER must be set to `azure`."],
        }

    return {
        "provider": provider,
        "model": "",
        "base_url": "",
        "api_key_configured": False,
        "ok": False,
        "issues": [f"Unsupported LLM provider `{provider}`."],
    }


@telemetry.observed_run
def process_pdf(
    pdf_path: Path,
    force: bool = False,
    dry_run: bool = False,
    test_generation: bool = False,
    logger: logging.Logger | None = None,
    progress_callback: Callable[..., None] | None = None,
    multiview: bool = False,
    draft_packages: bool = False,
    reviewed_citation: ReviewedCitation | None = None,
) -> dict[str, Any]:
    """Process one PDF and report the exact stage for skips or failures."""
    logger = logger or configure_logging()
    if draft_packages:
        force = False  # New draft attempts preserve every historical output package.
    pdf_path = pdf_path.resolve()
    logger.info('Processing: [redacted]')

    if not pdf_path.exists():
        logger.error('PDF not found: [redacted]')
        return {
            "status": "failed",
            "failure_stage": "input",
            "reason": "missing_file",
            "pdf": str(pdf_path),
        }

    safe_name = safe_output_dir_name(pdf_path.stem)
    output_dir = OUTPUT_DIR / safe_name
    diagnostics: dict[str, Any] = {}
    telemetry.attach(diagnostics)
    source_doc: SourceDocument | None = None
    source_state: ProcessingState | None = None
    coverage_report: dict[str, Any] | None = None
    preparation_started=time.perf_counter()
    cached_preparation=None
    cached_visual_preparation=None
    if draft_packages and not dry_run:
        from .preparation_cache import load_preparation
        cached_visual_preparation=load_preparation(pdf_path,OUTPUT_DIR/'.preparation-cache',visual=True)
        cached_preparation=cached_visual_preparation or load_preparation(pdf_path,OUTPUT_DIR/'.preparation-cache')
        telemetry.count("preparation_cache_hits" if cached_preparation else "preparation_cache_misses")
        if cached_visual_preparation:
            telemetry.count("visual_preparation_cache_hits")

    try:
        _notify_progress(progress_callback, "EXTRACTING", {"message": "Extraction started."})
        try:
            if cached_preparation:
                extraction,source_doc=cached_preparation
                _notify_progress(progress_callback,'EXTRACTING',{'message':'Resuming verified source preparation checkpoint.'})
            else:
                extraction = extract_pdf(pdf_path)
        except PdfExtractionError as source_error:
            if not draft_packages or source_error.extraction is None:
                raise
            extraction = source_error.extraction
        if draft_packages and not dry_run:
            from .generation_preflight import require_source_reference
            try:
                if reviewed_citation is not None or extraction.get('reviewed_citation'):
                    from .citation_recovery import ReviewedCitation, apply_reviewed_citation
                    try:
                        review = reviewed_citation or ReviewedCitation.model_validate(extraction['reviewed_citation'],strict=True)
                        extraction = apply_reviewed_citation(pdf_path,extraction,review)
                    except (ValueError,OSError,RuntimeError):
                        raise ChunkGenerationError('Source citation review cannot be verified; source review required.',
                                                   code='REFERENCE_IDENTITY_UNRESOLVED') from None
                    source_doc = None  # Rebuild with additive, verified citation buffers.
                require_source_reference(extraction)
            except ChunkGenerationError as identity_error:
                diagnostics = {'failure_code':identity_error.code,'requests_started':0,
                    'generated_candidates':0,'preparation_seconds':round(time.perf_counter()-preparation_started,3),
                    'reference_preflight':'blocked_before_visual_recovery'}
                telemetry.failure(identity_error, identity_error.code)
                telemetry.attach(diagnostics)
                failure_output_dir = select_failure_output_dir(output_dir, force=force)
                save_failure_report(failure_output_dir,
                    build_pipeline_failure_report('GENERATION_FAILED',str(identity_error),diagnostics),force=force)
                return {'status':'failed','failure_stage':'source_identity','failure_code':identity_error.code,
                    'pdf':str(pdf_path),'output_dir':str(failure_output_dir),'reason':'Source identity failed; see sanitized diagnostics.'}
        if draft_packages and not cached_preparation:
            from .native_layout import recover_native_layout
            extraction = recover_native_layout(extraction)
    except (PdfExtractionError, RuntimeError, OSError, ValueError) as exc:
        telemetry.failure(exc, "EXTRACTION_FAILED")
        logger.error('PDF extraction failed for [redacted]: [redacted]')
        failure_report = build_pipeline_failure_report("EXTRACTION_FAILED", str(exc), diagnostics)
        failure_output_dir = select_failure_output_dir(output_dir, force=force)
        try:
            save_failure_report(failure_output_dir, failure_report, force=force)
            logger.info('Extraction failure report saved to [redacted]')
        except OSError as report_exc:
            logger.error('Could not write extraction failure report: [redacted]')
        return {
            "status": "failed",
            "failure_stage": "extraction",
            "pdf": str(pdf_path),
            "output_dir": str(failure_output_dir),
            "reason": "Extraction failed; see sanitized diagnostics.",
        }

    logger.info('Extracted [redacted] pages')
    _notify_progress(
        progress_callback,
        "PAGES_EXTRACTED",
        {"message": f"Pages extracted: {extraction['page_count']}.", "page_count": extraction["page_count"]},
    )
    for warning in extraction.get("extraction_warnings", []):
        logger.warning('[redacted]')

    _notify_progress(progress_callback, "CLEANING", {"message": "Cleaning and source mapping started."})
    source_doc = source_doc or build_source_map(extraction)
    from .adaptive_ingestion import AdaptiveConfig, build_ingestion_plan
    generation_plan = build_ingestion_plan(extraction, AdaptiveConfig(multiview=multiview,draft_packages=draft_packages,all_draft_packages=draft_packages) if multiview or draft_packages else None,
        source_doc=source_doc if extraction.get("evidence") is not None else None)
    native_package_pages = {page for package in (generation_plan.source_package_plan.packages if draft_packages else []) if not package.visual_source_ids
                            for page in package.page_numbers}
    if draft_packages:
        from .hybrid_routing import recovery_decisions
        decisions=recovery_decisions(source_doc.evidence,native_package_pages,generation_plan.source_package_plan.holds)
        recovery_pages=[d.page_number for d in decisions if d.image_reading]
        cache_only_pages=[d.page_number for d in decisions if not d.image_reading and d.page_number not in native_package_pages]
        extraction['hybrid_page_routes']=[{'page_number':d.page_number,'image_reading':d.image_reading,'reason':d.reason} for d in decisions]
        extraction['preparation_checkpoint']={'native_reused':bool(cached_preparation),'visual_reused':bool(cached_visual_preparation)}
        if not dry_run and not cached_preparation:
            from .preparation_cache import save_preparation
            try:save_preparation(pdf_path,extraction,source_doc,OUTPUT_DIR/'.preparation-cache')
            except (OSError, ValueError):
                logger.warning('Native preparation checkpoint unavailable.')
    else:
        recovery_pages=[];cache_only_pages=[]
    if draft_packages and (recovery_pages or cache_only_pages) and not dry_run and not cached_visual_preparation:
        from .visual_source import recover_visual_source
        from .preparation_cache import load_preparation,save_preparation
        visual_preparation=load_preparation(pdf_path,OUTPUT_DIR/'.preparation-cache',visual=True)
        _notify_progress(progress_callback,"EXTRACTING",{"message":f"Hybrid recovery: {len(recovery_pages)} pages selected for image reading; completed source caches will be reused."})
        try:
            if visual_preparation:
                extraction,source_doc=visual_preparation
                extraction['preparation_checkpoint']={'native_reused':bool(cached_preparation),'visual_reused':True}
            else:
                for selected_pages,allow_network in ((recovery_pages,True),(cache_only_pages,False)):
                    if selected_pages:
                        extraction = recover_visual_source(pdf_path,extraction,
                            cache_directory=OUTPUT_DIR/".visual-evidence"/source_doc.evidence.pdf_sha256,
                            page_numbers=selected_pages,progress_callback=progress_callback,allow_network=allow_network)
                source_doc = build_source_map(extraction)
            generation_plan = build_ingestion_plan(extraction,AdaptiveConfig(draft_packages=True,all_draft_packages=True),source_doc=source_doc)
            if not visual_preparation and not (extraction.get('visual_recovery_outcome') or {}).get('failures'):
                try:save_preparation(pdf_path,extraction,source_doc,OUTPUT_DIR/'.preparation-cache',visual=True)
                except (OSError, ValueError):
                    logger.warning('Visual preparation checkpoint unavailable.')
        except (ChunkGenerationError,ValueError,OSError) as visual_error:
            extraction["visual_recovery_outcome"]={"status":"unresolved","failure_type":type(visual_error).__name__,
                "failure_code":getattr(visual_error,"code","VISUAL_SOURCE_UNRESOLVED"),
                "failures":[{"code":getattr(visual_error,"code","VISUAL_SOURCE_UNRESOLVED"),
                    "retryable":(getattr(visual_error,"diagnostics",{}).get("error") or {}).get("retryable")}]}
    _notify_progress(
        progress_callback,
        "CLEANING_COMPLETED",
        {"message": "Cleaning completed.", "source_section_count": len(source_doc.sections)},
    )
    if not dry_run and not force:
        reusable_output_dir = find_reusable_output_for_source(output_dir, source_doc)
        if reusable_output_dir is not None:
            logger.info('Skipping PDF with existing validated output for same content: [redacted]')
            return {
                "status": "skipped",
                "reason": "valid_identical_output_exists",
                "pdf": str(pdf_path),
                "output_dir": str(reusable_output_dir),
                "validation_status": read_output_validation_status(reusable_output_dir),
                "source_content_hash": source_doc.content_hash,
            }
        output_dir = select_output_dir_for_processing(output_dir, source_doc, force=force)

    source_state = (
        initial_processing_state(source_doc)
        if force and output_dir.exists()
        else load_or_initialize_processing_state(output_dir / "processing_state.json", source_doc)
    )
    coverage_report = build_coverage_report(source_doc, source_state)

    diagnostics = build_generation_diagnostics(extraction, generation_plan=generation_plan) | {
        'preparation_seconds':round(time.perf_counter()-preparation_started,3),
        'preparation_checkpoint':extraction.get('preparation_checkpoint'),
        'hybrid_page_routes':extraction.get('hybrid_page_routes'),
        "source_document_id": source_doc.document_id,
        "source_section_count": len(source_doc.sections),
        "source_map_warnings": source_doc.warnings,
        "checkpoint_invalidated": source_state.invalidated,
        "checkpoint_invalidation_reason": source_state.invalidation_reason,
    }
    telemetry.attach(diagnostics)
    _log_generation_diagnostics(diagnostics, logger)
    if dry_run:
        print(
            json.dumps(
                {
                    "dry_run": True,
                    "pdf": str(pdf_path),
                    "output_dir": str(output_dir),
                    "llm_call": False,
                    "would_create": [
                        str(output_dir / "chunks.json"),
                        str(output_dir / "chunks.md"),
                        str(output_dir / "validation_report.json"),
                        str(output_dir / "review_package.md"),
                        str(output_dir / "source_map.json"),
                        str(output_dir / "processing_state.json"),
                        str(output_dir / "coverage_report.json"),
                    ],
                    "would_move_pdf": False,
                    "diagnostics": diagnostics,
                },
                indent=2,
            )
        )
        logger.info('Dry run complete for: [redacted]')
        return {"status": "dry_run", "dry_run": True, "pdf": str(pdf_path), "output_dir": str(output_dir)}

    generation_started = time.perf_counter()
    try:
        _notify_progress(progress_callback, "GENERATING_CHUNKS", {"message": "LLM chunk generation started."})
        generated = generate_chunks(
            extraction,
            retry_feedback=build_retry_feedback(output_dir),
            diagnostics=diagnostics,
            source_doc=source_doc,
            generation_plan=generation_plan,
            **({"attempt_directory": OUTPUT_DIR/".draft-unit-cache"/source_doc.evidence.pdf_sha256[:16],
                "progress_callback":progress_callback,"package_concurrency":2} if draft_packages else {}),
        )
    except ChunkGenerationError as exc:
        telemetry.failure(exc, "GENERATION_FAILED")
        diagnostics['generation_seconds'] = round(time.perf_counter()-generation_started,3)
        diagnostics["failure_code"] = exc.code
        logger.error('LLM generation failed for [redacted]: [redacted]')
        failure_report = build_pipeline_failure_report("GENERATION_FAILED", str(exc), diagnostics)
        try:
            save_failure_report(
                output_dir,
                failure_report,
                force=force,
                source_doc=source_doc,
                processing_state=source_state,
                coverage_report=coverage_report,
            )
            logger.info('Generation failure report saved to [redacted]')
        except OSError as report_exc:
            logger.error('Could not write generation failure report: [redacted]')
        return {
            "status": "failed",
            "failure_stage": "llm_generation",
            "failure_code": exc.code,
            "pdf": str(pdf_path),
            "output_dir": str(output_dir),
            "reason": "Generation failed; see sanitized diagnostics.",
        }

    diagnostics['generation_seconds'] = round(time.perf_counter()-generation_started,3)
    logger.info('Generated [redacted] chunks')
    _notify_progress(
        progress_callback,
        "CHUNK_BATCH_COMPLETED",
        {"message": f"Generated {len(generated.get('chunks', []))} chunks.", "chunks_generated": len(generated.get("chunks", []))},
    )

    try:
        _notify_progress(progress_callback, "VALIDATING", {"message": "Chunk validation started."})
        validation_options = {}
        if generation_plan.multiview_selection is not None:
            from .multiview import validation_sources
            validation_options["chunk_sources"] = validation_sources(generation_plan.multiview_selection, source_doc, diagnostics)
        elif generation_plan.source_package_plan is not None:
            from .source_packages import package_validation_sources
            validation_options["chunk_sources"] = package_validation_sources(generation_plan.source_package_plan,source_doc.evidence,diagnostics)
        with telemetry.stage("final_document_validation"):
            report = validate_chunks(
                generated,
                source_text=extraction.get("full_text", ""),
                previous_output_root=OUTPUT_DIR,
                source_pages=extraction.get("pages", []),
                source_sections=source_sections_for_validation(source_doc),
                **validation_options,
            )
    except (RuntimeError, OSError, ValueError) as exc:
        telemetry.failure(exc, "VALIDATION_FAILED")
        logger.error('Chunk validation failed for [redacted]: [redacted]')
        failure_report = build_pipeline_failure_report("VALIDATION_FAILED", str(exc), diagnostics)
        try:
            save_failure_report(
                output_dir,
                failure_report,
                force=force,
                source_doc=source_doc,
                processing_state=source_state,
                coverage_report=coverage_report,
            )
            logger.info('Validation failure report saved to [redacted]')
        except OSError as report_exc:
            logger.error('Could not write validation failure report: [redacted]')
        return {
            "status": "failed",
            "failure_stage": "chunk_validation",
            "pdf": str(pdf_path),
            "output_dir": str(output_dir),
            "reason": "Validation failed; see sanitized diagnostics.",
        }

    report["diagnostics"] = diagnostics | {
        "number_of_chunks_returned": len(generated.get("chunks", [])),
        "validation_result": report["status"],
    }
    report["human_review"] = build_human_review_gate(report)
    telemetry.attach(report["diagnostics"])
    telemetry.count("final_document_chunks", len(generated.get("chunks", [])), replace=True)
    telemetry.count("final_reviewable_chunks", len(eligible_chunk_numbers(report, {"PASS", "REVIEW_REQUIRED"})), replace=True)
    telemetry.count("final_rejected_chunks", len(eligible_chunk_numbers(report, {"REJECTED"})), replace=True)
    if generation_plan.source_package_plan is None:
        telemetry.count("assessed_candidate_events", len(generated.get("chunks", [])))
        telemetry.count("accepted_candidate_events", len(eligible_chunk_numbers(report, {"PASS", "REVIEW_REQUIRED"})))
        telemetry.count("rejected_candidate_events", len(eligible_chunk_numbers(report, {"REJECTED"})))
    supported_count = len(eligible_chunk_numbers(report, {"PASS"}))
    review_count = len(eligible_chunk_numbers(report, {"REVIEW_REQUIRED"}))
    report["summary"].update(
        generated_candidates=diagnostics.get("generated_candidates", len(generated.get("chunks", []))),
        structurally_valid_chunks=diagnostics.get("structurally_valid_chunks", len(generated.get("chunks", []))),
        source_supported_chunks=supported_count, clinically_approved_chunks=0,
    )
    concept_coverage = None
    if generation_plan.source_package_plan is not None:
        packages = generation_plan.source_package_plan
        report["diagnostics"]["local_storage_identity"] = {"version":"pdf-content-v1", "pdf_sha256":packages.pdf_sha256}
        concept_coverage = {"plan_id":packages.plan_id,"complete":False,"complete_section_ids":[],
            "draft_evidence_packages":len(packages.packages),"source_holds":len(packages.holds),"clinically_approved_concepts":0}
        report["clinical_concept_coverage"] = concept_coverage
        report["human_review"]["source_document_complete"] = False
    if generation_plan.clinical_plan is not None:
        from .concept_planner import reconcile_concept_coverage
        if generation_plan.multiview_selection is not None:
            from .multiview import reconcile_multiview_coverage
            concept_coverage = reconcile_multiview_coverage(generation_plan.multiview_selection, source_doc, generated, report, diagnostics)
        else:
            concept_coverage = reconcile_concept_coverage(generation_plan.clinical_plan, source_doc.evidence, generated, report, diagnostics)
        report["clinical_concept_coverage"] = concept_coverage
        report["human_review"]["source_document_complete"] = concept_coverage["complete"]
    if generation_plan.document_concept_plan is not None:
        document_plan = generation_plan.document_concept_plan
        document_accounting = {
            "plan_id": document_plan.plan_id,
            "coverage_counts": document_plan.coverage_counts,
            "canonical_character_counts": document_plan.canonical_character_counts,
            "planning_accounted": document_plan.planning_accounted,
            "complete": document_plan.document_complete,
            "clinically_approved": False,
        }
        report["document_concept_coverage"] = document_accounting
        report["human_review"]["source_document_complete"] = document_plan.document_complete
        if concept_coverage is not None:
            # Literal chunk matching remains useful evidence, but is not proof
            # of complete clinical relationship/region coverage. Drafts still
            # persist normally; unresolved documents keep the existing partial
            # success path and their source PDF.
            concept_coverage["literal_source_coverage_complete"] = concept_coverage["complete"]
            concept_coverage["complete"] = document_plan.document_complete
            concept_coverage["document_concept_plan_id"] = document_plan.plan_id
    source_state = mark_processed_sections_from_validation(source_doc, source_state, report, concept_coverage=concept_coverage)
    coverage_report = build_coverage_report(source_doc, source_state, concept_coverage=concept_coverage)
    if generation_plan.document_concept_plan is not None:
        coverage_report["document_concept_coverage"] = document_accounting
    _log_validation_issues(report, logger)
    logger.info('Validation status: [redacted]')
    _notify_progress(
        progress_callback,
        "CHUNKS_VALIDATED",
        {
            "message": f"Chunks validated: {report['status']}.",
            "validation_status": report["status"],
            "chunks_validated": supported_count,
            "review_required_chunks": review_count,
            "clinically_approved_chunks": 0,
        },
    )

    try:
        _notify_progress(progress_callback, "SAVING_OUTPUT", {"message": "Saving chunk JSON and review package."})
        if not prepare_output_dir(output_dir, force=force):
            logger.info('Output already completed by another request: [redacted]')
            return {
                "status": "skipped",
                "pdf": str(pdf_path),
                "output_dir": str(output_dir),
                "validation_status": read_output_validation_status(output_dir),
                "reason": "valid_identical_output_exists",
            }
        save_outputs(
            output_dir,
            generated,
            report,
            source_doc=source_doc,
            processing_state=source_state,
            coverage_report=coverage_report,
        )
    except (OSError, ValueError) as exc:
        telemetry.failure(exc, "UNCLASSIFIED_ERROR")
        logger.error('Output writing failed for [redacted]: [redacted]')
        return {
            "status": "failed",
            "failure_stage": "output_writing",
            "pdf": str(pdf_path),
            "output_dir": str(output_dir),
            "reason": "Output persistence failed; see sanitized diagnostics.",
        }
    logger.info('Output saved to [redacted]')
    _notify_progress(
        progress_callback,
        "JSON_SAVED",
        {"message": "JSON output saved.", "output_dir": str(output_dir), "chunks_json": str(output_dir / "chunks.json")},
    )

    if report["status"] == "FAILED":
        reviewable_generated = reviewable_chunk_subset(generated, report)
        if reviewable_generated["chunks"]:
            logger.warning('Partial validation success for [redacted]: [redacted] reviewable chunk(s), [redacted] rejected chunk(s).')
            return {
                "status": "partial_success",
                "failure_stage": "chunk_validation",
                "pdf": str(pdf_path),
                "output_dir": str(output_dir),
                "validated_chunks_path": str(output_dir / "local_review_chunks.json"),
                "validation_status": report["status"],
                "reason": "partial_chunk_validation",
                "chunk_counts": {
                    "generated": len(generated.get("chunks", [])),
                    "validated": supported_count,
                    "source_supported": supported_count,
                    "review_required": review_count,
                    "clinically_approved": 0,
                    "rejected": len(generated.get("chunks", [])) - len(reviewable_generated["chunks"]),
                    "stored": 0,
                },
            }
        logger.error('Validation failed; source PDF remains in input_pdfs: [redacted]')
        return {
            "status": "failed",
            "failure_stage": "chunk_validation",
            "pdf": str(pdf_path),
            "output_dir": str(output_dir),
            "validation_status": report["status"],
            "reason": "chunk_validation_failed",
        }

    if concept_coverage is not None and not concept_coverage["complete"]:
        return {
            "status":"partial_success", "reason":"incomplete_clinical_concept_coverage",
            "pdf":str(pdf_path), "output_dir":str(output_dir), "validation_status":report["status"],
            "validated_chunks_path":str(output_dir / "local_review_chunks.json"),
            "source_pdf_moved":False, "source_coverage_complete":False,
            "chunk_counts":{"generated":len(generated.get("chunks",[])),"validated":supported_count,
                "source_supported":supported_count,"review_required":review_count,"clinically_approved":0,"rejected":0,"stored":0},
        }
    if test_generation:
        logger.info('Test generation complete; source PDF remains in input_pdfs: [redacted]')
        return {
            "status": "processed",
            "pdf": str(pdf_path),
            "output_dir": str(output_dir),
            "validation_status": report["status"],
            "source_pdf_moved": False,
        }

    moved_to = move_to_processed(pdf_path, PROCESSED_DIR)
    logger.info('Moved source PDF to processed: [redacted]')
    return {
        "status": "processed",
        "pdf": str(pdf_path),
        "output_dir": str(output_dir),
        "processed_pdf": str(moved_to),
        "validation_status": report["status"],
    }


def _notify_progress(progress_callback: Callable[..., None] | None, stage: str, detail: dict[str, Any] | None = None) -> None:
    if progress_callback is not None:
        progress_callback(stage, detail or {})


def validate_existing_chunks(
    chunks_json: Path, dry_run: bool, logger: logging.Logger
) -> int:
    """Validate an existing chunks.json file and optionally save the report beside it."""
    if not chunks_json.exists():
        logger.error("chunks.json not found: %s", chunks_json)
        print(f"chunks.json not found: {chunks_json}")
        return 1

    try:
        data = json.loads(chunks_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        logger.error("Malformed JSON: %s", exc)
        print(f"Malformed JSON: {exc}")
        return 1

    report = validate_chunks(data)
    print(json.dumps(report, indent=2))
    if not dry_run:
        report_path = chunks_json.parent / "validation_report.json"
        report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        logger.info("Validation report saved to %s", report_path)
    return 1 if report["status"] == "FAILED" else 0


@telemetry.timed("output_persistence")
def save_outputs(
    output_dir: Path,
    generated: dict[str, Any],
    validation_report: dict[str, Any],
    source_doc: SourceDocument | None = None,
    processing_state: ProcessingState | None = None,
    coverage_report: dict[str, Any] | None = None,
) -> None:
    """Write JSON, Markdown, and validation report files."""
    # Persist a content-free snapshot as of this write. The returned result/queue
    # snapshot includes completed persistence and subsequent SQLite readback.
    if telemetry.current() is not None:
        telemetry.current().publish()
    (output_dir / "chunks.json").write_text(
        json.dumps(generated, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "chunks.md").write_text(
        render_chunks_markdown(generated), encoding="utf-8"
    )
    (output_dir / "validation_report.json").write_text(
        json.dumps(validation_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    (output_dir / "review_package.md").write_text(
        render_review_package_markdown(generated, validation_report), encoding="utf-8"
    )
    partial_generated = validated_chunk_subset(generated, validation_report)
    reviewable_generated = reviewable_chunk_subset(generated, validation_report)
    quarantined_generated = quarantined_chunk_subset(generated, validation_report)
    if partial_generated["chunks"]:
        (output_dir / "validated_chunks.json").write_text(
            json.dumps(partial_generated, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if reviewable_generated["chunks"]:
        (output_dir / "local_review_chunks.json").write_text(
            json.dumps(reviewable_generated, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        telemetry.count("saved_review_drafts", len(reviewable_generated["chunks"]), replace=True)
    if quarantined_generated["chunks"]:
        (output_dir / "quarantine_chunks.json").write_text(
            json.dumps(quarantined_generated, indent=2, ensure_ascii=False), encoding="utf-8"
        )
    if source_doc is not None:
        from .evidence_storage import save_source_map
        save_source_map(output_dir / "source_map.json", source_doc, overwrite=True)
    if processing_state is not None:
        (output_dir / "processing_state.json").write_text(
            processing_state.model_dump_json(indent=2), encoding="utf-8"
        )
    if coverage_report is not None:
        (output_dir / "coverage_report.json").write_text(
            json.dumps(coverage_report, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def validated_chunk_subset(generated: dict[str, Any], validation_report: dict[str, Any]) -> dict[str, Any]:
    """Return a generated-document subset containing only chunks with PASS status."""
    return _chunk_subset_by_status(generated, validation_report, {"PASS"})


def reviewable_chunk_subset(generated: dict[str, Any], validation_report: dict[str, Any]) -> dict[str, Any]:
    """Return chunks safe for local review storage, excluding rejected chunks."""
    return _chunk_subset_by_status(generated, validation_report, {"PASS", "REVIEW_REQUIRED"})


def quarantined_chunk_subset(generated: dict[str, Any], validation_report: dict[str, Any]) -> dict[str, Any]:
    """Return chunks that require review or were rejected."""
    return _chunk_subset_by_status(generated, validation_report, {"REVIEW_REQUIRED", "REJECTED"})


def _chunk_subset_by_status(
    generated: dict[str, Any],
    validation_report: dict[str, Any],
    statuses: set[str],
) -> dict[str, Any]:
    chunks = generated.get("chunks", []) if isinstance(generated, dict) else []
    article = dict(generated.get("article", {})) if isinstance(generated.get("article"), dict) else {}
    eligible_numbers = eligible_chunk_numbers(validation_report, statuses)
    selected = [
        chunk
        for index, chunk in enumerate(chunks if isinstance(chunks, list) else [], start=1)
        if isinstance(chunk, dict)
        and int(chunk.get("chunk_number") or index) in eligible_numbers
    ]
    article["number_of_chunks"] = len(selected)
    return {"article": article, "chunks": selected}


def prepare_output_dir(output_dir: Path, force: bool) -> bool:
    """Create a clean output directory for a generation attempt.

    Return False when another request has already produced a reusable package.
    """
    if output_dir.exists() and force:
        remove_output_dir(output_dir)
    elif output_dir.exists() and output_dir_has_reusable_chunks(output_dir):
        return False
    elif output_dir.exists():
        remove_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    return True


@telemetry.timed("output_persistence")
def save_failure_report(
    output_dir: Path,
    validation_report: dict[str, Any],
    force: bool,
    source_doc: SourceDocument | None = None,
    processing_state: ProcessingState | None = None,
    coverage_report: dict[str, Any] | None = None,
) -> None:
    """Persist a failure report without writing chunk drafts."""
    if telemetry.current() is not None:
        telemetry.current().publish()
    if output_dir.exists() and force:
        remove_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "validation_report.json").write_text(
        json.dumps(validation_report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    if source_doc is not None:
        from .evidence_storage import save_source_map
        save_source_map(output_dir / "source_map.json", source_doc, overwrite=True)
    if processing_state is not None:
        (output_dir / "processing_state.json").write_text(
            processing_state.model_dump_json(indent=2), encoding="utf-8"
        )
    if coverage_report is not None:
        (output_dir / "coverage_report.json").write_text(
            json.dumps(coverage_report, indent=2, ensure_ascii=False), encoding="utf-8"
        )


def render_chunks_markdown(generated: dict[str, Any]) -> str:
    """Render generated chunks into a human-reviewable Markdown file."""
    article = generated.get("article", {})
    chunks = generated.get("chunks", [])
    lines = [
        "# Recommended classification",
        "",
        f"Topic: {article.get('topic', '')}",
        f"Subtopic: {article.get('subtopic', '')}",
        f"Reference: {article.get('reference', '')}",
        f"Number of chunks: {len(chunks)}",
        "",
    ]

    for index, chunk in enumerate(chunks, start=1):
        lines.extend(
            [
                f"## Chunk {chunk.get('chunk_number', index)}",
                "",
                f"Topic: {chunk.get('topic', '')}",
                f"Subtopic: {chunk.get('subtopic', '')}",
                f"Section: {chunk.get('section', '')}",
                f"Reference: {chunk.get('reference', '')}",
                "Original Text:",
                "",
                str(chunk.get("original_text", "")),
                "",
            ]
        )

    return "\n".join(lines).rstrip() + "\n"



def build_human_review_gate(validation_report: dict[str, Any]) -> dict[str, Any]:
    """Return the explicit human-review gate for downstream ingestion decisions."""
    status = str(validation_report.get("status", "")).strip() or "UNKNOWN"
    return {
        "approval_required": True,
        "approved": False,
        "ingestion_allowed": False,
        "approval_scope": "Admin Panel upload, vector indexing, and production ingestion",
        "reason": (
            "Explicit clinician approval is required before ingestion. "
            f"Current validation status is {status}."
        ),
    }


def render_review_package_markdown(
    generated: dict[str, Any], validation_report: dict[str, Any]
) -> str:
    """Render a clinician-facing review package with validation and provenance."""
    article = generated.get("article", {}) if isinstance(generated, dict) else {}
    chunks = generated.get("chunks", []) if isinstance(generated, dict) else []
    issues = validation_report.get("issues", []) if isinstance(validation_report, dict) else []
    evidence = validation_report.get("source_evidence", []) if isinstance(validation_report, dict) else []
    diagnostics = validation_report.get("diagnostics", {}) if isinstance(validation_report, dict) else {}
    human_review = validation_report.get("human_review", {}) if isinstance(validation_report, dict) else {}
    status = str(validation_report.get("status", "UNKNOWN"))
    summary = validation_report.get("summary", {}) if isinstance(validation_report, dict) else {}
    source_reference = (
        str(article.get("reference", "")).strip()
        or str(diagnostics.get("reference_metadata_detected", "")).strip()
        or "UNVERIFIED"
    )

    lines = [
        "# GastroAGI Human Review Package",
        "",
        "## Review Gate",
        "",
        f"Validation status: {status}",
        "Approval required before ingestion: YES",
        f"Approved for ingestion: {'YES' if human_review.get('approved') else 'NO'}",
        f"Ingestion allowed: {'YES' if human_review.get('ingestion_allowed') else 'NO'}",
        "Approval scope: Admin Panel upload, vector indexing, and production ingestion",
        "",
        "## Original Source Reference",
        "",
        f"Source file: {article.get('source_file', '')}",
        f"Reference: {source_reference}",
        f"Topic: {article.get('topic', '')}",
        f"Subtopic: {article.get('subtopic', '')}",
        f"Chunks: {summary.get('chunks', len(chunks))}",
        f"Warnings: {summary.get('warnings', 0)}",
        f"Errors: {summary.get('errors', 0)}",
        "",
        "## Global Warnings And Rejection Reasons",
        "",
    ]
    global_issues = [issue for issue in issues if not isinstance(issue.get("chunk"), int)]
    if global_issues:
        lines.extend(_render_issue_lines(global_issues))
    else:
        lines.append("- None.")
    lines.append("")

    for index, chunk in enumerate(chunks if isinstance(chunks, list) else [], start=1):
        if not isinstance(chunk, dict):
            continue
        chunk_number = int(chunk.get("chunk_number") or index)
        chunk_issues = [issue for issue in issues if issue.get("chunk") == chunk_number]
        chunk_status = _chunk_review_status(chunk_issues)
        chunk_evidence = _evidence_for_chunk(evidence, chunk_number)
        lines.extend(
            [
                f"## Chunk {chunk_number}: {chunk.get('section', '')}",
                "",
                "### Generated Chunk",
                "",
                f"Topic: {chunk.get('topic', '')}",
                f"Subtopic: {chunk.get('subtopic', '')}",
                f"Section: {chunk.get('section', '')}",
                f"Reference: {chunk.get('reference', '')}",
                "Original Text:",
                "",
                str(chunk.get("original_text", "")),
                "",
                "### Source Evidence",
                "",
            ]
        )
        lines.extend(_render_evidence_lines(chunk_evidence))
        lines.extend(
            [
                "",
                "### Validation Status",
                "",
                f"Chunk status: {chunk_status}",
                "Warnings and rejection reasons:",
                "",
            ]
        )
        if chunk_issues:
            lines.extend(_render_issue_lines(chunk_issues))
        else:
            lines.append("- None.")
        lines.append("")

    lines.extend(
        [
            "## Approval Requirement",
            "",
            "This package is NOT approved for ingestion by default. A clinician or designated reviewer must explicitly approve the chunks before Admin Panel upload, vector indexing, or production ingestion. The current workflow remains review-required until it has been thoroughly validated.",
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def _render_issue_lines(issues: list[dict[str, Any]]) -> list[str]:
    lines = []
    for issue in issues:
        severity = issue.get("severity", "WARNING")
        rule = issue.get("rule", "VALIDATION")
        message = issue.get("message", "")
        lines.append(f"- {severity} {rule}: {message}")
    return lines


def _render_evidence_lines(evidence: dict[str, Any] | None) -> list[str]:
    if not evidence:
        return ["- No source evidence recorded."]
    page_numbers = evidence.get("page_numbers") or []
    section_ids = evidence.get("source_section_ids") or []
    text_span = evidence.get("text_span")
    lines = [
        f"- Match type: {evidence.get('match_type', 'not_found')}",
        f"- Page numbers: {', '.join(str(page) for page in page_numbers) if page_numbers else 'Not available'}",
        f"- Source section IDs: {', '.join(str(section) for section in section_ids) if section_ids else 'Not available'}",
    ]
    if isinstance(text_span, dict):
        lines.append(f"- Text span: {text_span.get('start')} to {text_span.get('end')}")
    else:
        lines.append("- Text span: Not available")
    return lines


def _chunk_review_status(issues: list[dict[str, Any]]) -> str:
    if any(issue.get("severity") == "ERROR" for issue in issues):
        return "REJECTED"
    if issues:
        return "REVIEW_REQUIRED"
    return "PASS"


def _evidence_for_chunk(evidence: Any, chunk_number: int) -> dict[str, Any] | None:
    if not isinstance(evidence, list):
        return None
    for item in evidence:
        if isinstance(item, dict) and item.get("chunk") == chunk_number:
            return item
    return None

def safe_output_dir_name(name: str, max_length: int = 90) -> str:
    """Return a filesystem-safe article output directory name."""
    normalized = unicodedata.normalize("NFKD", name)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name)
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    return (slug[:max_length].strip("-._") or "article").lower()


def build_generation_diagnostics(
    extraction: dict[str, Any], prompt_path: Path = DEFAULT_PROMPT_PATH, *, generation_plan: Any = None
) -> dict[str, Any]:
    """Return generation diagnostics safe for logs and validation reports."""
    provider = os.getenv("GASTROAGI_LLM_PROVIDER", "").strip().lower()
    effective_provider = provider or ""
    if effective_provider == "azure":
        model = os.getenv("AZURE_OPENAI_DEPLOYMENT", "").strip()
        base_url = os.getenv("AZURE_OPENAI_ENDPOINT", "").strip()
        api_version = os.getenv("AZURE_OPENAI_API_VERSION", "").strip()
        api_key_configured = bool(os.getenv("AZURE_OPENAI_API_KEY", "").strip())
    else:
        model = ""
        base_url = ""
        api_version = ""
        api_key_configured = False
    prompt_loaded = False
    prompt_error = ""
    try:
        load_generation_prompt(prompt_path)
        prompt_loaded = True
    except ChunkGenerationError as exc:
        prompt_error = str(exc)

    generator_mode = "REAL LLM" if (
        effective_provider == "azure"
        and prompt_loaded
        and api_key_configured
        and model
        and base_url
        and api_version
    ) else "UNAVAILABLE"

    from .adaptive_ingestion import build_ingestion_plan
    plan = generation_plan or build_ingestion_plan(extraction)
    reference_metadata = extraction.get("reference_metadata") or {}
    if not isinstance(reference_metadata, dict):
        reference_metadata = {}

    multiview_metrics = {}
    if plan.source_package_plan is not None:
        packages = plan.source_package_plan
        multiview_metrics = {"source_package_plan_id":packages.plan_id,
            "planned_source_characters":sum(p.request_characters for p in packages.packages),
            "planned_request_characters":sum(p.request_characters for p in packages.packages),
            "source_package_count":len(packages.packages),"source_holds":len(packages.holds)}
    if plan.multiview_selection is not None:
        selection = plan.multiview_selection
        requests = 0
        for row in selection.selected:
            clinical = plan.clinical_plan if row.view == "original" else selection.alternate_plan
            requests += next(u.request_characters for u in clinical.units if u.unit_id == row.unit_id)
        multiview_metrics = {"multiview_selection_id": selection.selection_id,
            "planned_request_characters": requests,
            "unique_selected_units": len(selection.selected),
            "multiview_decision_counts": dict(Counter(r.outcome for r in selection.decisions)),
            "blocked_source_units": sum(not r.selected and r.outcome != "duplicate_representation" for r in selection.decisions)}

    return {
        "llm_provider": effective_provider,
        "model": model,
        "base_url": base_url,
        "api_version": api_version,
        "prompt_loaded": prompt_loaded,
        "prompt_path": str(prompt_path),
        "prompt_error": prompt_error,
        "characters_extracted": len(str(extraction.get("raw_full_text") or extraction.get("full_text") or "")),
        "characters_sent_to_model": 0,
        "system_prompt_characters_sent": 0,
        "planned_source_characters": sum(s.char_end-s.char_start for b in plan.batches for s in b.ownership) if plan.clinical_plan else len(str(extraction.get("full_text") or "")),
        "document_source_characters": len(str(extraction.get("full_text") or "")),
        "planned_context_characters": sum(s.char_end-s.char_start for b in plan.batches for s in b.context_dependencies) if plan.clinical_plan else 0,
        "planned_request_characters": sum(u.request_characters for u in plan.clinical_plan.units if u.generation_eligibility=="eligible_draft") if plan.clinical_plan else sum(len(b.text) for b in plan.batches),
        "planned_requests": len(plan.batches),
        "requests_started": 0,
        "generated_candidates": 0,
        "candidate_count_complete": False,
        "structurally_valid_chunks": 0,
        "completed_units": 0,
        "units": [],
        "adaptive_ingestion": extraction.get("adaptive_ingestion", {}),
        "visual_recovery_outcome": extraction.get("visual_recovery_outcome"),
        "evidence_document_id": plan.evidence_document_id,
        "evidence_config_hash": plan.evidence_config_hash,
        "canonical_buffer_id": plan.buffer_id,
        "critical_evidence_warning_ids": plan.critical_warning_ids,
        "generation_units": [b.model_dump(mode="json", exclude={"text"}) for b in plan.batches],
        "clinical_plan_id": plan.clinical_plan.plan_id if plan.clinical_plan else None,
        # Internal span references and controlled labels only; no source quotes.
        # This accounting is distinct from legacy chunk/section completion.
        "document_concept_plan": plan.document_concept_plan.model_dump(mode="json") if plan.document_concept_plan else None,
        "blocked_source_units": sum(u.generation_eligibility == "blocked" for u in plan.clinical_plan.units) if plan.clinical_plan else 0,
        "reference_metadata_detected": reference_metadata.get("reference", ""),
        "generator_mode": generator_mode,
        "number_of_chunks_returned": 0,
        "validation_result": "",
        **multiview_metrics,
    }


def build_pipeline_failure_report(
    rule: str,
    reason: str,
    diagnostics: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a validation-style report for a failed pipeline stage."""
    normalized_rule = rule.strip().upper() or "PIPELINE_FAILED"
    # Exception strings can contain source passages, filenames or provider bodies.
    message = f"{normalized_rule}: details withheld; see sanitized diagnostic codes."
    return {
        "status": "FAILED",
        "summary": {
            "chunks": (diagnostics or {}).get("generated_candidates", 0),
            "generated_candidates": (diagnostics or {}).get("generated_candidates", 0),
            "candidate_count_complete": (diagnostics or {}).get("candidate_count_complete", False),
            "structurally_valid_chunks": (diagnostics or {}).get("structurally_valid_chunks", 0),
            "source_supported_chunks": 0,
            "clinically_approved": 0,
            "passed": 0,
            "warnings": 0,
            "errors": 1,
        },
        "issues": [
            {
                "severity": "ERROR",
                "rule": normalized_rule,
                "failure_code": (diagnostics or {}).get("failure_code", normalized_rule),
                "message": message,
            }
        ],
        "diagnostics": diagnostics or {},
    }


def build_generation_failure_report(
    reason: str, diagnostics: dict[str, Any] | None = None
) -> dict[str, Any]:
    """Build a validation-style report for failed LLM generation."""
    return build_pipeline_failure_report("GENERATION_FAILED", reason, diagnostics)


def build_retry_feedback(output_dir: Path) -> str:
    """Build source-safe retry guidance from the previous failed attempt."""
    report_path = output_dir / "validation_report.json"
    if not report_path.exists():
        return ""
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    status = str(report.get("status") or "").strip().upper()
    if status not in {"FAILED", "REVIEW_REQUIRED"}:
        return ""

    issues = report.get("issues") if isinstance(report.get("issues"), list) else []
    issue_lines: list[str] = []
    for issue in issues[:12]:
        if not isinstance(issue, dict):
            continue
        chunk = issue.get("chunk")
        chunk_label = f"chunk {chunk}" if isinstance(chunk, int) else "article"
        severity = str(issue.get("severity") or "ISSUE").strip().upper()
        rule = str(issue.get("rule") or "VALIDATION").strip().upper()
        message = str(issue.get("message") or "").strip()
        if message:
            issue_lines.append(f"- Previous {chunk_label}: {severity} {rule}: {message}")
    if not issue_lines:
        return ""

    return "\n".join(
        [
            "## Retry Feedback From Previous Local Validation",
            "The previous generation attempt for this same PDF did not fully pass deterministic validation.",
            "Regenerate the chunk package from the supplied source text only. Do not copy unsupported numbers, claims, references, or wording from the prior failed attempt unless they are explicitly present in the source text below.",
            "Correct these validation findings:",
            *issue_lines,
            "Keep the exact five-field chunk contract and preserve verified source reference metadata.",
        ]
    )


def should_skip_pdf(
    pdf_path: Path,
    output_dir: Path,
    force: bool,
    processed_dir: Path = PROCESSED_DIR,
    consider_processed: bool = True,
) -> bool:
    """Return True only for reusable output; processed/ is not proof of conversion."""
    del pdf_path, processed_dir, consider_processed
    if force:
        return False
    return output_dir_has_reusable_chunks(output_dir)


def output_dir_has_reusable_chunks(output_dir: Path) -> bool:
    """Return True when an output directory contains a non-failed chunk package."""
    chunks_path = output_dir / "chunks.json"
    validation_status = read_output_validation_status(output_dir)
    return chunks_path.exists() and validation_status not in {"", "FAILED", "UNKNOWN", "UNREADABLE"}


def find_reusable_output_for_source(output_dir: Path, source_doc: SourceDocument) -> Path | None:
    """Find a validated output package that belongs to the same PDF content."""
    candidates = [output_dir, content_hash_output_dir(output_dir, source_doc)]
    if source_doc.clinical_plan is not None:
        candidates.append(planned_output_dir(output_dir,source_doc))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if reusable_output_matches_source(candidate, source_doc):
            return candidate
    return None


def reusable_output_matches_source(output_dir: Path, source_doc: SourceDocument) -> bool:
    """Return True when saved chunks are valid and their source hash matches."""
    if source_doc.multiview_selection is not None or source_doc.source_package_plan is not None:
        return False  # Native multi-view packages remain explicitly incomplete.
    if not output_dir_has_reusable_chunks(output_dir):
        return False
    saved_hash = read_output_source_content_hash(output_dir)
    if not saved_hash or saved_hash!=source_doc.content_hash:return False
    if source_doc.clinical_plan is not None:
        from .evidence_storage import load_source_map
        try:
            saved=load_source_map(output_dir / "source_map.json")
            coverage=json.loads((output_dir / "coverage_report.json").read_text(encoding="utf-8"))
            prior=saved.clinical_plan
            return bool(saved.multiview_selection is None and prior and prior.plan_id==source_doc.clinical_plan.plan_id
                and not prior.document_warning_ids and not prior.blockers and prior.units
                and all(u.generation_eligibility=="eligible_draft" for u in prior.units)
                and coverage.get("source_coverage_complete") is True)
        except (OSError,ValueError):
            return False
    return True


def read_output_source_content_hash(output_dir: Path) -> str:
    """Read the saved source content hash for an output package."""
    source_map_path = output_dir / "source_map.json"
    if not source_map_path.exists():
        return ""
    try:
        payload = json.loads(source_map_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(payload.get("content_hash") or "").strip()


def select_output_dir_for_processing(output_dir: Path, source_doc: SourceDocument, force: bool) -> Path:
    """Choose an output directory without deleting valid output from different content."""
    if force or not output_dir.exists():
        return output_dir
    if source_doc.clinical_plan is not None or source_doc.source_package_plan is not None:
        if reusable_output_matches_source(output_dir,source_doc):return output_dir
        candidate=planned_output_dir(output_dir,source_doc)
        if not candidate.exists() or reusable_output_matches_source(candidate,source_doc):return candidate
        return _unique_path(candidate)
    if not output_dir_has_reusable_chunks(output_dir):
        return output_dir
    if reusable_output_matches_source(output_dir, source_doc):
        return output_dir

    candidate = content_hash_output_dir(output_dir, source_doc)
    if not candidate.exists() or not output_dir_has_reusable_chunks(candidate):
        return candidate
    if reusable_output_matches_source(candidate, source_doc):
        return candidate
    return _unique_path(candidate)


def select_failure_output_dir(output_dir: Path, force: bool) -> Path:
    """Choose where to write a failure report without clobbering valid output."""
    if force or not output_dir_has_reusable_chunks(output_dir):
        return output_dir
    return _unique_path(output_dir.with_name(f"{output_dir.name}-failure"))


def content_hash_output_dir(output_dir: Path, source_doc: SourceDocument) -> Path:
    """Return the deterministic collision-safe output directory for source content."""
    suffix = source_doc.content_hash[:8] or "unknown"
    return output_dir.with_name(f"{output_dir.name}-{suffix}")


def planned_output_dir(output_dir: Path, source_doc: SourceDocument) -> Path:
    """Version local review packages without migrating or overwriting history."""
    if source_doc.source_package_plan is not None:
        return output_dir.with_name(output_dir.name+"-draft-"+source_doc.source_package_plan.plan_id.rsplit(":",1)[-1][:12])
    if source_doc.clinical_plan is None:return content_hash_output_dir(output_dir,source_doc)
    plan_id = source_doc.multiview_selection.selection_id if source_doc.multiview_selection else source_doc.clinical_plan.plan_id
    fingerprint=plan_id.rsplit(":",1)[-1][:12]
    return output_dir.with_name(f"{output_dir.name}-plan-{fingerprint}")


def read_output_validation_status(output_dir: Path) -> str:
    """Read a saved validation status without exposing source text."""
    report_path = output_dir / "validation_report.json"
    if not report_path.exists():
        return ""
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "UNREADABLE"
    return str(payload.get("status") or "").strip().upper()

def move_to_processed(pdf_path: Path, processed_dir: Path) -> Path:
    """Move a source PDF to processed/ without overwriting an existing file."""
    processed_dir.mkdir(parents=True, exist_ok=True)
    target = processed_dir / pdf_path.name
    if target.exists():
        target = _unique_path(target)
    shutil.move(str(pdf_path), str(target))
    return target


def remove_output_dir(output_dir: Path) -> None:
    """Remove one existing output folder after verifying it is inside output/."""
    output_root = OUTPUT_DIR.resolve()
    target = output_dir.resolve()
    if output_root not in target.parents:
        raise ValueError(f"Refusing to remove path outside output/: {target}")
    shutil.rmtree(target)


def configure_logging() -> logging.Logger:
    """Configure console and file logging without printing article text."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gastroagi_chunk_pipeline")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def _unique_path(path: Path) -> Path:
    stem = path.stem
    suffix = path.suffix
    for counter in range(1, 1000):
        candidate = path.with_name(f"{stem}-{counter}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique processed path for {path}")


def _log_generation_diagnostics(
    diagnostics: dict[str, Any], logger: logging.Logger
) -> None:
    def number(key: str) -> int:
        value = diagnostics.get(key)
        return value if type(value) is int and value >= 0 else 0
    logger.info("Generation plan: source_characters=%s planned_units=%s logical_calls_started=%s",
        number("characters_extracted"), number("planned_requests"), number("requests_started"))


def _log_validation_issues(report: dict[str, Any], logger: logging.Logger) -> None:
    for issue in report.get("issues", []):
        severity = issue.get("severity", "WARNING")
        rule = telemetry.safe_code(issue.get("rule"))
        if severity == "ERROR":
            logger.error("Clinical validation rule=%s; see protected review report.", rule)
        else:
            logger.warning("Clinical validation rule=%s; see protected review report.", rule)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())




















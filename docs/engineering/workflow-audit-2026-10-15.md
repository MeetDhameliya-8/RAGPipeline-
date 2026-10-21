# Workflow Stability Audit

Date: 2026-10-15. Scope: local inspection, original local PDF extraction, isolated regression tests, and focused repairs.
No live Azure calls, production writes, source PDF changes, or database migrations were performed.
Clinical text and generated medical answers are intentionally omitted from this report.

## Observed Baseline

- Existing suite: 198 passed, 2 skipped, 9 passing subtests.
- Current corpus: 30 PDFs, 350 pages; all 30 extracted in 37.75 seconds, all had detected reference metadata.
- Extraction produced 10 warnings. Nonempty extraction and detected metadata do not establish complete clinical fidelity.
- Saved reports: 21 REVIEW_REQUIRED and 9 FAILED packages, containing 2 PASS, 168 REVIEW_REQUIRED, and 13 REJECTED chunk decisions.
- Local review DB: 170 drafts across 30 packages, all marked REVIEW_REQUIRED.
- Read-only SQLite quick_check: ok; no duplicate package/chunk-number slots or orphan embeddings.
- Independent comparison to the saved reviewable subsets: 170 expected and 170 stored, zero extra or missing IDs.
- No stored blank references or Unicode replacement characters were found.
- Coverage reports contain 281 deferred sections. The current corpus is not fully covered or clinically approved.

## Fresh Corpus Validation After Repairs

A read-only run re-extracted all 30 PDFs and validated their existing saved generated chunks with the current
validator and fresh page/section evidence. It took 50.52 seconds including extraction and validation.

| Measurement | Saved baseline | Fresh audit |
| --- | --- | --- |
| Numeric source-fidelity errors | 8 | 3 |
| FAILED packages | 9 | 6 |
| REVIEW_REQUIRED packages | 21 | 24 |
| Rejected chunk decisions | 13 | 8 |

Five numerical failures across three papers disappeared after choosing cleaner page extractions. All 30 retained
detected references and no replacement characters. The run emitted 46 diagnostics, including additional page
fallback notices; eight unresolved font-code occurrences remain. A subsequent warning-only change explicitly
reports these residual codes. More diagnostics here reflect transparent fallback reporting, not failed extraction.

These are audit results, not new saved approvals. Existing reports, generated packages, source PDFs, and the
live review database were left intact. Six packages still need source/clinical review, and coverage is unresolved.

## Module Status

| Module | Verified behavior | Remaining boundary |
| --- | --- | --- |
| PDF intake / local_admin_panel | File validation, safe filenames, and upload-only workflow covered by tests | Upload-only panel remains a supported offline entry point |
| pdf_extractor | Hybrid page selection, reference checks, empty/scanned-file rejection; all current PDFs extracted | OCR and figure/table interpretation are not implemented |
| providers/base, registry, azure | Azure-only selection, bounded transient retries, sanitized diagnostics, mocked transport tests | Live deployment compatibility and throughput were not tested |
| chunk_generator / prompt | Five content fields, alias normalization, reference binding, JSON repair, source-number checks | Generation still uses the whole article; generation-stage exceptions do not preserve every parsed candidate |
| chunk_validator | Schema, source numbers, contradictions, duplicates, references, and review gates tested | Heuristic checks do not prove every endpoint/arm/timeframe association |
| source_map | Source/config invalidation and PASS-only checkpoint completion tested | Section batches are not independent generation units; deferred sections remain unresolved |
| pipeline | Failure stages, review artifacts, safe subsets, source reuse, retry feedback tested | Multi-file artifact output is not an atomic package publication mechanism |
| local_chunk_store | Snapshot replacement, transactional rollback, idempotency, embedding reuse, strict validation states, exact readback tested | Local hashing retrieval is a prototype with lexical limits, not production semantic search |
| Clinical answer assembly | Intent/context, dated filtering, population mismatch, endpoint hierarchy, citation and claim checks tested | Missing approved guidelines and current dated literature limit completeness |
| local_rag_chat / queue API | Sequential execution, retries, stale recovery protection, verified-storage readiness, request-body bounds tested | Localhost review service; no production authentication/deployment contract |
| Chat renderer | Tables/lists, literal user text, clean answers, References panel, Copy/Regenerate/New Chat and XSS tests pass | Confidence remains bounded by clinical evidence and review status |
| admin_uploader | Five-field form population, field verification, and no automatic final Upload tested | Real backend/selectors and human approval remain separate acceptance gates |

## Defects Repaired

| Function | Previous behavior | Corrected behavior |
| --- | --- | --- |
| ingest_chunks_file | Changed content gained a new ID while old package content remained searchable | Atomically replaces only that package's obsolete chunks and embeddings |
| ingest_chunks_file | Repeated imports re-embedded and rewrote unchanged content/audit rows | Reuses unchanged content and matching embedding metadata |
| ingest_chunks_file / _load_pipeline_chunks | Unknown states were accepted; malformed rows silently skipped | Only PASS/REVIEW_REQUIRED states accepted; malformed replacements rejected before writes |
| verify_chunks_file_ingested | Checked only expected IDs | Also detects obsolete IDs in the package |
| eligible_chunk_numbers / subset and checkpoint callers | Missing statuses defaulted to PASS; document-wide errors did not block approval | One shared fail-closed eligibility rule handles missing/conflicting states and article errors |
| _validated_grounded_answer | Added a fabricated citation mapping to uncited output; valid IDs alone approved statistics | Requires explicit valid IDs and checks cited passages with existing clinical validation helpers |
| _hits_within_requested_window / _evidence_scope | Admitted future dates; filtered after answer selection | Applies bounded past-to-present dates before selection |
| _prepare_chat_turn | Every recency failure message described 30 days | Uses the actual requested window |
| _terminal_queue_status_for_result | Reported ready after a failed readback | Requires explicit successful readback |
| recover_stale_queue_jobs | Could requeue a long-running job owned by this process | Protects work while the local worker is running |
| _read_limited_body | Accepted negative length and could read until EOF | Rejects malformed/negative length before reading |
| extract_pdf | raw_full_text contained already-normalized page text | Preserves the selected extractor's raw page text |
| _page_text_quality_score | Ignored font-code artifacts attached to clinical numbers | Penalizes /H font codes and CID artifacts when choosing one extractor per page |
| extract_relevant_excerpt | Cut excerpts at a character boundary, potentially truncating statistics or qualifications | Keeps complete sentences, treating length as a soft limit |
| addTurn | Assistant formatting stripped numeric markers from user input too | Renders user messages literally and safely |

## Cleanup

- Removed the inactive OpenAI adapter and duplicate error/retry/transport helpers from chunk_generator.
- Removed an unused greeting classifier and obsolete inline-citation UI state/CSS.
- Shared the sentence-preserving excerpt helper between the legacy in-memory interface and SQLite retrieval.
- Retained the upload-only panel and in-memory retrieval helpers because they are existing tested interfaces.
- Kept the current Azure, local draft storage, ingestion, and five-field content contracts.
- Added no dependencies, agents, paid calls, integrations, or persistent schemas.
- Original code copies and checksums were retained in an external temporary audit baseline because this checkout has no Git history.

## Verification

Final command: `py -m pytest -q -rs -p no:cacheprovider`.

Result: **223 passed, 2 skipped, 9 subtests passed**.
The cache provider was disabled for this run because the existing local pytest cache directory denies writes.

New regression coverage includes package replacement, rollback, repeated-import embedding reuse, unknown validation
states, invalid replacement preservation, document-wide errors, future publication dates, cited unsupported
statistics, contradiction checks, rejection before streamed text release, readback readiness, malformed body lengths,
worker recovery, raw evidence retention, font-code extraction quality, complete statistical excerpts, unsupported
cure claims with resolving citations, literal user messages, and 50-package local storage/retrieval.

Two historical real-PDF test methods remain skipped because the original NEJM fixture set is absent.
The all-five method stops at its first missing fixture; two skips do not mean only two original PDFs are missing.
The current 30 PDFs are a separate corpus.

Relevant API behavior was checked against the Python 3.13 [SQLite documentation](https://docs.python.org/3.13/library/sqlite3.html)
and [regular-expression documentation](https://docs.python.org/3.13/library/re.html).

## Release Recommendation

Continue local review testing. Do not equate the passing software suite with clinical approval.
Restore the original five-PDF regression set; adjudicate the 13 rejected chunks and deferred coverage with original
articles and a clinician; add approved general references/guidelines where needed; then run an authorized Azure
end-to-end benchmark and production retrieval acceptance separately.

The existing retry feedback is bounded prompt guidance from previous local validation findings. It is not model
training or autonomous learning, and it does not guarantee that arbitrary new PDF layouts will succeed.

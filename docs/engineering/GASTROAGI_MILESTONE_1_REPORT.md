# GastroAGI Milestone 1 — Diagnostics, Failure Classification and Outcome Accounting

Date: 19 October 2026. Scope: Milestone 1 only, based on the current implementation and the inspection audit, especially sections 10–12 and 17–21.

The pipeline now emits a versioned, content-free `observability` envelope with monotonic stage timings, logical-call versus HTTP-attempt accounting, controlled failure classifications and explicitly scoped candidate outcomes. Existing clinical content, prompts, validation rules, source ownership, concurrency limits, retry policy, provider configuration and approval gates were preserved.

No live PDF ingestion, Azure connectivity check, paid inference, production write, queue resume or service restart was performed. All executed processing tests used synthetic sources, mocks and temporary runtime paths. The real queue remained paused.

## Files and functions changed

| File | Functions / changes |
|---|---|
| `src/diagnostics.py` — new | `StageTiming`, `ModelCall`, `DiagnosticSnapshot`; `Observation`, `bind`, `stage`, `timed`, `observed_run`; `model_call`, `http_attempt`, `failure`, `transport_metadata`, `correcting`, `count`, `attach`, `incomplete_candidates`. Context-local timing/request state, lock-protected shared observations and strict export allowlists |
| `src/pipeline.py` | `process_pdf`: observation scope, checkpoint outcomes, final candidate decisions and sanitized failures. `save_outputs`, `save_failure_report`: persistence timing and pre-write snapshots. `build_pipeline_failure_report`: withheld exception contents. `_log_generation_diagnostics`, `_log_validation_issues` and processing logs: controlled metrics/codes instead of source filenames, free-text issues or exception tracebacks |
| `src/chunk_generator.py` | `generate_chunks`: generation scope, planned units/packages, merges/duplicates. `_generate_single_batch`: logical provider calls, raw candidate counts, safe transport metadata and original-code/category classification. Existing parsing/schema/content/reference raises now carry stable `reason_code` metadata; conditions/messages/codes governing decisions are unchanged |
| `src/package_generation.py` | `generate_source_packages`, worker `attempt`/`observed_attempt`, `assess`: explicit worker binding, cached/resumed/held outcomes, assessment/correction counts, local validation timing and merge duplicates. The existing groups of at most two workers and scheduling barrier remain |
| `src/providers/azure.py` | `_create_chat_completion`: observation immediately around actual SDK create attempts, failures, controlled HTTP/provider codes, attempt timing and retry-backoff timing. `_log_token_usage` and retry logs omit deployment/error/header strings. Request payload, retry eligibility/delays and SDK settings are unchanged |
| `src/visual_source.py` | `recover_visual_source`, `read_pass`/`observed_pass`: recovery/pass timing, explicit worker binding, cached-pass counts, logical transcription accounting and classified failures. Same transcription prompt, two-pass checks and concurrency |
| `src/local_rag_chat.py` | `process_uploaded_pdf`: end-to-end observation scope through storage. `store_generated_chunks_for_result`: verified final stored-row count. `_process_queue_worker`: retains sanitized classification/completed exception observations in unexpected failure results |
| `src/local_chunk_store.py` | `ingest_chunks_file`, `verify_chunks_file_ingested`: stage scopes inside existing function bodies, preserving captured default arguments and transaction/readback decisions |
| `src/pdf_extractor.py` | `extract_pdf`: native extraction timing; extraction behavior unchanged |
| `src/evidence.py` | `build_evidence_document`: nested evidence construction timing |
| `src/source_map.py` | `build_source_map`: source mapping timing |
| `src/native_layout.py` | `recover_native_layout`: native layout timing |
| `src/adaptive_ingestion.py` | `build_ingestion_plan`: clinical/source-package planning timing |
| `src/preparation_cache.py` | `load_preparation`: checkpoint lookup plus verification timing |
| `tests/test_milestone1_diagnostics.py` — new | 19 test functions expanding to 22 cases for the new contracts |
| `tests/conftest.py` | Extend existing temporary runtime redirection to pipeline outputs, processed paths and logs; prohibit Azure SDK clients without an explicitly injected fake client |
| `tests/test_pipeline_helpers.py` | Update one error-message assertion to verify redacted output-persistence failures instead of requiring the raw OS error text |

`src/queue_recovery.py` was inspected and left unchanged. No dependency, prompt, clinical validator or persistent database schema changed. No Git repository was present; no repository was initialized.

## Exact diagnostic fields

### Envelope and timing

The additive envelope is `diagnostics.observability` for generator/report diagnostics and `result.observability` for orchestration results. Unexpected observed exceptions also carry `exc.observability`, allowing queue failure results to retain the snapshot without retaining the error body. Nothing is added to any of the five chunk content fields.

Top-level fields:

- `version`: `milestone1-v1`.
- `run_complete`: false in an in-progress write snapshot; true after the outer observed call finishes, including failure.
- `wall_seconds`: elapsed monotonic time for the observation scope.
- `timing_semantics`: declares that exclusive timings are per execution context and concurrent work seconds are not wall time.
- `stages`, `model_calls`, `failures`, `counts`, `request_totals`.
- `candidate_accounting_complete`: whether currently observed candidate events have known dispositions, with parse/request uncertainty retained. This is not PDF coverage or clinical approval.

Each `stages.<name>` contains `calls`, `failures`, `inclusive_seconds`, `exclusive_seconds`, `samples_seconds`. Samples are individual invocation durations in completion order, including individual transcription passes. They contain no source/page text or filename.

Stage names:

| Name | Boundary |
|---|---|
| `checkpoint_lookup` | Preparation lookup, decoding/checksum verification and existing evidence/plan verification |
| `native_extraction` | Native PDF extraction, excluding nested measured evidence/planning work in its exclusive time |
| `evidence_construction` | Initial source evidence graph construction |
| `source_mapping` | Section/evidence source-map construction |
| `native_layout` | Native geometry/order recovery |
| `planning` | Existing clinical/source-package/adaptive planning, including repeated invocations |
| `visual_recovery` | Visual-recovery envelope, including page preparation/cache work |
| `transcription_pass` | Each cached or newly generated transcription pass |
| `transcription_request` | Each logical uncached visual provider invocation |
| `generation` | Chunk generation envelope, including existing scheduling/merge work |
| `llm_request` | Each logical chunk-writing or corrective provider invocation |
| `http_attempt` | Each observed SDK HTTP create call, successful or failed |
| `retry_backoff` | Existing bounded transport retry sleep; policy unchanged |
| `local_chunk_validation` | Per-package actual assessment, including assessment after correction |
| `correction` | Existing single corrective generation, with nested request time excluded from exclusive time |
| `final_document_validation` | Final document-level `validate_chunks` call |
| `output_persistence` | Successful output bundle or failure-report persistence |
| `sqlite_storage`, `sqlite_readback` | Existing local transaction/upsert and readback functions |

Nested same-context child elapsed time is subtracted from its parent exclusive duration. Fake-clock tests verify this, including HTTP attempts versus backoff. Worker contexts explicitly reset timing stacks and share only the locked accumulator. Parallel pass/request sums can exceed wall time. A generation/visual envelope and its concurrent worker work must not be summed as a serial end-to-end duration. `wall_seconds` is the elapsed run measure; inclusive stage values are for attribution, not addition.

The saved `validation_report.json` contains a truthful **in-progress** snapshot taken immediately before its write. It cannot contain its own completed persistence time or later SQLite work. The completed returned result contains these timings; the normal Admin queue persists that result in its existing result JSON. The CLI caller also receives the completed result through `process_pdf`; the CLI does not newly rewrite a finished report to append post-write timings.

### Request and token accounting

Each `model_calls[]` item has exactly these diagnostic fields:

`call_id`, `kind`, `status`, `seconds`, `http_attempts`, `http_attempts_complete`, `http_successes`, `http_failures`, `prompt_tokens`, `completion_tokens`, `usage_observations`, `unknown_usage_attempts`, `http_statuses`, `provider_error_codes`.

- `kind` distinguishes `generation`, `correction`, `visual_transcription`.
- `call_id` is an observation-local opaque sequence; concurrent assignment order need not equal source order. Existing ordered output merging is unchanged.
- `status` is started/succeeded/failed at the logical provider boundary. A completed provider response can still fail subsequent JSON/schema/source validation.
- `http_attempts=null` means HTTP attempts were not observed, as for a custom/mock provider without the Azure transport hook. It does not mean zero attempts or one attempt.
- Per-call `http_attempts_complete` distinguishes an observed transport outcome from an uncertain local SDK rejection. A successful SDK return or HTTP/timeout/connection failure is observed; arbitrary local SDK errors are not promoted to proven HTTP attempts. The `http_attempt` stage measures SDK dispatch duration, including local rejection time; timing its invocation alone does not prove a network send.
- `http_statuses` retains only bounded integer HTTP codes; unknown statuses are null. Successful SDK returns do not invent a numeric 200 code.
- `provider_error_codes` contains only known transport error labels, such as `rate_limit_exceeded`; arbitrary error bodies/headers/identifiers are excluded.
- Available prompt/completion counts are preserved independently. Missing values and failed attempts increment `unknown_usage_attempts`; missing usage is never reported as a known zero.

`request_totals` fields:

`logical_calls`, `successful_responses`, `failed_calls`, `correction_calls`, `generation_calls`, `transcription_calls`, `http_attempts_observed`, `http_attempts_complete`, `calls_with_unknown_http_attempts`, `http_successes`, `http_failures`, `prompt_tokens_observed`, `completion_tokens_observed`, `unknown_usage_attempts`, `token_usage_complete`.

`generation_calls` includes corrective calls. All logical call kinds contribute to `logical_calls`. A three-attempt timeout is one logical failed call and three observed failed HTTP attempts. A retry followed by success is one successful logical response with two HTTP attempts and incomplete overall token accounting if the failed attempt's usage is absent. Token completeness requires observed attempt counts and complete usage for all those attempts; a custom response's usage alone cannot establish a complete transport/billing total.

Existing `requests_started`, `generated_candidates`, per-unit audit fields and retry-consumed codes remain available with their prior semantics. New code does not reinterpret logical calls as HTTP calls. Legacy `units[].response` now whitelists numeric `transport_attempts`, numeric/null usage and controlled `finish_reason`; arbitrary metadata, response identifiers and configuration fingerprints are not copied there.

### Candidate, cache and storage counters

Counters describe this observation, not project lifetime. A missing counter means that event/boundary was not observed; consumers must not infer an unmeasured final stage completed with zero rows.

| Fields in `counts` | Definition |
|---|---|
| `planned_source_units`, `planned_source_packages` | Existing scheduled units/packages; not a promise every unit causes a new model call |
| `preparation_cache_hits`, `preparation_cache_misses`, `visual_preparation_cache_hits` | Existing preparation reuse/miss outcomes |
| `cached_transcription_passes` | Saved pass read without a provider call |
| `generated_candidates` | Raw chunk-list entries observed in new parseable responses, including correction attempts |
| `resumed_candidate_units`, `resumed_candidates` | Compatible saved candidate units and their loaded chunk entries, separate from newly generated entries |
| `cached_source_hold_units`, `cached_validation_hold_units` | Compatible permanent held-unit checkpoints reused without generation |
| `source_held_units`, `source_validation_held_units` | Newly returned typed source abstentions; newly held package validation failures after the existing correction policy |
| `schema_rejected_candidates`, `reference_or_content_rejected_candidates` | Known raw candidates rejected before clinical assessment |
| `assessed_candidate_events`, `accepted_candidate_events`, `rejected_candidate_events` | Every actual assessment disposition; accepted means PASS or REVIEW_REQUIRED, never clinically approved |
| `duplicate_candidates` | Entries discarded by the existing exact content merge/deduplication decision |
| `unassessed_duplicate_candidates` | Normal/clinical-path duplicates removed before final assessment, enabling an honest event partition |
| `merged_candidates` | Surviving merged candidate entries |
| `unclassified_candidate_events` | Observed new plus resumed candidates without a known assessment/prevalidation/duplicate disposition |
| `final_document_chunks`, `final_reviewable_chunks`, `final_rejected_chunks` | Final report's chunk count, PASS/REVIEW_REQUIRED eligibility, REJECTED eligibility |
| `saved_review_drafts` | Reviewable entries whose review JSON write completed |
| `final_stored_rows` | Verified matching rows for the current package/readback, not the total global SQLite row count |
| `storage_readback_unverified` | A readback was not verified; no verified stored total is invented |

Assessment counters are events, not unique clinical facts. An initial rejected candidate followed by one accepted correction produces two generated candidates, two assessments, one rejection and one acceptance. Four accepted source representations merged into one chunk produce four acceptance events, three duplicates and one merged candidate. Duplicates already assessed in package mode are not counted again as additional rejected candidates.

The event partition is new candidates plus resumed candidates = assessed events + prevalidation rejections + unassessed duplicates, when the observations are complete. Unknown/unparseable/truncated/request-failed results keep completeness false. Failure before final assessment can leave known candidates unclassified. Cached source holds have no new candidate events. A complete event partition says nothing about unattempted packages, missing PDF coverage or medical truth.

## Failure classification and privacy

`failure_classification` preserves `original_code` separately from high-level `category`. Controlled optional fields are `reason_code`, `exception_type`, `errno` (a numeric string). Existing external `.code` categories such as SCHEMA_INVALID or REFERENCE_OR_CONTENT_INVALID still drive the same retry/queue policy. No exception message is copied into the new classification.

Stable `reason_code` values were added at the existing deterministic raises, without changing their conditions:

`JSON_NOT_STRING`, `JSON_EMPTY`, `JSON_PARSE_ERROR`, `JSON_NOT_OBJECT`, `JSON_DUPLICATE_KEYS`, `RAW_SCHEMA_INVALID`, `CHUNK_COUNT_MISMATCH`, `DUPLICATE_CHUNK_NUMBER`, `ARTICLE_NOT_OBJECT`, `CHUNKS_NOT_LIST`, `NO_CLINICAL_CHUNKS`, `CHUNK_NOT_OBJECT`, `EXTRA_CONTENT_FIELD`, `REQUIRED_FIELD_EMPTY`, `REFERENCE_BINDING_UNAVAILABLE`, `REFERENCE_IDENTITY_CONFLICT`, `REFERENCE_DOI_NOT_IN_SOURCE`, `REFERENCE_DOI_CONFLICT`, `REFERENCE_DOI_UNBOUND`, `REFERENCE_DOI_AMBIGUOUS`, `REFERENCE_BIBLIOGRAPHY_UNBOUND`, `REFERENCE_CITATION_UNVERIFIED`, `REFERENCE_CITATION_COMPONENT_MISMATCH`, `REFERENCE_NOT_TRACEABLE`.

For example, a generic original generation exception can now be classified as category REFERENCE_OR_CONTENT_INVALID with reason REFERENCE_DOI_NOT_IN_SOURCE, without exporting the missing DOI, passage or filename. Source-validation reports still retain their existing rule lists; unit failure records also retain the sanitized classification through the package and queue boundaries.

Unknown arbitrary exception/provider codes are exported as UNCLASSIFIED_ERROR rather than copied verbatim. Known exception types and errno/HTTP metadata help distinguish operational failures without free-text messages. `failures[]` records boundary observations; one exception can be observed at several layers. Its length is not a count of unique failed requests. Use `model_calls`/`failed_calls` for that count.

Privacy changes intentionally affect diagnostic presentation: pipeline logs omit filenames, paths, free-text validation messages and exception tracebacks; retry logs omit raw error/header identifiers; failure report messages and returned processing reasons use fixed stage summaries. The raw `disk full` assertion in one prior test was replaced by assertions for a redacted persistence failure. Protected clinical output and review content are still stored only through the existing output paths; no clinical text is added to the observability envelope.

## Tests executed and actual outcomes

Before execution, test fixtures and live-capable entrypoints were inspected. Commands explicitly selected `tests/`, excluding the root `test_azure_connection.py` diagnostic. An autouse test guard rejects a real Azure client unless the test supplies a fake client factory. Existing fixture redirection was extended so pipeline logs/outputs and captured local-store defaults point to temporary paths. Tests that start local HTTP/browser fixtures operate against temporary local state; no real backend was started or contacted. Existing diagnostic tests inject fake transport objects.

All commands below used `python -B -m pytest`, `-q`, `-p no:cacheprovider` and separate `tmp/milestone1/` base directories. No test cache or bytecode writes were required in source/runtime directories.

| Execution | Actual result |
|---|---|
| Initial focused collection, `tests/test_milestone1_diagnostics.py`, base `focused` | Collection failed with one SyntaxError introduced while editing two single-line exception/logging statements; zero tests executed. Corrected before subsequent execution |
| Focused run, base `focused2` | 16 passed in 3.00 s |
| Preliminary full `tests/` run, base `offline-suite` | Started and progress captured, but final process output was not retained. Outcome not claimed; superseded by recorded full runs |
| Focused run, base `focused3`, `focused-results.xml` | 18 passed in 2.28 s |
| Recorded full `tests/` run, base `offline-suite2`, `offline-results.xml` | 550 passed, 7 skipped, 9 subtests passed in 39.66 s |
| Focused compatibility run: diagnostics, generator, pipeline helpers, P0 accounting, ingestion latency and queue recovery; base `recheck`, `recheck-results.xml` | 113 passed, 9 subtests passed in 9.57 s |
| Recorded full `tests/` run, base `final-offline`, `final-offline-results.xml` | 552 passed, 7 skipped, 9 subtests passed in 40.21 s |
| Post-change persistence run: diagnostics, pipeline helpers, evidence efficiency; base `persistence-final`, `persistence-final-results.xml` | 63 passed, 5 subtests passed in 3.00 s |
| Post-change run: diagnostics, generator, pipeline helpers, evidence efficiency; base `final-verification`, `final-verification-results.xml` | 97 passed, 9 subtests passed in 3.53 s; zero failures/skips |
| HTTP-boundary refinement run of those same four modules; base `http-boundary-final`, `http-boundary-final-results.xml` | **98 passed, 9 subtests passed in 3.44 s; zero failures/skips** |
| Final unknown-usage accounting check: diagnostics and generator modules; base `unknown-usage-final`, `unknown-usage-final-results.xml` | **55 passed, 4 subtests passed in 2.60 s; zero failures/skips** |

The HTTP-boundary and final unknown-usage runs include all **22 new diagnostic cases**. They cover the HTTP/backoff timing, token completeness, failure-persistence and local-SDK-rejection refinements made after the latest full suite. The last full-suite result is not represented as a full run of those subsequent refinements. JUnit counts include successful subtest records; terminal pass/subtest numbers above preserve pytest's distinction. Static AST parsing and the synthetic example below were additional diagnostics, not extra test passes.

The seven full-suite skips were missing runtime-source fixtures: two parameterized protected multiview anchors, three real native-layout source tests, and two TASK11 real-source regressions. They are not counted as passes or as newly verified clinical source fidelity.

### Acceptance evidence

| Required case | Evidence in `tests/test_milestone1_diagnostics.py` |
|---|---|
| Successful processing diagnostics | `test_synthetic_pipeline_timings_and_storage_preserve_decisions`: synthetic PDF, unchanged review decision/content, all required stage boundaries, temporary SQLite storage/readback and completed result snapshot |
| Provider timeout/failed request | Timeout test: one logical failure, three actual mocked SDK attempts, unknown token usage; retry-success test retains partial token totals |
| Cached/resumed zero calls | Resumed candidate content equality; permanent validation hold reuse; visual pass reuse with provider configured to fail if called |
| Source-validation failure | Two rejected assessments under existing initial/correction policy, typed source hold and no paid retry on compatible hold reuse |
| Partially accepted candidates | Two generated entries, one accepted/one rejected, one merged result and no corrective call |
| Corrective regeneration | Initial rejection remains visible, one corrective call, one acceptance and two assessments |
| Concurrent updates | Existing two-worker package execution, unique observation IDs, unchanged duplicate merging; 200 concurrent count updates without loss |
| Confidential data redaction | Sensitive exception/filename/metadata sentinels excluded from exported observations/logs/failure report; provider labels/statuses allowlisted |
| Unchanged prior validation decisions | Direct actual validator report equality with/without timing scope; existing source/citation/numeric/queue/storage tests passed |
| Monotonic/nested time | Fake clock: parent exclusive excludes nested work; two HTTP attempts plus retry sleep partition correctly; failed stages remain measured |

## Sanitized synthetic example

This example uses a fake clock, a fake provider and synthetic transport notifications. **No HTTP request occurred.** The fixture advances the clock by two seconds and returns numeric usage only. Full synthetic output is in ignored `tmp/milestone1/synthetic-observability.json`; it is not a medical output or real benchmark.

```json
{
  "version": "milestone1-v1",
  "run_complete": true,
  "wall_seconds": 2.0,
  "stages": {
    "llm_request": {
      "calls": 1,
      "failures": 0,
      "inclusive_seconds": 2.0,
      "exclusive_seconds": 2.0,
      "samples_seconds": [2.0]
    }
  },
  "request_totals": {
    "logical_calls": 1,
    "successful_responses": 1,
    "failed_calls": 0,
    "correction_calls": 0,
    "generation_calls": 1,
    "transcription_calls": 0,
    "http_attempts_observed": 1,
    "http_attempts_complete": true,
    "calls_with_unknown_http_attempts": 0,
    "http_successes": 1,
    "http_failures": 0,
    "prompt_tokens_observed": 10,
    "completion_tokens_observed": 4,
    "unknown_usage_attempts": 0,
    "token_usage_complete": true
  },
  "counts": {
    "planned_source_packages": 1,
    "generated_candidates": 1,
    "assessed_candidate_events": 1,
    "accepted_candidate_events": 1,
    "merged_candidates": 1,
    "unclassified_candidate_events": 0
  },
  "candidate_accounting_complete": true
}
```

This is a selected view of the envelope; the full file also contains the content-free model call record, failure list and timing semantics. No source filename, source passage, generated clinical text, endpoint, credential or raw error appears.

## Backward compatibility and remaining gaps

- The five clinical fields, optional pipeline numbering, prompts, word thresholds, citation/source rules, ownership, concurrency two, transport retry settings, queue recovery policy and approval/production gates are unchanged. Existing clinical validators and `queue_recovery.py` were not edited.
- Return/report diagnostics gain an additive envelope. Raw error messages in pipeline failure summaries/reasons and untrusted unit response metadata are deliberately redacted; consumers parsing those presentation strings must use classifications instead.
- Existing counters retain their meanings. New event counters distinguish corrections, reused candidates and filtered siblings; they do not retrospectively repair historical totals.
- Current preparation/hold fingerprint policies hash implementation modules. Editing instrumentation can cause existing caches/holds to be considered incompatible on a future authorized run. That policy was not weakened or bypassed, and no existing cache/checkpoint was rewritten or tested against live processing.
- A saved report's in-progress snapshot excludes its own completed write and later storage. Completed pipeline/Admin results carry final timings; no historical report is rewritten for telemetry. Recording the final snapshot in a separate durable CLI artifact remains a possible future presentation improvement, not a missing measured boundary.
- Custom providers without transport hooks leave HTTP attempts and overall token completeness unknown. Azure/provider billing, failures that return no usage, service queueing and token decoding cannot be reconstructed locally. There was no live verification or benchmark.
- Streaming chatbot requests and unrelated diagnostic/production service activity are outside this PDF-run observation scope. This is not a global account billing ledger.
- Source proof verification inside checkpoint loading is included in `checkpoint_lookup`; every low-level span/hash operation does not receive an independent timer. Native library internals remain part of native extraction. Concurrent work is not an additive wall-time ledger.
- Direct generator calls that stop before clinical assessment can leave candidate dispositions incomplete, explicitly flagged. Original unknown/custom exception codes are withheld rather than exported. Failure boundary observations can repeat one underlying exception.
- Seven real-source tests remain skipped for unavailable fixtures. Clinical approval, whole-document coverage, production ingestion and ChromaDB integration remain outside this milestone. No clinical accuracy or speed guarantee is claimed.

## Protected state and final assessment

A before/after SHA-256 manifest covered **2,185 files** in input PDFs, processed PDFs, existing outputs/checkpoints, local databases and logs, plus `.env` and prompt files. Verification found **zero changed/missing files and zero new files in protected runtime directories**. The manifest and test artifacts are isolated under `tmp/milestone1/`.

Read-only immutable SQLite inspection at **19 October 2026, 14:18:40 IST / 08:48:40 UTC** still showed paused=1, eleven LOCAL_REVIEW_READY jobs, nineteen QUEUED jobs, 227 REVIEW_REQUIRED chunk rows and two SOURCE_REVIEW_REQUIRED rows. No queue or real database mutation was performed. All current source modules parsed successfully.

The nine requested test categories and timing/accounting/privacy compatibility checks passed in safe offline tests. Remaining uncertainty is explicitly represented instead of inferred. Work stopped after Milestone 1; no Milestone 2 optimization, paid benchmark or queue continuation was started.

Implementation references checked: Python 3.13 [context-local state](https://docs.python.org/3.13/library/contextvars.html), [monotonic performance timing](https://docs.python.org/3.13/library/time.html#time.perf_counter), and [wrapper metadata](https://docs.python.org/3.13/library/functools.html#functools.wraps). Installed-runtime behavior was verified through the recorded offline tests.

**PASS — Milestone 1 acceptance criteria satisfied by the recorded full-suite evidence and final post-change verification.**

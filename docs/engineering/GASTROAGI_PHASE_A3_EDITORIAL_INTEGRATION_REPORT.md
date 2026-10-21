# GastroAGI Phase A3 — Proposal-aware editorial integration

Date: 21 October 2026. Scope: Phase A3 only. **Status: PASS for the opt-in offline engineering acceptance criteria.** No qualified clinician assessed these outputs. Milestones 4 and 5, and Phase A1, remain **PARTIAL**. This result establishes neither clinical approval nor production readiness.

## 1. Current integration gap diagnosis

A2 emits immutable proposals with unresolved literal role candidates; it intentionally grants no generation eligibility or human source-review attestation. The existing `src/editorial_generation.py` route requires reviewed questions. Passing automatic proposals through fabricated review records would bypass that boundary. The legacy source-package orchestration also has extractive correction and ownership assumptions that cannot simply be applied to editorial paraphrases.

The new explicitly invoked API adapts the proposal contract, reuses the existing scheduler, parsers, editorial validators, independent consistency assessment, diagnostics and local storage functions, and keeps each accepted candidate a mandatory-review draft. No normal pipeline, upload, queue, UI or production route imports or activates it.

The audit, Milestone 1–5 reports, A1/A2 reports, 6B design, relevant current implementation and test isolation were inspected before implementation. A protected snapshot and baseline tests were recorded before edits. Git was not assumed or initialized.

## 2. Exact new and changed files

All paths below are under `D:\gastroagi-chunk-pipeline\`. **Existing protected files modified: none.**

| New file | Functions/contracts and purpose |
| --- | --- |
| `proposal_editorial/__init__.py` | Explicit API exports; no startup behavior |
| `proposal_editorial/models.py` | `WorkflowConfig`, `LiteralScope`, `ProposalDraftEligibility`, `UnitResult`, `ReviewDraft`, `WorkflowResult`, sanitized errors |
| `proposal_editorial/eligibility.py` | `assess_eligibility`: conservative proposal classification and budgets |
| `proposal_editorial/requests.py` | `provider_identity`, `generation_contract`, `validation_policy`, `build_request`, `editorial_prompt` |
| `proposal_editorial/checkpoints.py` | `check_path_budget`, `workspace_lock`, `atomic_json`, `Checkpoints`: immutable responses, request intents, policy decisions, sanitized run reports |
| `proposal_editorial/validation.py` | `assess_response`, `_safe_report`, `Assessment`: existing technical/editorial checks plus independent source consistency |
| `proposal_editorial/storage.py` | `check_target`, `save_review`, `_store_and_verify`, `quarantine_owned`: existing SQLite adapter and independent readback |
| `proposal_editorial/workflow.py` | `run_proposal_editorial`, `_run_proposal_editorial`: explicit orchestration, rolling generation, ordered persistence and error accounting |
| `tests/editorial_integration_support.py` | Fictional prepared sources and a thread-safe explicit fake provider |
| `tests/test_phase_a3_editorial_integration.py` | Eligibility, fidelity, concurrency, recovery, diagnostics and approval-boundary tests |
| `tests/test_phase_a3_recovery_storage.py` | Real subprocess exits, SQLite isolation/readback, protected holds and synthetic PDF preparation |
| `tools/evaluate_phase_a3.py` | Protected baseline comparison and isolated group/rolling benchmark |
| `docs/engineering/GASTROAGI_PHASE_A3_EDITORIAL_INTEGRATION_REPORT.md` | This report |

No dependencies were added. No existing function signatures, source hashes, checkpoint formats, schemas, prompts, thresholds, provider settings or runtime defaults changed.

## 3. Proposal eligibility contract

`ProposalDraftEligibility` is an internal immutable Pydantic record, distinct from `ReviewedQuestion` and `SourceReview`. Its exact fields are `proposal_id`, `source_order`, `state`, `reasons`, `ownership`, `context`, `scope`, `clinical_question`, `document_category`, `human_source_reviewed=False`, `clinically_approved=False`.

| State | Meaning |
| --- | --- |
| `draftable_review` | A bounded, self-contained source selection can produce a draft for mandatory review |
| `relationship_review` | An essential context, population/intervention, outcome or attribution relationship is unresolved |
| `blocked_source` | A2/source evidence already carries a blocking hold |
| `insufficient_evidence` | No independent supporting source selection is available |
| `duplicate_deferred` | Exact compatible proposal repetition or an explicit generation/evidence budget deferral |

The initial automatic drafting subset is deliberately narrow: one canonical owner, one literal population descriptor and one literal treatment/procedure descriptor, descriptive/selection content without association hazards, or one explicit recommendation grammar containing its strength and certainty. Non-time numerical associations, outcome/comparator associations, safety relationships, unbound context and cross-page relationships remain held. Source/context overflow is never resolved by truncating evidence.

This syntax gate is **not proof of a clinical relationship**. It authorizes only unreviewed drafting. All A2 unresolved flags survive in the request; they are not rewritten into reviewed relationships. `LiteralScope.basis` is always `literal_consistency_only`. The existing `BoundRole` is used only as a role/span argument to the independent consistency checker, never to construct a reviewed question or approval.

`WorkflowConfig` defaults: `concurrency=2` (allowed 1–2), `max_requests=80`, `max_unit_characters=20000`, `retry_uncertain_requests=False`. These are new opt-in limits, not changes to production defaults.

## 4. Proposal-aware editorial generation design

Entry point: `proposal_editorial.run_proposal_editorial(extraction, source_doc, inventory, *, provider, workspace, db_path, diagnostics, proposal_plan=None, config=None, control=None, progress=None)`.

It consumes already prepared source objects. It does not extract a PDF, select a provider from the environment, initialize a queue, or start a service. An explicit provider and local review DB path are required. An optional A2 plan is rebuilt and validated against the current source before use; otherwise A2 generates the plan locally.

Each request contains the question, complete selected owner text, exact buffer offsets, reference authority, page/section identities, document category, candidate label references, literal scope anchors, unresolved flags, concern IDs, limitations and stable identities. It uses an opaque `source-<PDF hash prefix>.pdf` alias. Unselected article text and the sensitive original filename are omitted from the model request and operational audit.

`prompts/gastroagi_editorial_rules.md` is read unchanged. Each independent question requests one candidate under the existing `article`/`chunks` envelope. The existing five content fields remain `topic`, `subtopic`, `section`, `reference`, `original_text`; `chunk_number` remains metadata. No new provenance fields enter clinical content. Existing normalization and optional local JSON repair are reused; clinical prose is not rewritten after generation. Normal 100–200 word guidance and existing short/long review findings remain unchanged.

Results merge in source order as **independent one-chunk envelopes**, numbered 1 within their individual local packages. Their `source_order` is internal metadata. A3 does not create an artificial combined article envelope whose unrelated topics could bypass the existing article-wide consistency checks. Document chunk quantity follows eligible independent proposals, not a four-to-eight cap.

## 5. Evidence/source ownership guarantees

`pin_inputs` reuses A2's validated deep-copy adapter and existing evidence/inventory/package/clinical-plan checks. The prepared full text and filename must match the canonical source; evidence must have `pdf_bytes` identity quality. The existing `require_source_reference` remains authoritative.

Source, inventory, extraction reference/body and supplied proposal identities are rechecked before work and after a returned response, and again before persistence. The full source-map hash additionally binds auxiliary warnings and planning holds omitted from the narrower M3 fingerprint. A mid-flight newly added source hold prevents storage while retaining the returned raw candidate.

Owner/context spans retain canonical buffer identity and offsets. A2 retains page/section, concern and unresolved-relationship records; requests include those references. Immutable local `source_link.json` binds the full PDF/evidence/proposal identity and selected spans. Independent claim assessment receives a separate evidence copy per worker, protecting mutable private indexes.

## 6. Generation scheduling and concurrency results

The workflow directly reuses `src.bounded_scheduler.run_bounded` and `SubmissionControl`. In-flight submissions are limited to the current one/two-worker window. A deterministic event-based test holds request 1 until request 3 starts, proving refill before an unrelated slow request completes. Ordered draft output remains source/proposal order despite out-of-order completion.

Auth/quota failures stop new submissions through the existing control gate. Already started work drains, successful responses remain checkpointed and valid drafts are saved. Tests for both fatal codes made exactly two calls for three units, retained the independent successful draft and left the third unit unsubmitted.

Unexpected worker or progress exceptions are recorded and propagated through a sanitized `WorkflowError` carrying accounted results. Keyboard interruption drains safe in-flight work, persists valid results and raises `WorkflowInterrupted` with its result. Pre-submission cancellation makes zero calls. No thread-killing or document-wide speculative submission was introduced.

## 7. Checkpoint/resumption design

All artifacts are in an explicitly selected, marked A3 workspace. An unmarked nonempty directory is rejected. A nonblocking OS file lock excludes concurrent writers and releases after process exit. Windows path budgets are checked before generation instead of depending on optional machine long-path configuration.

Candidate keys bind full PDF/evidence identity, A2 plan/proposal identity, selected owner/context/scope/question, reference, actual request, prompt, provider configuration identity and generation implementation contract. The contract hashes `requests.py`, `eligibility.py`, `models.py`, `workflow.py` plus its version. Azure identity includes endpoint/deployment/API version/timeout/retries and provider implementation hash, excluding credentials. Custom injected providers must supply an explicit `generation_identity` mapping.

Workspace artifacts:

- `workspace.json`, `workflow.lock`: dedicated workspace identity and writer exclusion.
- `units/<candidate key>/request-<attempt>.json`, `request-intent.json`: immutable attempt history and current request commitment.
- `response.json`: immutable raw response, full key, raw SHA-256 and whitelisted transport metadata, published before parsing/validation.
- `last-failure.json`: sanitized known transport/source failure for the latest attempt.
- `validation-<policy>.json`: separate, recomputed validation decision, raw hash and full policy identity.
- `local/a3_<candidate key>/{chunks.json,source_link.json,validation_report.json}`: accepted local review package and internal source linkage.
- `runs/<opaque run ID>.json`: completed sanitized M1 diagnostics, including held reasons and per-worker audit/storage results.

Temporary files are flushed/fsynced; immutable publication uses exclusive `os.link`, and mutable records use atomic `os.replace`. Existing records with incompatible content or damaged response hashes fail closed; they do not authorize another model call.

The validation policy separately hashes the existing validation fingerprint and `validation.py`. Every resumed raw candidate is revalidated. Policy changes reconsider prior holds without regenerating compatible responses. Prompt/provider changes create different candidate keys. Tests prove zero-call successful resume, zero-call reconsideration of a held decision, and stricter-policy exclusion of a previously stored A3 draft.

Known transient failures can be retried on a subsequent **explicit invocation**; compatible successes are resumed. There is no new automatic retry loop. A committed request with unknown outcome and no published response is held by default to avoid paying again blindly; `retry_uncertain_requests=True` is an explicit opt-in recovery decision. No legacy checkpoint is read or rewritten.

The legacy extractive corrective prompt is incompatible with editorial paraphrasing and is not reused. A3 makes zero corrective model calls; existing local JSON repair and the provider's unchanged transport retry policy remain available. This deliberately avoids introducing an unverified editorial correction policy.

## 8. Validation and rejection handling

`assess_response` reuses strict JSON/schema checks, existing normalization/content/reference checks, `validate_chunks(..., editorial=True)`, A1 grounding safeguards and `assess_editorial_chunk`. Article/chunk references must exactly equal the prepared reference authority. Literal topic/population conflicts add adapter findings without changing legacy validators.

| Outcome | Representation |
| --- | --- |
| Technically valid, independently reviewable draft | Existing `REVIEW_REQUIRED`, never clinical `PASS`/approval |
| Invalid JSON/schema | `INVALID_JSON` / `SCHEMA_INVALID`; raw retained, no review storage |
| Citation/content preflight mismatch | `REFERENCE_OR_CONTENT_INVALID`; original controlled root code and high-level category retained separately |
| Numeric, recommendation-strength/certainty, topic or population contradiction | `SOURCE_VALIDATION_FAILED` with existing rule/claim reasons; quarantined |
| Model source abstention | `SOURCE_PASSAGE_HELD`; raw preserved, zero-call reconsideration possible |
| Source/relationship/insufficient-evidence hold | Eligibility reason retained; no model call |
| Transport/auth/quota failure | Original allowlisted provider code plus separate classification; no provider message |
| Local readback failure | Adapter storage code `STORAGE_READBACK_INCOMPLETE`; own new row excluded; candidate retained |

Adapter-only storage codes live in `UnitResult.storage.code`; unchanged M1 code sanitization may render their aggregate root as `UNCLASSIFIED_ERROR`. The explicit adapter code prevents losing that storage distinction. Failure to perform compensating exclusion is separately `STORAGE_QUARANTINE_FAILED` and never reported as verified storage.

Missing population/qualification or insufficient semantic proof produces mandatory review/hold rather than fabricated text. Short conditional/low-certainty recommendations remain short review drafts; no padding was introduced. Source-grounding errors are not disabled. Raw candidate text stays in the dedicated candidate artifact; operational reports replace validation messages with generic text and retain controlled rules, reasons and offsets. Provider errors/secrets/source passages are not copied into operational diagnostics.

## 9. SQLite storage/readback behavior

The adapter calls the actual existing `ingest_chunks_file` and `verify_chunks_file_ingested` against the explicitly supplied **existing-schema local review database**. No alternative review schema, migration, production collection or remote embeddings were added. Tests use disposable DBs only.

Each accepted independent candidate uses a stable opaque A3 package namespace and the existing package-scoped ID mode. Internal source identity is stored under `proposal_storage_identity` and in `source_link.json`, deliberately avoiding the legacy global `local_storage_identity` upsert mode. A matching historical global PDF-content row is detected read-only and preserved. Unrelated historical rows/embeddings and manually changed review decisions are not overwritten. Repeated ingestion leaves compatible rows unchanged.

Only candidates allowed by `eligible_chunk_numbers(report, {'REVIEW_REQUIRED'})` are passed to storage. Generated duplicates retain their raw checkpoints but add no second row. Rejected/held proposals add no review rows.

Independent readback compares all five text fields, source/package/chunk metadata, validation/review status, content hash, actual local vector values and the existing hash-embedding algorithm/dimension. False verification, content/vector corruption and a raised readback exception are tested. In-process failures compensate by setting only the adapter-owned row's validation status to `FAILED`; the existing local retrieval query excludes it. No five-field text is rewritten. Stricter revalidation similarly excludes only a demonstrably owned A3 draft. If SQLite is unavailable even for compensation, exclusion cannot be guaranteed; the result explicitly reports storage failure and requires operator recovery.

The existing local chatbot remains an **internal review** experience that can retrieve unapproved `REVIEW_REQUIRED` material. Its legacy query does not constitute production approval filtering. A process exit immediately after SQLite commit can leave an unapproved review row before final readback; a real subprocess test proves the next invocation verifies it with zero model calls and no duplicates. No production publishing boundary is activated.

## 10. Synthetic end-to-end execution evidence

All evidence below is fictional, offline and unreviewed by a qualified clinician.

| Scenario | Actual outcome |
| --- | --- |
| Prepared descriptive protocol proposal | One fake logical call, one `REVIEW_REQUIRED` draft, exact SQLite/vector readback |
| Distinct 100–200 word protocol paraphrase | Accepted for mandatory review; output text preserved exactly without post-generation rewriting |
| Four independent prepared protocol units, each fresh benchmark | Four calls, four ordered review drafts, four independently verified rows |
| Same four units resumed | Zero calls, same ordered output, four compatible existing rows unchanged |
| Conditional/low-certainty recommendation | Original qualifiers retained; short review-required draft |
| Wrong citation / invented number / population swap / strengthened recommendation / inflated certainty | Candidate retained and rejected; no eligible local review row |
| Ambiguous trial arms/endpoints, table/figure associations, missing cross-page relationship, damaged passage | No calls; visible relationship/source holds |
| One transient timeout among two units | Independent success saved; next invocation calls only failed unit and resumes success |
| Hard process exit after response publication | Response survives; restart makes zero calls and saves/verifies draft |
| Hard process exit after SQLite commit | Restart makes zero calls, verifies the existing row, count remains one |
| Synthetic actual PDF with printed fictional DOI, native extraction/layout recovery and preparation save/load | DOI passes preflight; existing critical citation-cleaning/unmapped-evidence hold survives cache; zero calls and zero stored drafts |

The last case is intentionally **held**, not relabeled a fresh-PDF success. Earlier development used an injected fictional prepared citation; the final physical-PDF test uses the actual extracted authority and asserts the observed hold. Memory fixtures use synthetic prepared authority to isolate the A3 contract.

Example sanitized diagnostics from the final four-unit cached benchmark (selected real fields):

```json
{
  "proposed_concepts": 4,
  "eligible_drafting_units": 4,
  "planned_generation_calls": 4,
  "resumed_units": 4,
  "reviewable_drafts": 4,
  "persisted_drafts": 4,
  "corrective_calls": 0,
  "scheduler": {"mode": "rolling", "max_in_flight": 2},
  "observability": {"request_totals": {"logical_calls": 0, "http_attempts_observed": 0}},
  "document_complete": false,
  "document_coverage_complete": false,
  "clinically_approved": false,
  "production_ingestion": false
}
```

`planned_generation_calls` counts eligible units, including cached units; it is not a forecast of actual HTTP traffic. `persisted_drafts`, M1 `saved_review_drafts` and `final_stored_rows` count verified selected rows present after the run, including unchanged resumed rows. Each unit's `storage.inserted/updated/unchanged` distinguishes actual writes. In this cached example all four rows are unchanged; no new rows are claimed.

Exact new top-level audit fields are `version`, `proposal_plan_id`, `evidence_fingerprint`, `validation_policy`, `proposed_concepts`, `eligible_drafting_units`, `planned_generation_calls`, `corrective_calls`, `document_complete`, `clinically_approved`, `production_ingestion`, `eligibility_counts`, `scheduler`, `resumed_units`, `reviewable_drafts`, `persisted_drafts`, `validation_rejections`, `source_proposal_gaps`, `unit_outcomes`, `recovery_signals`, `source_region_counts`, `source_regions_not_yet_assessed`, `source_regions_blocked`, `document_coverage_complete`; `scheduler_failure` appears for unexpected scheduler/progress failures. M1 adds `observability`.

Per-unit audits include identity/order, `logical_calls`, `provider_seconds` when called, `validation_seconds` when assessed, sanitized `response_metadata`, `prior_validation_decision`, optional failure classification and duplicate link. Unit outcomes retain eligibility reasons and storage results. `source_proposal_gaps` counts proposal-processing dispositions without a stored/preserved/deduplicated result; it is **not** a medical document-coverage metric. A2 region counts and blocked/unassessed regions remain separate.

Reused/new M1 count names include `proposed_concepts`, `planned_source_units`, `eligible_drafting_units`, `planned_generation_calls`, `held_proposal_units`, `resumed_candidate_units`, `cached_source_hold_units`, `generated_candidates`, `resumed_candidates`, `source_held_units`, `schema_rejected_candidates`, `reference_or_content_rejected_candidates`, `assessed_candidate_events`, `accepted_candidate_events`, `rejected_candidate_events`, `validation_decisions_recomputed`, `validation_policy_reconsidered_units`, `scheduler_in_flight_high_water`, `duplicate_candidates`, `saved_review_drafts`, `final_stored_rows`, `merged_candidates`. M1 derives unclassified-event/accounting completeness. Missing candidate events remain explicitly incomplete.

Stage names include `proposal_editorial_workflow`, `proposal_identity_validation`, `proposal_draft_eligibility`, `checkpoint_lookup`, `candidate_publication`, `llm_request`, `local_chunk_validation`, `output_persistence`, `sqlite_storage`, `sqlite_readback`, plus stages emitted by reused helpers. Durations use monotonic clocks. Concurrent work-seconds must not be summed into wall time.

## 11. Before/after offline performance measurements

Command: `python -B tools/evaluate_phase_a3.py`. Final machine-readable evidence: `D:\gastroagi-chunk-pipeline\tmp\phase_a3\evaluation.json`.

The benchmark-only reference uses the same workers and validation/storage, with an explicit two-worker group barrier. The proposed path uses the existing rolling scheduler. Four fake requests sleep 180/25/180/25 ms. Three independent fresh workspaces per scheduler; each is then resumed. All 24 fresh fake requests and all six zero-call resumes are accounted for. Clinical outputs are structurally identical and ordered; max in-flight is two in every run.

| Scheduler | Fresh-workspace seconds, three runs | Median | Cached median |
| --- | --- | --- | --- |
| Group-barrier reference | 0.688807, 0.678881, 0.704451 | 0.688807 s | 0.198518 s |
| Rolling | 0.543544, 0.525484, 0.528036 | 0.528036 s | 0.208778 s |

Rolling median elapsed time is about 23.3% lower for this artificial workload. Cached rolling work is slightly slower in this sample; no general cached speedup is claimed. This comparison is within the new opt-in A3 path, not a measured change to the unchanged default legacy pipeline. It is neither a fresh-upload throughput benchmark nor an Azure latency estimate.

Fake token metadata (50 prompt/110 completion per successful fake response) tests accounting only; these are not measured model tokens or spend. HTTP counts remain unknown for that fake provider. A separate instrumentation test records one logical call with two synthetic observed HTTP attempts (one failure/one success) and unknown usage for the failed attempt. Logical calls are never substituted for HTTP attempts.

## 12. Executed tests and actual outcomes

The existing `tests/conftest.py` redirects live runtime paths/captured defaults to temporary locations and prohibits real Azure clients. New A3 test modules additionally block socket connections. Both subprocess tests invoke only the explicit fake provider and their passed temporary workspace/DB. No real PDF queue, live DB or service was used.

| Execution | Actual outcome |
| --- | --- |
| Protected pre-edit safe suite | **838 passed, 2 deselected, 9 subtests passed**, 61.67 s console time |
| Final focused A3 suite | **62 passed, 0 failed, 0 skipped**, 12.39 s console time |
| First full integration attempt | **895 passed, 1 failed, 2 deselected, 9 subtests passed**, 68.07 s; failure was a new A3 test's overlong nested Windows fixture path |
| Final safe full suite | **900 passed, 0 failed, 2 deselected, 9 subtests passed**, 65.32 s console time |
| Final protected/A1/A2/benchmark comparison | Exit 0; 117 protected files unchanged, 20 A2 cases identical, complete A1 comparison identical; all six benchmark fresh/resume pairs passed |

JUnit counts include subtests: baseline 847, first full 905 (one failure), final 909 (900 tests plus nine subtests); all recorded JUnit `skipped` counts are zero. Real-source exclusions below are not disguised as executed passes.

Reproduction from `D:\gastroagi-chunk-pipeline`:

```powershell
python -B -m pytest tests/test_phase_a3_editorial_integration.py tests/test_phase_a3_recovery_storage.py -q -p no:cacheprovider --tb=short --basetemp=tmp/phase_a3/ft2 --junitxml=tmp/phase_a3/focused-test-results.xml -o junit_family=xunit1
python -B -m pytest tests/ --ignore=tests/test_native_layout_real_source.py --ignore=tests/test_task11_regression.py -k 'not both_protected_real_anchors' -q -p no:cacheprovider --tb=short -o junit_family=xunit1 --basetemp=tmp/phase_a3/f3 --junitxml=tmp/phase_a3/final-test-results.xml
python -B tools/evaluate_phase_a3.py
```

Both real-PDF modules were excluded **before collection**. Two protected-real-anchor cases were deselected by the same selector in baseline and final runs. No live-capable entrypoint was enabled to increase the pass count. The shorter final basetemp is intentional on Windows.

All 25 requested test categories are represented: valid review draft; ambiguous arms/outcomes; missing population and context review; wrong reference; invented number; stronger recommendation; preserved conditional/low certainty; duplicates; stale source/proposal; table/figure hold; budgets; bounded refill; ordered merge; timeout; auth/quota; zero-call resume; policy reconsideration; interruption; partial completion; SQLite/readback; rejected/held exclusion; legacy extractive regression; A1 adversaries; no fabricated review objects; no production publishing/network. Additional tests cover process exits at two publication/storage boundaries, corrupt response hashes, unknown request outcomes, manual review-status preservation, historical global IDs, readback exceptions/corruption and source changes during generation.

Development iterations also ran focused subsets. Initial failures involved two helper mistakes, the overlong new benchmark/fixture path, and an initially incorrect success expectation for the physical synthetic PDF. Those were diagnosed and corrected; no prior test or validator was weakened. The physical-PDF test now explicitly asserts the actual retained source hold. The first failed full JUnit is retained as `tmp/phase_a3/first-final-test-results.xml` rather than hidden.

Before the final paraphrase test was added, the intermediate focused run passed 61 tests in 10.55 s and the intermediate full run passed 899 tests plus nine subtests in 65.27 s. The final counts above include that added assertion of unchanged paraphrased output.

## 13. Legacy generation/regression compatibility

All 117 protected hashes match the pre-edit manifest. Current A2 full outputs, not just counts, match all 20 baseline cases. The entire A1 comparison except its elapsed-time field matches baseline, including all 31 legacy report comparisons/statuses/reasons and original grounding-error counts.

A1 unchanged: eight source cases, 31 variants, five intended examples review-required and three rejected; 15 adversaries, 11 rejected and four review-required; all 15 expected reasons identified; **zero automatic clinical passes**. `review_uncertainty`, `conditional_guideline`, `uncertain_dose` remain unresolved at chunk level. Existing extraction, source planning, generation/correction, scheduling, provider retries, checkpoint reuse, storage/retrieval and queue/approval tests pass in the safe suite.

No default route, runtime module import, prompt, clinical threshold, retry limit or source ownership algorithm changed. No human-reviewed route was replaced.

## 14. Cache/source fingerprint changes

Existing broad validation fingerprint remains:

`79d1298b9b4b56a629f33c3c7581cb0c49719d7715cad135c84be7ffbc8d8b13`

No `src/` file changed, so legacy preparation/generation/validation cache identities are not invalidated. New A3 candidate and policy namespaces are separate. Generation-related implementation revisions intentionally invalidate A3 candidates; validation-adapter-only revisions revalidate the same compatible raw response. Existing preparation checkpoint keys/formats are unchanged and exercised by the physical synthetic-PDF cache test.

Installed dependency baseline remains pypdf 6.9.2, PyMuPDF 1.27.2.2, Pydantic 2.13.5, pytest 9.1.1, OpenAI 3.26.1, Playwright 1.62.0, python-dotenv 1.2.3 (Python 3.13 execution). Relevant lock/atomic-publication/SQLite APIs were checked against installed behavior and official Python 3.13 documentation: [byte-range locks](https://docs.python.org/3.13/library/msvcrt.html), [file links/replacement](https://docs.python.org/3.13/library/os.html), [SQLite](https://docs.python.org/3.13/library/sqlite3.html). No provider capability check or SDK upgrade was performed.

## 15. Remaining clinical accuracy and A1/M4 issues

Automatic eligibility is intentionally limited and may over-hold useful concepts. It does not establish semantic entailment. Complex outcomes, arm binding, noninferiority, safety and cross-page clinical associations remain unresolved before generation. Generic lexical/consistency checks cannot certify an arbitrary paraphrase, even when all deterministic checks pass. A model could introduce a subtle unsupported interpretation that still requires clinician review.

There is no qualified clinician-reviewed A3 evaluation, live Azure output-quality evaluation, or editorial regeneration policy. The three A1 examples and four review-required adversaries remain as before. M4 clinical acceptance is **PARTIAL**. A3 reviewable/storage counts are never clinical approval counts.

## 16. Remaining fresh-PDF/M5 limitations

The new API begins at verified preparation; it does not repair OCR, complex layouts, tables, extraction uncertainty or bibliographic conflicts. The physical synthetic-PDF case demonstrates a genuine retained citation-cleaning/unmapped-source hold, even though its citation passes preflight. That must be resolved in a separately scoped preparation/source-mapping task, with corroboration retained, before claiming adequate automatic fresh-PDF throughput for this layout.

No existing user PDF, the 19 queued PDFs, production DB or historical checkpoints were accessed for testing. No real Azure throughput/token/capability evidence was collected. Unknown-response interruption remains an explicit hold; disk/SQLite failure can require operator recovery; abrupt power-loss durability and every filesystem failure point were not exhaustively tested. M5 remains **PARTIAL**.

## 17. Recommended Phase A4 coverage reconciliation scope

Future authorized A4 work should reconcile A2 canonical regions and supporting contexts with A3 accepted/rejected/duplicate/deferred dispositions; keep exclusions/holds/unassessed material visible; distinguish proposed representation from clinically reviewed completeness; and adjudicate context/relationship holds without silently broadening generation eligibility. The synthetic citation-removal hold should be characterized at the preparation boundary with explicit source mapping, not suppressed by coverage arithmetic. No A4 implementation was started.

## 18. Backward Compatibility and Regression Report

| Item | Evidence/result |
| --- | --- |
| Existing modified files | None; all 117 protected source/config/prompt/test/report/skill files match baseline |
| New files | Eight isolated package modules, three test/support files, one evaluator, this report |
| Reproducible baseline | `tmp/phase_a3/baseline/source/`, `sha256-manifest.json`, `versions.json`, `validation-fingerprint.txt`, `a2-proposals.json`, `a1-comparison.json`; baseline JUnit |
| Final tests | 900 passed plus nine subtests; same exclusions; zero final failures/runtime skips |
| Output/provenance invariants | Exact A2/A1 comparison, source mutation/stale-plan rejection, five-field schema, selected-source-only requests, ordered output equality, immutable responses and sidecars |
| Runtime invariants | Max two requests, refill/drain, fatal-stop handling, timeout recovery, unknown-outcome hold, zero-call compatible resume |
| Storage invariants | Same schema/functions/local embedding settings, exact content/vector readback, idempotence, historical-row/decision preservation, quarantine exclusion |
| Approval invariants | No reviewed identities fabricated; all drafts unapproved; production path inactive; no network in A3 tests |
| Intentional new behavior | Explicit opt-in proposal eligibility/orchestration and separate A3 checkpoint namespace; adapter-owned draft exclusion after failed readback or stricter revalidation; no existing shared behavior changed |
| Performance | Synthetic group/rolling comparison above; no measured legacy/default-route or Azure speedup |
| Remaining risks | Narrow eligibility, unresolved semantics, clinical review outstanding, physical-PDF source hold, unknown interrupted requests, filesystem/SQLite outage recovery and legacy internal-review retrieval semantics |

Rollback: stop explicitly invoking/importing `proposal_editorial`; no default caller needs reverting. Remove only the listed new code/test/tool/report files if desired. Preserve new A3 workspace candidates and review records for audit/recovery; do not delete a shared DB, legacy outputs or any queue/checkpoint directory. Existing protected sources need no restoration; their byte-identical snapshot and SHA-256 manifest are available for verification. There is no schema migration, deployment/config switch or production publishing state to roll back.

No paid calls, Azure connectivity checks, production embeddings/ingestion, queue resumption, `.env` edits, user PDF processing or A4/A5/6C/6D work occurred.

**PASS — Phase A3 opt-in offline engineering acceptance: implemented, bounded, source-bound, resumable, validated and locally persisted with protected regression behavior preserved. Milestones 4 and 5 and Phase A1 remain PARTIAL; clinical approval and production readiness are not established.**

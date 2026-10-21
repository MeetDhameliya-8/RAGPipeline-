# GastroAGI Milestone 2 — Safe Throughput and Processing Optimization

Date: 19 October 2026. Scope: Milestone 2 only. The current audit and Milestone 1 report were inspected before implementation. Milestone 1's saved results were checked and its current diagnostic/provider tests rerun: **55 passed, 4 subtests passed** before these changes.

Implemented a bounded rolling source-package scheduler. Measured local proof/alignment/storage/cache/extraction work on disposable synthetic fixtures; **no CPU proof-cache or extraction shortcut was shipped**. Clinical prompts, writing, planning, validation criteria, source ownership, provider settings, production gates and real runtime state were preserved.

## 1. Files changed

| File | Change |
|---|---|
| `src/bounded_scheduler.py` — new | `SubmissionControl`, ephemeral `Outcome`/`ScheduleRun` records, `run_bounded`: completion-driven refill, one/two-future admission limit, shared stop gate, cancellation/draining and ordered outcomes |
| `src/package_generation.py` | `generate_source_packages`: rolling dispatch; worker-local audit retention even on unexpected exceptions; source-order merge; fatal service stop; safe correction deferral; scheduler diagnostics and error propagation. Existing ownership equality check also runs before dispatch |
| `tests/milestone2_support.py` — new | Disposable native-PDF fixture, deterministic per-packet mock responses and an implementation of the original fixed-group scheduling policy for comparison |
| `tests/test_milestone2_scheduler.py` — new | 14 test functions / 22 cases covering refill, ordering, limits, source equivalence, service failures, cancellation, interruption, checkpoint recovery and invalid identities |
| `tools/benchmark_milestone2.py` — new | Reproducible offline scheduler comparison and isolated CPU measurements using Milestone 1 observation scopes; only metrics/digests exported |
| This report — new | Results, evidence, limitations and deliberately deferred optimization |

No dependencies were added. No prompt, clinical validator, planning, extraction, source-proof, evidence-storage, preparation-cache, queue-recovery, database or production-ingestion implementation was edited. No Git repository was present or initialized.

## 2. Rolling scheduler behavior

The scheduler initially commits at most the configured one/two tasks. It waits for the **first completed future**, collects the entire completed cohort, checks the stop latch, then fills only free slots. It does not enqueue the document into the executor. Source-package workers continue using the existing generation, assessment, immutable candidate publication and bounded correction logic.

Completed results are retained and returned in source-index order. `generate_source_packages` merges those outcomes in that order using the existing five-field deduplication, chunk numbering and ownership logic. Model completion order does not determine article metadata, chunk order, duplicate winners or provenance.

Implementation evidence: `src/bounded_scheduler.py:L48-L115`; `src/package_generation.py:generate_source_packages`, especially `attempt`, `observed_attempt`, `merge_audit` and the final ordered outcome loop. The scheduler's dataclasses are transient process records, not new persistent schemas or content fields.

### Safety guarantees

- **Concurrency remains one or two.** Direct callers retain their default of one; the configured Admin path still requests two. Neither path can configure more through this helper.
- **No speculative document-sized future queue.** Pending submitted futures never exceed the worker limit. A 100-item test also measures actual executor outstanding submissions.
- **Prompt refill.** An event-controlled test makes source 0 wait until source 2 enters a free worker. It succeeds under rolling scheduling, demonstrating that completion of source 1 can schedule source 2 while source 0 remains unfinished.
- **Deterministic output.** Actual generation output, provenance, rejection lists and final actual validator reports are structurally identical to the fixed-group baseline when given the same responses. Explicit out-of-order completion preserves numbering and source-order audits.
- **Worker isolation.** Each source index owns a separate audit dictionary. The coordinating thread reads it only after that future has finished. Milestone 1's observation binding and lock-protected request accounting remain in use.
- **Immutable checkpoints.** Existing complete attempt files and exclusive hard-link publication remain unchanged. Results completed while another worker fails are still retained on disk and can be resumed.
- **Ownership mismatch stops before dispatch.** The preexisting per-worker ownership equality rule is also checked for the whole schedule before starting workers, preventing an unrelated valid worker from issuing a request while another scheduled unit is already invalid. No acceptance rule or ownership value changed.

### Authentication, quota and correction handling

Workers set a shared stop latch as soon as authentication/quota errors reach the package boundary, before writing their failure sidecars. Submission and stop share a short lock so admission is coordinated. The initial one/two-task window is committed as a bounded unit; those already-committed workers can finish. No later source-package submissions occur after the stop latch is set.

A correction is a new request commitment. Before committing an existing bounded correction, a worker checks the same stop gate. If service stop/interruption has already occurred, it retains the initial failed candidate and assessment and defers the correction. It records `phase=correction_deferred_after_stop`, `provider_call_started=false` and the known service-stop code, or GENERATION_CANCELLED for an interruption. It does **not** create a permanent SOURCE_VALIDATION_FAILED hold for a correction that never ran. Requests committed before a stop can still finish; running Python threads/SDK calls are not forcibly terminated.

The correction policy on a healthy run is unchanged: at most one corrective generation when no candidate is allowed, with the same prompts and checks. No retries, timeouts, rate limits or model settings changed.

### Unexpected errors and cancellation

Unexpected worker exceptions stop replenishment. Every submitted future is drained and its audit accounted for. One error is re-raised; multiple errors are propagated together through a BaseExceptionGroup/ExceptionGroup. Exception text is not copied into new diagnostic records. Unexpected worker interruption also marks candidate accounting incomplete rather than implying all events are known.

Coordinator/progress-callback interruption stops admission, cancels only futures that have not started, and waits for already-running work. Those workers retain completed checkpoints. Cancellation is represented explicitly and propagated rather than treated as a successful empty result. Progress callbacks that raise and futures cancelled before start are covered by tests. There is no new cancellation endpoint, queue state or production API.

The returned ordered outcome collection can grow with completed work while an early source is slow. **In-flight futures are bounded; completed-result memory is not limited to two items.** This distinction allows rolling progress while preserving deterministic merge order. It is a remaining memory consideration for very large documents, not speculative queued model work.

## 3. New diagnostics

The existing audit gains a `scheduler` object:

- `mode`: rolling.
- `max_in_flight`: high-water count of submitted, not-yet-collected futures, bounded by configured concurrency.
- `submitted_units`: number of submitted source tasks, including cancelled outcomes.
- `completed_units`: finished non-cancelled task outcomes, including failed tasks; not successful model calls or clinically accepted chunks.
- `cancelled_units`: cancelled task outcomes.
- `unsubmitted_units`: planned source tasks never submitted.
- `submission_stopped`: whether the stop latch was set.
- `stopped_early`: whether tasks remained unsubmitted or an outcome was cancelled.

`scheduler_errors[]` records `source_index`, `cancelled` and the existing sanitized `failure_classification`. It contains no exception body, source text, filename or credential. Milestone 1 adds a `counts.corrections_deferred_after_stop` event when applicable. Existing logical-call/HTTP-attempt/token counters continue to describe observed requests; future counts do not substitute for those counters.

Progress now reports actual finished/visited source tasks, without implying clinical approval or complete-document coverage. It can update after out-of-order completions. Healthy source-order output merging occurs after the submitted work has drained.

## 4. Actual offline scheduler benchmark

Metric artifact: `tmp/milestone2/measurements.json`, written **19 October 2026, 14:44:53 IST / 09:14:53 UTC**. Command:

```powershell
python -B tools/benchmark_milestone2.py --repeats 5 --output tmp/milestone2/measurements.json
```

The benchmark uses eight already-prepared **synthetic** source packages, the actual generation/validation/merge implementation, two workers and a mock provider. Mock response delays are `[0.12, 0.01, 0.12, 0.01, 0.12, 0.01, 0.12, 0.01]` seconds. No generation candidates or provider responses are reused from disk. The source is prepared once to isolate scheduling; this is **not a fresh-upload PDF benchmark**.

The baseline substitutes the original fixed-group admission/wait policy while sharing all generation, validation and merge logic with the rolling run. Measurement order alternates between baseline-first and rolling-first. Each of ten runs makes eight mock calls, returns eight source-owned chunks, reaches maximum two active provider calls, and produces the same structural output/provenance/rejection/failure digest.

| Trial | Fixed groups, seconds | Rolling, seconds |
|---|---:|---:|
| 1 | 0.544341 | 0.314288 |
| 2 | 0.549283 | 0.335058 |
| 3 | 0.553774 | 0.320724 |
| 4 | 0.553113 | 0.316751 |
| 5 | 0.546138 | 0.332251 |
| Median | **0.549283** | **0.320724** |

For this injected-delay workload, rolling scheduling reduced median elapsed time by **41.61%**, a baseline/rolling ratio of **1.71×**. This is a measured offline scheduling result, **not an Azure speedup or a general ingestion-time promise**. Actual service latency, quota, rate limits, visual processing, local CPU load and cache state were not modeled as real external systems.

All actual Azure/paid requests: **zero**. Mock transport has no verified HTTP-attempt count; it is not described as eight paid HTTP requests.

## 5. Measured local processing — no CPU shortcut shipped

`cpu_measurements` uses disposable 4-, 8- and 16-page native PDFs. Milestone 1 `Observation`/`stage`/`timed` helpers temporarily instrument proof validation and alignment during the benchmark; application source functions are not rewritten. Evidence maps, source spans, warnings and package plans are compared structurally through full-model digests and save/load round trips.

Five direct proof checks and five proposed full-identity-key calculations are timed per fixture. The proposed key calculation serializes the current complete evidence graph and plan and hashes the validation implementation. It is a **cost experiment, not a deployed proof cache**, and does not establish a complete reusable-proof protocol under every mutation/concurrency condition.

| Pages | Median proof validation | Median full-identity key calculation | Compressed source-map file | Source JSON reads per valid preparation load |
|---:|---:|---:|---:|---:|
| 4 | 7.426 ms | 8.013 ms | 12,586 bytes | 2 |
| 8 | 15.620 ms | 10.364 ms | 22,910 bytes | 2 |
| 16 | 31.914 ms | 14.576 ms | 42,771 bytes | 2 |

The additional operation sequence deliberately performs **two native extractions**, direct/package-source proof checks, source-map save/load, preparation save/load, and corrupted-cache rejection. It is not one production upload path and its timings must not be summed as such.

| Pages | Proof checks in that sequence | Cumulative proof time | Alignment calls / cumulative time | Two native extraction envelopes |
|---:|---:|---:|---:|---:|
| 4 | 6 | 47.732 ms | 10 / 28.055 ms | 157.450 ms |
| 8 | 6 | 94.362 ms | 18 / 55.716 ms | 322.758 ms |
| 16 | 6 | 190.255 ms | 34 / 110.844 ms | 634.204 ms |

Inclusive envelopes contain nested measurements. For example, alignment is part of evidence construction and native extraction; these columns overlap and must not be added.

### Source-package proof validation

Repeated recomputation is measurable. However, current evidence/plan models are mutable and proof checks occur across source construction, serialization, loading and generation boundaries. Repetition alone does not establish that a check is redundant after arbitrary intervening mutation.

The tested full-identity key already costs more than recomputation on four pages and remains a material fraction of the check on larger fixtures. The larger cases suggest a possible optimization opportunity, but this experiment does not prove an end-to-end benefit for realistic mixed/scanned evidence or a safe concurrent reuse contract. **Proof memoization is deferred.** No object-identity-only, filename-only or stale proof cache was added; no proof check was skipped.

Any future proof cache must bind the exact PDF identity, current buffers/spans/mappings/warnings/geometry, plan ownership/dependencies/configuration, visual-source identity and validation implementation, and must fail closed on stale/modified evidence. This milestone introduces no such reuse mechanism.

### Buffer alignment

The measured calls correspond to per-page/document alignment in two deliberately repeated extractions. They operate on distinct mappings/targets; the benchmark did not establish a safely removable duplicate operation in one extraction. Existing raw/cleaned/canonical mappings and transformation warnings remain unchanged. **No alignment optimization shipped.**

### Evidence storage verification

Save/load structural equality and plan/source identity equality passed for all three fixtures. Measured inclusive save/verify times were 14.969/18.571 ms for four pages, 23.378/23.205 ms for eight, and 46.452/47.105 ms for sixteen. These include nested proof work. Checksums, size bounds, evidence validation, warning retention and plan verification remain enabled. **No storage verification was removed or reused.**

### Preparation cache

A valid preparation load reads its source JSON twice: once for the manifest checksum and again through source-map loading. This is a verified duplicate read, but these test source maps are only 12–43 KB; the experiment did not establish a significant I/O bottleneck. Cached source/plan round trips matched the original models, and corruption of a disposable extraction checkpoint correctly caused a cache miss. **Duplicate-read optimization is deferred**, avoiding an unmeasured loader-interface change or weakened validation.

### PDF page text and geometry

Across the two native runs, each fixture page received one pypdf layout extraction, one PyMuPDF sorted extraction and one PyMuPDF geometry extraction per run: counts were 8/8/8, 16/16/16 and 32/32/32 respectively. There were no plain/inflation fallback calls in these fixtures.

The cross-library reads provide corroboration; geometry supplies separate native evidence. These are not interchangeable operations. The test does not cover every sparse/scanned/vector-heavy fallback. **No extractor, geometry pass or corroboration check was removed.** Historical cached clinical runs were not used as fresh-upload evidence.

## 6. Tests and outcomes

Existing test isolation and the fake-client Azure guard were retained. Commands explicitly target `tests/`, excluding the root live-capable Azure diagnostic. All use `python -B -m pytest`, `-q -p no:cacheprovider`, a separate `tmp/milestone2/` base directory and JUnit result file; later focused commands also use `--tb=short`.

| Execution / artifact under `tmp/milestone2/` | Actual outcome |
|---|---|
| Milestone 1 prerequisite: diagnostics/provider modules; `prerequisite-results.xml` | 55 passed, 4 subtests passed in 3.26 s |
| Initial scheduler + ingestion-latency + diagnostic tests; `focused-results.xml` | 4 failed, 52 passed in 6.18 s |
| Corrected same focused selection; `focused2-results.xml` | 56 passed in 5.69 s |
| Scheduler safety tests including service-stop/correction handling; `scheduler-final-results.xml` | 21 passed in 4.50 s |
| Full offline `tests/` suite; `offline-results.xml` | **575 passed, 7 skipped, 9 subtests passed in 44.25 s** |
| Final scheduler + diagnostics + ingestion-latency + source-package + queue-recovery selection; `final-boundaries-results.xml` | **76 passed in 12.07 s** |
| Final unexpected-error accounting refinement: scheduler module; `exception-accounting-results.xml` | **22 passed in 4.11 s** |

The initial failures were two restart cases reusing the same stopped test controller, the same controller reuse in the unexpected-error restart case, and a late per-worker ownership check allowing another valid worker to call the mock provider. Tests now create a fresh controller on restart, and the existing ownership check also runs before dispatch. No failed test was ignored or converted into a skip.

The final targeted runs include the explicit out-of-order full-generation test and final diagnostic flags added after the full suite. The last change conservatively marks unknown worker-error candidate accounting incomplete. The full-suite result is not represented as a rerun of those subsequent small refinements.

Seven skips remain missing runtime-source fixtures: two multiview anchors, three real native-layout tests and two TASK11 real-source regressions. They are not claimed as passed or independently reviewed clinical coverage.

### Safety coverage

| Requirement | Direct evidence |
|---|---|
| Refill before an unrelated slow request finishes | Event-controlled helper and actual provider tests; source 2 starts before source 0 finishes |
| Bound pending work / requests | 100-task submission test; configured serial/two-worker tests; all benchmark runs maximum two active mock provider calls |
| Deterministic merge/numbering/provenance | Fixed-policy versus rolling full output/ownership comparison; actual final validator-report equality; explicit out-of-order numbering and audit-order assertions |
| Repeated candidates / deduplication | Four repeated candidates retain one output chunk and the same ordered source ownership, with three duplicates |
| Authentication/quota | Fast service failure while earlier work waits; no replacement packages; earlier completed checkpoint preserved; restart resumes it |
| Correction after service stop | No new uncommitted correction, no permanent validation hold for unattempted work, original failed assessment retained |
| Unexpected exceptions | Single error propagated with complete submitted audit; two simultaneous errors both retained/propagated; no private error text exported in scheduler diagnostics |
| Interruption/cancellation | Progress callback KeyboardInterrupt drains two committed tasks and supports restart; cancelled future is explicitly counted and propagated; pre-stopped/empty schedules dispatch nothing |
| Source mismatch / duplicate IDs | PDF identity, schedule ID, ownership and duplicate package-ID corruption fail before provider invocation |
| Invalid checkpoints | Corrupt JSON, wrong fingerprint, invalid schema and wrong source filename cause revalidation/regeneration; compatible sibling candidate resumes unchanged |
| Source/CPU integrity | Full evidence/plan identity unchanged by measurements; storage/preparation round trips structurally equal; disposable damaged preparation rejected |

No live latency, rate-limit, cost, server throughput or fresh clinical upload benchmark was executed.

## 7. Compatibility, risks and deliberately deferred work

1. **Clinical behavior:** prompts, five content fields, word targets, citation rules, source spans/ownership, clinical planning, validation decisions and approval/production gates remain unchanged. The same supplied responses produce the same ordered clinical output and final validation report.
2. **Operational changes:** rolling admission can complete more work before a later failure is discovered. After the stop is observed, no further packages are admitted. Already-committed work is drained and kept; Python threads cannot be killed safely. Existing request timeout/retry limits still apply.
3. **Correction deferral:** unavailable service/interruption can leave a rejected candidate awaiting a future correction instead of issuing a new call after stop. It is not treated as a permanent clinical source rejection or approval.
4. **Error compatibility:** unexpected single exceptions still escape; simultaneous failures may now escape together as an exception group. This intentionally exposes failures previously liable to be lost behind one future's exception. Queue safety tests passed; no new retry classification policy was introduced.
5. **Memory:** at most two pending futures, but completed outcomes and per-unit audit metadata accumulate for deterministic merging. Further result-buffer/storage optimization is deferred.
6. **Cache compatibility:** successful candidate and source-passage hold identities/publication rules are unchanged. The existing validation-hold fingerprint hashes all source Python files, so a code update can invalidate old validation-hold reuse on a future authorized run. That policy was neither weakened nor bypassed. Preparation-cache implementation modules were not edited. No real checkpoint was rewritten or resumed here.
7. **CPU optimization:** no memoized proof, altered alignment, shared extractor representation, bypassed storage check or cache-loader shortcut was shipped. Measurements suggest where future work may be worthwhile but do not establish a safe general replacement. Any reused proof still requires exact identity and mutation rejection.
8. **Accuracy limits:** independent clinical review, complete-document coverage, scanned-source fidelity and production retrieval remain unresolved outside this milestone. A throughput improvement does not establish medical accuracy.
9. **External performance:** higher local utilization can change request cadence and encounter different real service limits. No real Azure speedup is claimed. Separately authorized service benchmarking would be needed to measure that.

## 8. Protected state and completion

Before/after hashes cover **2,202 protected files**, including real input/processed PDFs, saved outputs/checkpoints, database/log files, prompts/configuration, `.env` and the inspected clinical/extraction/planning/storage/provider/queue modules that were not in scope for changes. The verification at **19 October 2026, 14:46:17 IST / 09:16:17 UTC** found **zero changed/missing protected files and zero new files in real runtime directories**.

Read-only immutable SQLite inspection still showed paused=1, eleven LOCAL_REVIEW_READY and nineteen QUEUED jobs. No real queue/database mutation, paid request, service restart, production write or clinical ingestion was performed. Benchmark sources/checkpoints were disposable synthetic files; only sanitized metrics and test artifacts remain under `tmp/milestone2/`.

Implementation references checked: Python 3.13 [futures completion/shutdown/cancellation](https://docs.python.org/3.13/library/concurrent.futures.html) and [thread events](https://docs.python.org/3.13/library/threading.html#event-objects). Installed-runtime behavior was verified by the tests and offline measurements above.

**PASS — Milestone 2 implemented and verified offline. Rolling throughput improved on the measured mock workload; CPU shortcuts were deliberately deferred. Work stops here with the queue paused.**

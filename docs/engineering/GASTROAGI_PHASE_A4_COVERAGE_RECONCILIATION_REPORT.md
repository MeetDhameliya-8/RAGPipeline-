# GastroAGI Phase A4 — Coverage reconciliation and missing-evidence accounting

Date: 21 October 2026. Scope: **Phase A4 only**. **PASS for offline technical coverage reconciliation.** Clinical completeness is never asserted. Milestones 4–5 and Phase A1 remain **PARTIAL**. No qualified clinician reviewed these synthetic results.

All repository-relative paths below are under `D:\gastroagi-chunk-pipeline\`.

## 1. Current coverage gap diagnosis

M3 inventories canonical passages and native-source concerns; A2 proposes source-backed questions without certifying their relationships; A3 records drafting eligibility, returned candidates, validation and local review storage. A3's aggregate counts alone do not establish that a particular passage produced a particular verified stored draft. They also do not reconcile overlapping contexts, exact duplicates, omitted proposals, interrupted requests or source holds into one traceability ledger.

A4 adds an explicitly invoked, deterministic, local reconciliation engine. It reads the existing contracts and artifacts, independently checks their links, and reports missing/unknown evidence. It does not schedule generation, fill missing knowledge, change source ownership, approve a clinical statement or activate publishing.

The audit, Milestone 1–5 reports, A1–A3 reports, 6B plan, actual source contracts and test isolation were inspected. The project GastroAGI engineering skill was applied. A fresh protected snapshot and safe full-suite baseline preceded implementation; no Git repository was initialized.

## 2. New files and functions

**Existing files modified: none.** No dependency was added.

| New file | Main functions/contracts |
| --- | --- |
| `coverage_reconciliation/models.py` | Versioned `CoverageReport`, `SourceIdentity`, `RegionLink`, `ProposalLink`, `ReviewTask`, `StageRecord`, `StageObservation`, `GenerationContext`, `ReconciliationLimits`, controlled error |
| `coverage_reconciliation/accounting.py` | `union_characters`, `task`: exact interval unions and stable review-task IDs |
| `coverage_reconciliation/artifacts.py` | `ArtifactReader`: bounded, read-only JSON snapshots, path containment and stability checks |
| `coverage_reconciliation/verification.py` | `capture_generation_context`, `verify_context`, `verify_workflow`, `candidate_proof`, `expected_link` |
| `coverage_reconciliation/storage.py` | `StorageSnapshot`, `expected_chunk`, `verify_storage`: read-only indexed SQLite checks and local vector verification |
| `coverage_reconciliation/engine.py` | `reconcile_coverage`, `implementation_identity`, internal budget/join/region/task helpers |
| `coverage_reconciliation/reporting.py` | `human_summary`, `write_report`, `_checked`: conservative summaries and integrity-checked export into a new directory |
| `coverage_reconciliation/__init__.py` | Explicit library exports; no startup behavior |
| `tests/coverage_support.py` | Isolated fictional A2/A3 fixtures |
| `tests/test_phase_a4_reconciliation.py` | Determinism, read-only behavior, basic links, rejection and missing stages |
| `tests/test_phase_a4_uncertainty.py` | Duplicates/context, important literal cues, budgets, service stops, interruption, revocation and malformed claims |
| `tests/test_phase_a4_boundaries.py` | Exclusion provenance, unauthorized approval markers, artifact/storage tampering, physical PDF and no-generation boundaries |
| `tools/evaluate_phase_a4.py` | Before/after comparisons, physical-source-hold characterization and timing/memory measurements |
| This report | Engineering evidence and compatibility assessment |

No `src/`, `clinical_proposals/`, `proposal_editorial/`, existing tests, prompts, configuration, queue entrypoint or database schema changed.

## 3. Source/proposal/generation/storage linking model

The public API is `reconcile_coverage(source=None, inventory=None, proposals=None, workflow=None, *, extraction=None, workspace=None, db_path=None, generation=None, observations=(), limits=None, run_id=None)`. Inputs are current prepared objects plus explicit artifact/database locations. There is no provider argument and no environment-selected runtime path.

`capture_generation_context(provider, config=None)` reads configuration only; it never calls `provider.generate`. It supplies the provider configuration hash, current A3 generation-contract hash, prompt hash and actual A3 limits used. A3's returned object does not persist enough configuration to recover these inputs automatically. If unavailable or incompatible, A4 reports `GENERATION_CONFIGURATION_UNVERIFIED`; it does not guess the candidate identity or rewrite an older checkpoint.

The report schema is `coverage-reconciliation-v1`:

- `source`: full recorded PDF hash, evidence document/canonical buffer identity, evidence fingerprint, full source-map hash and inventory hash. No sensitive source filename is exported.
- `regions`: canonical/native concern identity, basis, exact owner span and native trace spans, page/section references, original inventory state, A4 source state, owner/context proposal IDs, link-verification flag, reviewable/stored links, literal concern cues and controlled reasons.
- `proposals`: A2 ID/category/status/owners/contexts; A3 eligibility/reasons; claimed versus verified generation outcome; request/checkpoint state and local committed-attempt count; resumed flag; candidate/raw hashes; observed candidate count; validation policy/disposition/reasons; persistence state, verified chunk ID and outstanding clinical-review state; duplicate link and verification issues.
- `stages`, `counts`, character partition, review tasks/reason counts and verification issues remain separate.
- `input_state_sha256`, `artifact_state_sha256`, `implementation_sha256` and `reconciliation_id` bind the actual input, relevant artifact/row evidence and A4 implementation.

These are internal records. No traceability field is added to the five clinical content fields or existing JSON envelope. Independent A3 one-chunk package numbering and ordered output are preserved.

## 4. Coverage and completion definitions

| Dimension | Implemented states/meaning |
| --- | --- |
| Source | `owned_by_proposals`, `supporting_context_only`, `existing_source_hold`, `reviewed_nonclinical_exclusion`, `structural_candidate_exclusion`, `not_yet_assessed` |
| Proposal/generation | `draft_generated_review_eligible`, `generated_rejected`, `generated_source_hold`, `held_before_generation`, `insufficient_source_evidence`, `duplicate_deferred`, `request_budget_deferred`, `service_stop_deferred`, `provider_failure`, `interrupted_or_unknown`, `not_started`, `verification_incomplete` |
| Checkpoint | Unavailable/not scheduled/no request record, request failure/unknown outcome, or independently verified published response; damaged/missing records produce explicit issues |
| Persistence | `verified_local_review`, `not_stored`, `readback_incomplete`, `eligibility_revoked`, `reconciliation_required` |
| Clinical verification | `not_clinically_reviewed` or `clinical_review_required`; no authenticated approval authority exists in this integration |

Existing source holds take precedence in the source-state partition, while owner/context IDs remain visible on the same record. Supporting ownership is not lost because a region is held. An existing M3 source-review exclusion record is distinguished from an automatic structural exclusion; observing that record does not authenticate its reviewer or establish clinical approval.

`technical_reconciliation_complete=True` means the automatic-draft traceability scope has verified source/proposal/configuration/run links, accounted per-unit evidence, compatible validation and resolved local persistence evidence. Known, justified holds, duplicates, budget deferrals, rejected candidates and verified absence of storage can be fully accounted. They remain medical coverage gaps. Missing/invalid identity, unknown requests, interrupted runs or unresolved readback prevent this completion flag.

`clinically_complete=False`, `clinically_approved_chunks=0` and `production_ingestion_allowed=False` are literal schema constraints in every result. `independent_clinical_facts` is always unknown (`null`), because proposal/source counts do not prove medical independence. Every extracted character being categorized is not proof that every relevant medical finding was captured.

## 5. Duplicate and context handling

Source regions are joined through indexed maps keyed by exact IDs. Exact source intervals are unioned per buffer; overlapping or reused context is counted once in `context_characters_union`. `context_link_characters` separately reports repeated link volume. `ownership_and_context_characters_union` avoids adding an owner again when it also supplies context.

Canonical characters are partitioned once by source state. Native warning/unmapped/empty-page concern records do not add canonical characters. Overlapping owner/context relationships remain in the ledger instead of being silently deleted.

One owner region with two proposals remains one unique owned region. A2 `duplicate_of` and independently checked generated-content duplicate links reduce `nonduplicate_proposal_units`; they do not create another independent fact. Two eligible candidate events can yield one unique reviewable draft and one stored row. Generated duplicate status is corroborated against the normalized five-field content and its earlier source-ordered candidate, not a matching topic or filename.

An exact duplicate's second source region remains visible with its duplicate link and no separately generated draft. A4 does not silently promote that region to clinically complete.

## 6. Missing-source and missing-knowledge detection

The ledger and review tasks expose regions without proposal links, unassessed source, incomplete extraction/mapping, unresolved context/qualifications, rejected candidates, deferrals, unverified table/figure relationships, citation problems, concepts without reviewable drafts, incomplete local storage and interrupted/unknown requests.

`proposal_links_verified=False` distinguishes an unavailable A2 stage from a verified empty owner set. Such records produce `PROPOSAL_LINK_UNAVAILABLE`, not an assertion that no concept exists. No missing stage is converted into zero generated/valid chunks. `generated_candidates=null` is retained if a required candidate count was not observed; `known_generated_candidates` is a clearly scoped lower-bound observation. Provider failure without a response is not proof that the remote model generated zero candidates.

`verified_stored_review_drafts` counts distinct current chunk IDs actually verified by A4. It is not the database's total row count or the total number of approved chunks. Unknown storage units remain separately visible. Independently verified earlier work can survive a missing workflow-stage link, while overall reconciliation remains incomplete.

## 7. Review task definitions

`ReviewTask` contains `task_id`, `source_revision`, region/span/page references, related proposal/candidate, reason, uncertainty/blocking classification, required review type and current technical outcome. It intentionally has no reviewer, approval timestamp or clinical-decision field.

Task IDs hash source revision, relevant references, proposal/candidate, reason and review type. Changing a task's current outcome alone does not invent a new review identity. With no verified source, the task's source revision is explicitly null and remains an unbound preflight concern.

Review types are source extraction, citation identity, clinical relationship, missing qualification, clinical paraphrase fidelity, document coverage and local storage reconciliation. Representative reasons include `SOURCE_EVIDENCE_HELD`, `UNVERIFIED_TABLE_FIGURE_RELATIONSHIP`, `CONCEPT_WITHOUT_REVIEWABLE_DRAFT`, `CONTEXT_OR_QUALIFICATION_REVIEW`, `GENERATED_CANDIDATE_REJECTED`, `GENERATION_EVIDENCE_INCOMPLETE`, `DEFERRED_OR_INSUFFICIENT_CONCEPT`, `CLINICAL_PARAPHRASE_FIDELITY_REVIEW` and `LOCAL_STORAGE_RECONCILIATION`.

Primary outcomes, safety, contraindications, recommendations/conditional language, low certainty, limitations and applicability are retained as literal-cue concerns. The extra conditional/low-certainty checks are bounded literal matching only. Every region labels these as `literal_cue_not_clinical_priority`; they are not a medical importance ranking or relationship proof. A source may need review even when technical counts reconcile.

## 8. Source identity and tampering protection

A4 reuses `pin_inputs`, existing source/inventory validators and a rebuilt A2 plan. It compares the complete supplied proposal plan, not its schema/ID alone. Prepared extraction text/filename must match the pinned canonical source; citation authority is checked through the existing preflight. Verification concerns the prepared evidence graph and its recorded PDF hash; A4 deliberately does not reread an original user PDF to re-prove physical byte identity.

For A3, the engine validates the typed result again (including false approval flags), exact proposal/eligibility/order sets, source revision, per-unit audit/storage outcomes, and a matching persisted A3 run report. It recomputes candidate keys from exact source/proposal/configuration/prompt/implementation inputs. An optional explicit `run_id` avoids searching more than the bounded report limit.

Request-intent/history records, candidate key, raw checksum and current-policy decision are independently checked. The existing A3 `assess_response` reruns the unchanged deterministic validators against exact selected evidence. Saved verdict text or an asserted `PASS` cannot replace that assessment. Candidate and validation-decision identities are separate; stale policy evidence remains incomplete until a compatible A3 revalidation has actually produced current evidence. A4 never writes that revalidation decision or changes a stored row.

Artifacts are read with size/count budgets and resolved path containment; watched files and observed absences are rechecked for concurrent changes. SQLite opens with URI `mode=ro`, `PRAGMA query_only=ON` and a read transaction. Primary-key reads compare source/package/chunk identity, five fields, content hash, validation/review status and actual local hash-vector values. Revocation is corroborated by the owned A3 source link, failed row and matching quarantine-policy audit record.

SQLite strings such as `APPROVED` are not authenticated approvals and do not count positively. Matching content alone cannot establish a historical package's source revision; such links require reconciliation. Entire forged artifact sets cannot be authenticated by plain SHA-256: these are consistency checks on local evidence, not signed audit evidence or proof of a remote HTTP request. No nonexistent clinical authority was introduced.

## 9. Incomplete and interrupted-run handling

Inputs and stages are optional so preflight failures can be represented. `StageObservation` accepts only failure/incomplete/unknown/not-started observations with controlled codes; it cannot assert success or clinical approval. Conflicting observations remain an issue rather than overriding verified work silently.

Without a valid source inventory, source/proposal/generated counts remain null. If extraction is available but citation fails, the valid source inventory survives and generation remains unverified/not started. A2 failure does not become a verified empty proposal list.

Known auth/quota failure and service-stop deferral are distinct. A committed request without a published response remains unknown, with candidate count null. A3 interrupted results preserve verified independent responses/rows, while their generation stage stays incomplete. Compatible cached responses retain source identity and zero new provider calls. Rejected or stricter-policy-revoked rows are excluded from verified review-storage counts. Local readback failure requires storage reconciliation; A4 performs no compensating write.

## 10. Stage-level status accounting and output

The named stages are `extraction`, `citation`, `source_inventory`, `proposals`, `generation`, `validation`, `storage`, `clinical_review`. Each has availability, state and controlled reasons. States are `not_started`, `incomplete`, `failed`, `unknown`, `completed_verified`.

For a fully observed run with no eligible requests, generation/validation/storage are `not_started`, with known zero published candidates. An unavailable run has unknown counts. A failed provider request can coexist with an independently verified stored draft; the generation stage is failed and unknown candidate counts remain null. Clinical review stays unavailable/not started because no verified authenticated authority exists.

`CoverageReport` exports all source/proposal/region/task/stage records, separate counts and completion flags. New count names are:

```text
source_regions, canonical_characters, proposals, nonduplicate_proposal_units,
unique_owned_regions, owned_characters_union, context_characters_union,
context_link_characters, ownership_and_context_characters_union,
generated_candidates, known_generated_candidates, verified_response_units,
known_eligible_candidate_events, unique_reviewable_drafts, rejected_units,
held_units, deferred_units, unknown_generation_units, resumed_units,
verified_stored_review_drafts, revoked_storage_units, unknown_storage_units,
independent_clinical_facts
```

`attempt_count` is the count of local committed request-history records, including prior invocations. It is not an HTTP-attempt counter. Resumption is separate from publication/validation/storage. No A3 logical-call counter is treated as proof of one HTTP request.

`human_summary(report)` provides concise known/unknown counts and explicitly states that clinical completeness is not established. `write_report(report, new_directory)` writes `coverage.json` and `summary.txt`, verifies the report identity/schema, and refuses an existing directory. It never writes to the supplied A3 workspace or DB.

Example invocation, with deliberately explicit previously captured objects (this code does not select a live provider or process an upload):

```python
from coverage_reconciliation import reconcile_coverage, write_report

report = reconcile_coverage(
    prepared_source, inventory, a2_plan, a3_result,
    extraction=prepared_extraction,
    workspace=explicit_a3_workspace,
    db_path=explicit_local_review_db,
    generation=captured_a3_generation_context,
)
write_report(report, new_report_directory)
```

M1 monotonic timing is reused through `coverage_reconciliation`, `coverage_source_verification`, `coverage_proposal_verification`, `coverage_indexed_join`, `coverage_candidate_verification` and the unchanged validator stages. Timing is external to the deterministic report/identity; no current wall-clock field or new reviewer timestamp is invented.

## 11. Synthetic end-to-end examples

These are actual final evaluator observations from fictional sources/fake providers. Counts are not clinical assertions.

| Fixture | Regions / canonical chars | A2 proposals | Published candidate count | Verified review rows | Technical / clinical completeness |
| --- | --- | --- | --- | --- | --- |
| One protocol, resumed | 2 / 723 | 1 | 1, resumed | 1 | true / false |
| Two protocol concepts, resumed | 3 / 1,438 | 2 | 2, resumed | 2 | true / false |
| Exact duplicate proposal, resumed | 3 / 1,439 | 2; one nonduplicate unit | 1 | 1 | true / false |
| Trial arms/outcomes held | 6 / 533 | 4 over three owner regions | 0 observed in completed held run | 0 | true / false |
| Physical citation-cleaning hold | 5 / 723 | 1 held | 0 observed in completed held run | 0 | true / false |

Additional tests demonstrate two generated duplicate candidates producing one reviewable draft/row, context reuse whose link-volume exceeds its character union, incorrect statistics/references rejected, and one saved success beside a failed auth/quota request whose total candidate count stays unknown.

Machine-readable aggregate: [evaluation.json](D:/gastroagi-chunk-pipeline/tmp/a4/evaluation.json). Complete ledger example: [coverage.json](D:/gastroagi-chunk-pipeline/tmp/a4/p/d5552f/one/coverage/coverage.json). Concise example: [summary.txt](D:/gastroagi-chunk-pipeline/tmp/a4/p/d5552f/one/coverage/summary.txt). `tmp/a4/example-paths.json` locates all four final compatibility examples.

The complete JSON contains only IDs, hashes, codes, counts and span/page/section references. It excludes the fictional source passages and generated prose as well as actual sensitive filenames and provider errors. Clinical text remains in the protected A3 source/candidate artifacts.

## 12. Physical-PDF held-source analysis

The exact A3 synthetic layout was recreated in a temporary PDF: standalone fictional DOI, Methods heading and intact protocol paragraph. The final result verifies the DOI through actual extraction preflight; it does not inject a replacement citation or review attestation.

| Stage | Critical transformation warnings | Canonical chars | DOI in canonical | Clinical body retained |
| --- | --- | --- | --- | --- |
| `extract_pdf` | 1 | 723 | false | true |
| `recover_native_layout` | 1 | 723 | false | true |
| `build_source_map` | 1 | 723 | false | true |

Exact introducing chain: `pdf_extractor.extract_pdf` calls `clean_article_text`; `_is_boilerplate_paragraph` removes the short standalone DOI paragraph. `build_evidence_document` aligns the cleaned-document join to canonical text via `align_buffers(..., 'existing_article_cleaning')`. The non-whitespace deletion is retained and produces critical `text_transformation_unreviewed`. Its recorded deleted source extent is **33 characters**, target extent zero. The warning already exists before native layout recovery/source-map construction; neither introduces a new warning in this fixture.

M3 `_inventory` then retains **two unmapped-native concern regions**, spanning 34 and 31 characters in different native representations. Both reduce to the same DOI after whitespace normalization. They are not 65 extra canonical characters or independent clinical omissions. The inventory has two canonical regions, one warning concern and two native concerns; all five retain source holds because the existing warning-scope propagation conservatively affects the page/document.

The 714-character clinical owner plus nine heading/separator characters remain present in the 723-character canonical buffer. The full body is retained after whitespace normalization; the DOI is retained in raw evidence and verified citation metadata. This fixture demonstrates removal from a clinical buffer, **not destruction of its raw source or observed loss of clinical body text**. The critical warning is conservative about the unreviewed transformation/ownership exclusion. It is still a legitimate existing hold that A4 must not downgrade.

A4 reports the canonical hold partition, native concerns, held proposal, source-review tasks, zero calls/rows and clinical incompleteness. Technical accounting can be complete because every known concern is accounted, not because the source was cleared for generation.

A future separately authorized source-mapping repair is justified: establish a source-backed, explicitly distinguished nonclinical citation exclusion/mapping with retained raw evidence, and prove that adjacent clinical text and corroborating views cannot be dropped. This phase made **no extraction, cleaning, mapping, warning or citation change**.

## 13. Reconciliation timing and memory measurements

Command: `python -B tools/evaluate_phase_a4.py`. Three untraced repetitions per size, followed by one separately timed `tracemalloc` run; identical report objects were asserted. Prepared-source creation, A3 fake generation and fixture DB writes occur before measurement and are excluded. Every measured A4 invocation makes zero model calls.

| Verified drafts | Source regions / chars | Three elapsed samples | Median | Traced peak Python bytes | Separately traced elapsed |
| --- | --- | --- | --- | --- | --- |
| 4 | 5 / 2,879 | 0.102359, 0.099821, 0.110213 s | **0.102359 s** | 2,467,608 | 0.300917 s |
| 16 | 17 / 11,495 | 0.353656, 0.351258, 0.358428 s | **0.353656 s** | 3,374,831 | 1.216448 s |
| 48 | 49 / 34,471 | 1.366244, 1.250617, 1.230821 s | **1.250617 s** | 5,644,612 | 3.502652 s |

These are local synthetic measurements, not Azure latency, PDF extraction throughput, total process RSS or real-document clinical coverage benchmarks. There was no pre-A4 reconciler to compare against. Before/after comparisons instead demonstrate unchanged A1/A2/A3 outputs, candidate IDs and resumption. No speedup to the existing pipeline is claimed.

The join layer uses indexed region/proposal maps, SQLite chunk primary keys and cached per-ID lookups; unions sort intervals. Successful readback does not repeatedly scan the audit table. Revocation corroboration consults the latest matching owned quarantine record only when the row is failed. Existing source/inventory reconstruction and per-candidate defensive evidence copies/revalidation remain necessary costs; they were not replaced by an unsafe cached proof. Runtime on very large native evidence graphs remains bounded by explicit limits and is not established by this small benchmark.

Defaults: 5,000 regions, 256 proposals, 256 drafts, 16,000,000 characters across input model fields, 200,000 input records, 2,000,000 bytes per artifact/readback payload, a 32,000,000-byte artifact-stream budget and a separate 32,000,000-byte storage-readback budget, 32 run reports and 16 committed attempts per unit. Files are reread for the final stability check; these limits do not describe total lifetime I/O. Over-budget inputs/readback yield explicit incomplete verification; source evidence is not silently truncated. All limit/error cases remain review-visible.

## 14. Test commands and actual results

The existing `tests/conftest.py` isolates runtime paths/captured defaults and blocks real Azure clients. New A4 modules block socket connections. The standalone evaluator also prohibits network connections. All A3 setup uses an explicit fake provider and temporary DB/workspace; A4 itself has no generation/storage-write call.

| Execution | Actual outcome |
| --- | --- |
| Pre-edit safe full baseline | **900 passed, 2 deselected, 9 subtests passed**, 67.15 s |
| Initial A4 tests | **7 passed**, 2.07 s |
| Expanded uncertainty tests | **48 passed**, 10.60 s |
| Boundary/physical-hold tests | **62 passed**, 14.82 s |
| Final focused A4 suite | **63 passed, zero failures/skips**, 14.51 s |
| Final safe full regression suite | **963 passed, 2 deselected, 9 subtests passed**, 81.69 s |
| Standalone evaluator | Two executions exited 0; the final measurements/comparisons above describe the final implementation |

No executed A4 test failed. No existing test was edited or weakened. Final JUnit has 972 testcase records (963 tests plus nine subtests), zero failures/errors/runtime skips. Baseline JUnit has 909 records. The two deselected real-source cases and two precollection module exclusions are explicit, not claimed as passes.

Commands from the project root:

```powershell
python -B -m pytest tests/test_phase_a4_reconciliation.py tests/test_phase_a4_uncertainty.py tests/test_phase_a4_boundaries.py -q -p no:cacheprovider --tb=short --basetemp=tmp/a4/t2 --junitxml=tmp/a4/focused.xml -o junit_family=xunit1
python -B -m pytest tests/ --ignore=tests/test_native_layout_real_source.py --ignore=tests/test_task11_regression.py -k 'not both_protected_real_anchors' -q -p no:cacheprovider --tb=short -o junit_family=xunit1 --basetemp=tmp/a4/f --junitxml=tmp/a4/final.xml
python -B tools/evaluate_phase_a4.py
```

All 30 requested categories are covered across A4 tests plus the protected regression suite: single/several drafts; one owner/multiple proposals; proposal/generated duplicates; reused context; held primary outcome/safety omission; conditional/low-certainty content; wrong numbers/reference; extraction/table/figure holds; unassessed source; budget versus service-stop deferral; interrupted/unknown requests; zero-call resume; policy revocation; failed readback; stale source/proposal; missing/corrupt records; preflight without inventory; exact characters; no false clinical completion or approvals; unchanged legacy generation/scheduler/checkpoints; no production ingestion. Additional tests reject forged result/draft/candidate/validation/approval claims, observe an unauthenticated SQLite approval marker without trusting it, detect concurrent artifact changes, enforce readback budgets and refuse to overwrite export directories.

## 15. Before/after regression comparisons

The full A1 comparison matches baseline after excluding its elapsed-time field: eight source cases, 31 variants, all legacy report/status/reason comparisons, five intended examples review-required and three rejected. Fifteen adversarial variants remain 11 rejected/four review-required; all 15 expected reasons remain identified and automatic clinical passes remain zero.

All **20 full A2 proposal outputs** match baseline, including exact source ownership, IDs, contexts, status and accounting. Four A3 scenarios (one, several, duplicate, held) were captured before implementation and replayed after it. Fresh/resumed result status, exact five-field text/envelopes, draft ordering, selected spans, candidate/raw hashes, validation, chunk/storage IDs and disposition match. Every compatible resume in those scenarios makes **zero provider calls**.

The safe full regression run includes existing Milestone 1–5/A1–A3 tests for two-worker refill/drain, retry/error handling, source provenance, immutable checkpoints, resumption, numeric/citation safeguards, local persistence and approval boundaries. New before/after byte checks confirm that A4 does not change its explicitly supplied A3 workspace or SQLite DB.

## 16. Protected file hashes and cache compatibility

**131/131 protected files are byte-identical** to the pre-edit A4 snapshot. Baseline artifacts are in `tmp/a4/base/`: `source/`, `manifest.json`, `versions.json`, `a1.json`, `a2.json`, `a3.json`, `fingerprints.json`; baseline tests are in `tmp/a4/baseline.xml`. Final checks are in `tmp/a4/evaluation.json` and `tmp/a4/final.xml`.

Unchanged fingerprints:

| Identity | SHA-256 |
| --- | --- |
| Existing `validation_fingerprint()` | `79d1298b9b4b56a629f33c3c7581cb0c49719d7715cad135c84be7ffbc8d8b13` |
| A3 generation contract | `cfda60c31d40522eac65d6943350920f95d492ae37ff562a7c3ff1a09931ca1b` |
| A3 validation policy | `642847de7821fd5aa49b8eb49494e7039fab06fad6257f7959634d7f650ed5e1` |

The existing validation fingerprint hashes Python files under `src/`. A4's separate top-level package therefore causes **no existing source/cache invalidation**. No hash algorithm, preparation manifest, immutable candidate format or validation threshold was modified. A4 reconciliation IDs intentionally change when its implementation/input/artifact/storage evidence changes; prior reconciliation artifacts are not rewritten.

Dependencies remain pypdf 6.9.2, PyMuPDF 1.27.2.2, Pydantic 2.13.5, pytest 9.1.1, OpenAI 3.26.1, Playwright 1.62.0 and python-dotenv 1.2.3. The installed Python 3.13 SQLite/Pydantic behavior was checked with actual tests and official [SQLite URI/read-only documentation](https://docs.python.org/3.13/library/sqlite3.html) and [Pydantic model documentation](https://pydantic.dev/docs/validation/latest/concepts/models/). The versioned Pydantic URL was unavailable, so the current official documentation was checked against the installed API. No provider capability or connectivity check was performed.

## 17. Remaining clinical completeness limitations

The engine verifies internal consistency and observed provenance, not medical meaning. A valid stored paraphrase may omit an important qualification that deterministic checks cannot establish. Literal cues do not identify every clinically important topic, prove independent facts or certify article-level coverage. A report with technically reconciled held/excluded/unassessed regions can remain clinically inadequate.

Source-review exclusions in existing models are operator attestations without authenticated identity. SQLite review markers are not an approval authority. A4 deliberately provides no clinical approval input/API and cannot promote any draft. Complete malicious rewriting of the local artifact set is outside unsigned consistency checking; authenticated, tamper-evident approval/audit infrastructure remains future work.

Snapshots describe the state read during the invocation, not perpetual storage validity. Concurrent artifact mutation invalidates verification; a later DB/source/policy change requires a new reconciliation. Missing generation configuration, unavailable A3 run metadata, stale policy decisions, SQLite/schema/permission failures or exceeded budgets remain structured incomplete outcomes. No automatic repair occurs.

## 18. Remaining Milestone 4/5 clinical-readiness requirements

M4 still requires qualified clinician adjudication, broader source/claim relationship verification and editorial-quality acceptance. The original A1 `review_uncertainty`, `conditional_guideline` and `uncertain_dose` examples remain rejected; no fixture, prompt or validator was changed to improve those results. The four review-required adversaries still require clinical review.

M5 still needs separately authorized representative unseen-PDF evaluation and resolution of extraction/citation/layout/OCR ownership limits. The characterized citation-removal warning remains intact. No real PDF queue, existing user PDF, production DB, live Azure output, billed token measurement or real clinical coverage evaluation was used here. M4/M5 and A1 remain **PARTIAL**.

## 19. Recommended Phase A5 real-PDF evaluation scope

A separately authorized A5 should define permitted fresh documents, isolated extraction/cache/storage locations, explicit provider/cost permission if needed, qualified clinical review scope and source/coverage acceptance measures before execution. Include standard research, long reviews, guidance, multi-column/scanned material, tables/figures and interrupted runs. Compare fresh and compatible cached paths without treating historical outputs as fresh-upload benchmarks.

Use A4 ledgers to select missing-outcome/safety/qualification/citation/source-map cases for review. Resolve the citation-only transformation through source-backed exclusion/mapping evidence while preserving corroboration and holds for genuinely ambiguous material. Do not replace clinical adjudication with reconciled region percentages or chunk counts. **No A5 implementation or evaluation on real PDFs was started.**

## 20. Backward Compatibility and Regression Report

| Item | Evidence/result |
| --- | --- |
| Existing files modified | None; all 131 protected files match the pre-edit SHA-256 manifest |
| New files | Eight isolated package modules, four test/support files, one evaluator, this report |
| Baseline/final | 900 → 963 passed; nine subtests in both; same two real-source deselections and two excluded modules; zero failures/runtime skips |
| Provenance/output invariants | Full A1/A2 comparison and A3 fresh/resume text/order/IDs/dispositions identical; stale/tampered links do not establish completion |
| Scheduler/recovery invariants | Existing maximum two workers, immutable candidates, bounded retry/drain, interruption and zero-call resume retained |
| Storage invariants | Existing schema and local hash embeddings; explicit read-only snapshot; no initialization/upsert/quarantine/approval write by A4; fake-DB/workspace byte equality checked |
| Completion/clinical boundaries | Unknown stage counts remain null; context/duplicate counts do not create independent facts; no authenticated approval/clinical completeness/production activation |
| Performance | Three synthetic sizes with actual timing/memory above; no before/after legacy throughput or Azure-speed claim |
| Intentional behavior changes | New explicitly invoked reporting API and new-directory-only export; no existing processing decision changes |
| Risks/unverified assumptions | Clinical semantics, real-PDF coverage, unknown request outcomes, unsigned local artifacts, stale snapshots and authenticated review authority remain limited/unverified |
| Rollback | Stop importing/calling `coverage_reconciliation`. Optionally remove only its new modules/tests/tool/report. Preserve generated reconciliation artifacts for audit. No source restoration, schema rollback, queue manipulation or shared DB deletion is needed. |

No paid requests, Azure/OpenAI connectivity checks, `.env`/deployment/prompt/validator changes, user-PDF processing, queue resumption, production database access, historical checkpoint/chunk mutation, publishing or Milestone 6C/6D work occurred. The production queue was not accessed or activated by this task.

**PASS — Phase A4 offline technical coverage reconciliation is implemented, deterministic, source-bound, read-only and regression-protected. Clinical completeness and approval remain unestablished. Work stops after A4 for independent review.**

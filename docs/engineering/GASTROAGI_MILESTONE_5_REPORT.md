# GastroAGI Milestone 5 — Fresh PDF Reliability and Recovery

Date: 20 October 2026. Scope: Milestone 5 only. Evidence: synthetic PDFs, mocked providers and isolated temporary runtime directories/databases.

Implemented bounded, explicitly reviewed citation recovery and repaired visual recovery accounting/restart behavior. The final offline suite passed **703 tests and 9 subtests**, with **2 real-PDF cases deselected**. There were no final test failures or runtime skips. Two additional test modules were excluded before collection because they reference existing user PDFs.

**Overall status: PARTIAL.** The permitted offline engineering checks pass. This does not establish dependable clinical output for arbitrary unseen medical PDFs or deployed Azure capability. Milestone 4 remains PARTIAL, the new citation-review boundary has no authenticated reviewer UI, and no qualified clinician reviewed these synthetic acceptance results. These limitations are not resolved by saving drafts.

## Prerequisite review and inspected boundaries

Reviewed the current audit and Milestones 1–4 reports against current code/tests. Milestones 1–3 report PASS. Milestone 4 explicitly reports PARTIAL: an opt-in editorial prototype, with five of eight intended paraphrases review-required and three rejected by existing grounding checks. Its clinical readiness prerequisite cannot honestly be described as satisfied. The user's subsequent Milestone 5 request authorized this bounded reliability work; the work does not promote that prototype or change its default status.

Inspected `pdf_extractor`, `native_layout`, `visual_source`, `hybrid_routing`, `generation_preflight`, `preparation_cache`, `chunk_generator`, `package_generation`, `providers/azure` and `queue_recovery`, plus the source/evidence, diagnostics, validation and test boundaries needed to verify them. Applied the project RAG engineering skill.

Findings:

- Citation identity is checked before clinical visual recovery. This correctly prevents paid work on an unresolved article, but also blocks scanned front matter even when its citation is readable in the image.
- Native multi-column reconstruction and pypdf/PyMuPDF corroboration already preserve source ownership. They were retained.
- Package candidates already have source/configuration-bound, immutable checkpoints. Expected later package failures already permit partial draft saving; compatible restart revalidates successful candidates. Tests verify this rather than replacing the mechanism.
- Visual recovery retained old active failures after successful retries. Its paired `future.result()` collection could report the first failure while hiding a second authentication/quota failure. Invalid cached transcription JSON could prevent reuse of another valid pass.
- Queue recovery previously allowed a stray `retryable=True` hint to override a permanent source/review hold.

## Files and functions changed

| File | Changes |
|---|---|
| `src/citation_recovery.py` — new | Typed `FrontMatter`, `CitationPage`, `CitationReadings`, `ReviewedCitation`; bounded `recover_citation_readings`; source/image/attestation verification in `apply_reviewed_citation`. |
| `src/pipeline.py` | Adds optional `process_pdf(..., reviewed_citation=...)` for the draft-package path. Applies and verifies an explicitly supplied or preparation-cached review before existing citation preflight; incompatible reviews fail with `REFERENCE_IDENTITY_UNRESOLVED`. Rebuilds the source map with additive citation buffers. |
| `src/visual_source.py` | Drains both parallel reads; records each known failure with pass number; honors either pass's auth/quota stop. Successful retries move obsolete failures into history. Invalid pass caches are regenerated individually only when network use is allowed; cache-only invalid evidence is held. Sequential failures retain the correct pass number. Unexpected exceptions propagate after draining workers. |
| `src/queue_recovery.py` | `SOURCE_HOLD_CODES` prevents retry hints from overriding source, identity, refusal and content-filter holds. Independent transient failures still trigger the existing retry policy. |
| `src/preparation_cache.py` | Adds citation recovery implementation to the existing preparation identity fingerprint. |
| `src/diagnostics.py` | Counts citation reads in `transcription_calls` and adds `citation_transcription_calls`; all existing HTTP-attempt/token uncertainty accounting remains intact. |
| `tests/test_milestone5_recovery.py` — new | 39 tests/parameter cases, synthetic PDF builders and deterministic mock providers; records sanitized acceptance properties in JUnit XML. |
| This report | Results, limitations and benchmark plan. |

No existing extraction algorithms, generation prompts, package-writing/correction instructions, clinical/numeric validators, source ownership rules, concurrency limit, provider request parameters, deployment configuration, storage schema or clinical approval gates changed. No dependencies were added. No Git repository was assumed or initialized.

## Bounded scanned-citation recovery

The new reader is a separate explicit library operation; normal queue/CLI processing does not start it automatically. A caller must supply its provider and cache directory. It reads only physical front pages 1–2, with two passes per page: at most **four logical image reads**. Rendering is at scale 1.5, capped at 12 million pixels per page. Existing Azure retries, if that provider were deliberately supplied in a future authorized run, remain separate HTTP attempts: four logical calls do not mean four HTTP requests.

The dedicated prompt transcribes front matter verbatim, forbids guessing/completing identifiers and stops at references. The clinical visual transcription prompt is unchanged. Candidate detection excludes reference/bibliography headings, including a heading at the very start of a transcript. Both readings must independently yield the same unambiguous focal DOI using the existing citation parser. Uncertainty, missing identifiers, multiple distinct identifiers or disagreement leave identity held. Bibliographic fields are not reconstructed from guesses; this route supplies a DOI-only reference.

**OCR agreement produces a candidate, not verified article identity.** Applying it requires `ReviewedCitation` with an exact reading ID, an opaque hashed review ID and affirmative confirmations that a human checked the focal article and identifier against the rendered source. These are trusted operator inputs, not authentication of a reviewer. They must not be generated automatically by the transcription model. Test attestations are synthetic test stubs, not claims of real clinical/source review.

Application rechecks the PDF byte hash, evidence PDF identity, prompt identity, record digest, page sequence, rendered image hashes, agreement and native reference compatibility. Native identity conflicts cannot be overridden. Mutated images/PDFs and stale attestations fail. Cached reviews undergo these checks again; a preparation cache is not an identity bypass. Complete pass caches survive an interrupted second read. Invalid cache-only records remain held; an explicitly permitted read replaces only the invalid/missing pass.

Exact citation readings and span-bearing raw buffers remain internal source artifacts. Canonical clinical text and native clinical buffers remain unchanged. A critical source warning remains; citation review is not clinical approval or proof of complete source coverage. There is no automatic DOI lookup, unrelated-reference fallback, citation invention or promotion to production ingestion.

For integrations, keep `recover_citation_readings` inside the existing diagnostics `Observation`/`bind` context to retain its independent timing and request accounting. After actual source review, pass its `ReviewedCitation` to `process_pdf(..., draft_packages=True, reviewed_citation=review)`. The optional path is not wired to an unauthenticated UI or queue action.

## Diagnostic and failure fields

Additions:

- `visual_recovery_outcome.failure_history`: obsolete per-page failures retained separately from active `failures` after retry/recovery.
- `visual_recovery_outcome.failures[].pass_number`: 1 or 2, including sequential second-pass failure.
- `observability.counts.invalid_transcription_checkpoints`, `invalid_citation_checkpoints`, `cached_citation_passes` when those events occur.
- `observability.request_totals.citation_transcription_calls`; `transcription_calls` now includes clinical visual and citation reads.
- Timing stages `citation_recovery`, `citation_transcription_pass`, `citation_review_verification`, using existing monotonic timing/exclusive accounting.
- Internal extraction `reviewed_citation`: source-bound review record, not a sixth clinical content field or a clinical approval.

Existing failure codes are preserved. `TRANSPORT_TIMEOUT` remains transient; authentication/quota remain action-required and stop further page submissions. Source/identity/validation holds, content filtering and refusal cannot become retryable solely because of a conflicting hint. Invalid visual cache data without network permission returns `SOURCE_PASSAGE_HELD`, `retryable=False`. With explicit network permission, only the invalid pass is reread. Schema-invalid new readings remain `MALFORMED_RESPONSE`.

Unexpected worker exceptions are propagated, not silently discarded. Completed pass/package checkpoint files survive. An unexpected orchestration exception may prevent final review JSON publication in that invocation; restart reuses successful checkpoints. This is distinct from expected package/provider failures, which save partial drafts immediately.

No new diagnostic field contains source passages, generated clinical text, filenames, credentials or provider error messages. Private citation records hide transcripts from `repr` and validation error strings. Source artifacts still intentionally contain their evidence and require the same protections as existing preparation caches.

## Fresh synthetic acceptance outcomes

Measurements below are final-run single observations of the actual `process_pdf` call, using `perf_counter`. They include local extraction, planning, mocked requests, validation and persistence. They exclude fixture construction, the separate citation-reader operation and human review time. They are not statistically controlled throughput benchmarks or Azure latency measurements.

All saved candidates below are **REVIEW_REQUIRED**, clinically approved count zero, document complete false. No rows were written to a real database. `Calls` is logical mocked generation + clinical image reads inside `process_pdf`; it is not HTTP traffic. The two separate citation reads for each reviewed scan are stated explicitly.

| Synthetic fixture | Seconds | Saved review drafts | Calls | Package source holds | Held / unassessed document regions | Outcome |
|---|---:|---:|---:|---:|---:|---|
| Standard research | 0.123845 | 1 | 1 | 2 | 5 / 0 | Partial success |
| Nine-page review | 0.498817 | 9 | 9 | 10 | 66 / 0 | Partial success; no eight-chunk cap |
| Conditional guideline, low certainty | 0.115118 | 1 | 1 | 2 | 5 / 0 | Partial success; wording retained |
| Multi-column layout | 0.128210 | 1 | 1 | 3 | 7 / 0 | Partial success; short adjacent material held |
| Numerical findings, population, tolerability and limitation | 0.127883 | 1 | 1 | 2 | 5 / 0 | Partial success; exact qualifiers/numbers retained |
| Table/figure-like structured region beside adequate prose | 0.135243 | 1 | 1 | 3 | 8 / 0 | Prose saved; structured relationship held |
| Damaged numeric qualification beside adequate prose | 0.132971 | 1 | 1 | 3 | 8 / 0 | Intact prose saved; damaged passage held |
| Missing citation | 0.041573 | 0 | 0 | Not planned | Not assessed | `REFERENCE_IDENTITY_UNRESOLVED` |
| Damaged citation | 0.041559 | 0 | 0 | Not planned | Not assessed | `REFERENCE_IDENTITY_UNRESOLVED` |
| Reference-list-only DOI | 0.043769 | 0 | 0 | Not planned | Not assessed | `REFERENCE_IDENTITY_UNRESOLVED` |
| Scanned front matter + native body, after explicit source-review stub | 0.280215 | 1 | 3 | 2 | 6 / 2 | Partial success; plus 2 separate citation reads |
| Fully scanned qualitative source, after explicit source-review stub | 0.343141 | 1 | 5 | 2 | 11 / 0 | Partial success; plus 2 separate citation reads; visual review mandatory |
| Invented numerical claim | 0.122985 | 0 | 2 | 2, plus validation-held unit | No final coverage assessment | `SOURCE_VALIDATION_FAILED`; original + correction rejected |
| Corrupted PDF bytes | 0.016671 | 0 | 0 | Not planned | Not assessed | Extraction stage failure; diagnostics `EXTRACTION_FAILED` |

Package holds and document regions are different inventories, not additive counts or interchangeable coverage percentages. In all reported coverage inventories, represented/supporting/excluded region counts are zero: automatic extraction does not establish reviewed clinical relationships. Saving a source passage does not promote those regions to clinically verified concepts. Preflight failures do not have an assessed coverage inventory; missing values are not zero coverage gaps.

Observed hold reasons in the saved synthetic source maps:

- Research/guideline/statistics: one `insufficient_coherent_source_length` and one `source_heading_or_label`.
- Review: one short region and nine heading/label holds.
- Columns: two short regions and one heading/label hold.
- Table: the usual short/heading holds plus `structured_or_visual_relationship`.
- Numeric damage: the usual short/heading holds plus `damaged_native_text`.
- Scanned front: `no_machine_readable_text_requires_ocr` plus a heading hold. Fully scanned: two native no-text holds remain visible even though a qualitative visual draft is saved.

The adversarial numerical test records **2 generated/assessed/rejected candidate events, 0 accepted events, 1 source-validation-held unit**. The per-unit rejection record count of one is not a claim that there was only one failed candidate event. On restart the validation hold causes zero new generation calls. Existing tests also verify that two OCR passes do not independently prove numbers absent from native source evidence.

## Restart and duplicate behavior

| Two-package scenario | Seconds | Saved drafts | New generation calls | Successful units resumed |
|---|---:|---:|---:|---:|
| Second package times out | 0.165948 | 1 | 2 (one succeeds, one fails) | 0 |
| Resume after timeout clears | 0.099695 | 2 | 1 | 1 |
| Compatible duplicate upload | 0.089974 | 2 | 0 | 2 |
| Identical bytes under a different filename | 0.164205 | 2 | 2 | 0 |

The first review JSON remains byte-for-byte unchanged across later attempts. Duplicate ordered chunks are identical on the compatible cached run. Draft attempts use new review output directories instead of claiming that incomplete document coverage is complete.

Renamed uploads intentionally retain a different source-file identity in existing generation envelopes/checkpoints; cross-filename reuse was not introduced. This can cause redundant generation on a future live renamed upload and remains a cost limitation. Rebinding another filename's immutable provenance without an explicit identity migration would change persistent state semantics. Same-name, same-source/configuration compatible checkpoints demonstrably avoid generation.

Visual tests verify that one successful pass survives a second-pass timeout; resume reads only the missing pass and clears the active failure while preserving history. They also verify two simultaneous failures, including second-pass authentication failure, stop further pages; corrupted JSON replacement leaves the other pass reusable; cache-only invalid JSON stays held; and unexpected worker exceptions drain the successful sibling before propagating.

## Installed provider and API capability assessment

Locally inspected Python **3.13.7**, OpenAI SDK **3.26.1**, Pydantic **2.13.5**, pypdf **6.9.2**, PyMuPDF **1.27.2.2**. The configured API-version value is **2024-12-01-preview**. No endpoint, deployment alias or key is included here. No connection, DNS health check, SDK live client, paid capability probe or real model invocation was performed.

The installed chat-completions signature supports `response_format`, `max_completion_tokens`, `max_tokens` and `timeout`. This is client syntax evidence, not proof that the configured Azure deployment/model accepts each feature. Official [Azure structured-output documentation](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs?view=foundry-classic) requires model/API compatibility; JSON mode and strict schema output are different guarantees. Deployment model/version, image support and context/output limits remain unverified.

Existing generation requests still send only `model` and `messages`. A fake-client test verifies that no schema/token-limit argument was silently added. Timeout remains **120 seconds**, manual retries **2** (up to **3 HTTP attempts**), SDK retries **0**, with existing bounded backoff. The [SDK timeout/retry documentation](https://github.com/openai/openai-python#timeouts) was checked against the installed/fake-client behavior. No timeout policy was tuned from mock latency.

Existing response handling rejects `finish_reason=length` as `OUTPUT_TRUNCATED` even when content happens to be valid JSON; refusal, content filtering, missing and unexpected finish reasons remain distinct failures. No truncated content is repaired or approved. Deterministic tests cover all these cases. Existing M1 tests exercised three timeout attempts with unknown usage and retry-then-success partial token accounting using fake clients/clocks.

Strict JSON schema could reduce structural errors on a supported deployment, but is not clinical/source validation. Output limits require the actual deployed model's supported parameter and budget. Both changes are deferred until authoritative deployment metadata or separately authorized capability verification exists. This work stops before making any paid request. Page rendering uses the documented [PyMuPDF pixmap API](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_pixmap).

## Tokens and measurement limits

Acceptance mock providers return no billed usage or HTTP-attempt metadata. Their token usage and corresponding hypothetical HTTP attempts are **unknown**, not measured zero. Diagnostics correctly mark HTTP/token completeness false for these calls. Observed token counters of zero are an empty observation, not evidence of zero model cost. Zero-network cached cases have no new calls; they do not establish lifetime historical cost.

The citation reader is a separate review preparation operation. Its two mocked passes in the scan scenarios are outside the `process_pdf` call totals and timings above. A dedicated four-read test binds an observation and verifies both citation-specific and total transcription counts, unknown transport metadata, bounded calls and content-free diagnostics.

Persistence timing snapshots written during persistence can precede completion of that timing span; use the complete outer observation for finished persistence durations. The acceptance wall clock measures completion through return. No lifetime ledger, historical billing reconciliation or real Azure speedup is claimed.

## Tests actually executed

Before execution, inspected synthetic fixtures, extraction/recovery/provider tests and autouse isolation. `tests/conftest.py` redirects runtime paths/defaults to per-test temporary directories/databases and rejects real Azure clients unless a fake client factory is supplied. The connection-diagnostic tests use fake HTTP connections. Root live-capable diagnostic scripts were not run as entrypoints. User-PDF test entrypoints were excluded/deselected.

All commands used `python -B -m pytest`, `-q -p no:cacheprovider --tb=short`, isolated `--basetemp=tmp/milestone5/<run>` and `--junitxml=tmp/milestone5/<run>-results.xml`. Runs recording acceptance properties also used `-o junit_family=xunit1`, except the initial development run.

| Run | Selection | Actual result |
|---|---|---|
| Prerequisite | `test_milestone4_editorial.py test_visual_source.py test_ingestion_latency.py test_native_layout_recovery.py test_source_packages.py` | **94 passed, 7.02 s** |
| Initial development | `test_milestone5_recovery.py` | **22 passed, 8 failed, 6.39 s**; six assertions assumed a nonexistent gate key; numeric-damage mock did not match transformed source; start-of-transcript reference heading was not excluded. Twelve JUnit property warnings from xunit2. Corrected fixture/assertions and citation boundary. |
| Development 2 | `test_milestone5_recovery.py` | **30 passed, 5.20 s** |
| Focused | `test_milestone5_recovery.py test_visual_source.py test_ingestion_latency.py test_queue_recovery.py test_milestone1_diagnostics.py` | **89 passed, 15.64 s** |
| Broad regression | `tests/ --ignore=tests/test_native_layout_real_source.py --ignore=tests/test_task11_regression.py -k 'not both_protected_real_anchors'` | **702 passed, 2 deselected, 9 subtests passed, 56.70 s** |
| Final, after cache-only hold fix and added test | Same broad selection | **703 passed, 2 deselected, 9 subtests passed, 48.82 s**; **0 failed, 0 skipped** |

The two ignored files were not collected or executed; their contents include some synthetic tests as well as real-source tests. They are not counted as passed or as runtime skips. The two deselected parametrized cases reference existing user PDFs. No claim of full real-source regression verification is made.

Exact final command:

```powershell
python -B -m pytest tests/ --ignore=tests/test_native_layout_real_source.py --ignore=tests/test_task11_regression.py -k 'not both_protected_real_anchors' -q -p no:cacheprovider --tb=short -o junit_family=xunit1 --basetemp=tmp/milestone5/final --junitxml=tmp/milestone5/final-results.xml
```

Sanitized extracted observations: `tmp/milestone5/acceptance-outcomes.json`; individual case properties and totals: `tmp/milestone5/final-results.xml`. Synthetic runtime artifacts/checkpoints are confined to the test directories. Existing tests cover package concurrency/auth/quota/cancellation, invalid checkpoints, duplicate candidates, source mismatch, local temporary SQLite persistence/readback and M3/M4 relationship/adversarial safeguards.

## Fresh-versus-cached benchmark plan

1. Use only newly constructed synthetic PDFs or a separately authorized, fixed document corpus copied to an isolated workspace. Record byte hashes and synthetic case labels; do not inspect the paused queue or reuse historical runs as fresh evidence.
2. Separate cold preparation + cold generation, warm preparation + cold generation, compatible successful candidate resume, interrupted visual pass resume, permanent validation hold and renamed-file cases. Give each cold replicate an explicitly empty disposable cache/output root. Do not delete or move any existing runtime cache.
3. Run at least five replicates per workload/configuration. Record pipeline monotonic wall time, exclusive/inclusive stage times, source/validation identities, page/region counts, held reasons, accepted/rejected/duplicate candidate events, saved drafts, resumed units and max in-flight calls. Preserve ordered output/source spans/warnings and compare them structurally; do not compare timing alone.
4. Use deterministic slow/fast fake providers and fake retry clocks for orchestration comparisons. Report median/range and sample size. Record physical mock invocations separately from logical calls and simulated transport attempts.
5. For citation recovery, separately measure render/read/cache/attestation verification and record review waiting time separately from CPU/network work. Never combine an already reviewed warm citation with a cold-upload result without labeling that difference.
6. Only after separate live authorization and verified model/API capabilities, measure deployed HTTP attempts, retry delays, prompt/completion/unknown usage, throttling, truncation and provider limits with strict cost bounds. Clinical review and source coverage remain separate outcomes. No such live benchmark was executed here.

## Clinical reliability, compatibility and unresolved risks

The synthetic research/statistics tests assert exact retention of population, treatment/time frame, numerical findings, tolerability and the source's follow-up limitation. Conditional guideline language/low certainty is retained. A numeric invention is rejected after one bounded corrective call and cached as a validation hold. Existing M4 tests cover arm/endpoint swaps, strengthened recommendations, negative/noninferiority findings and invented statistics. These are automated engineering checks using fictional text and predetermined mock responses; **no qualified clinician reviewed output quality**, and they do not measure real-model fidelity.

The five content fields, `chunk_number` metadata and existing JSON envelope remain unchanged. Legacy extractive and optional editorial routes retain their prompts and validation behavior. The citation option is confined to draft-package processing. Omitting it preserves the existing fail-before-paid-reading behavior. Existing human review remains mandatory; every acceptance draft is unapproved and ingestion-disallowed.

Preparation fingerprints now invalidate incompatible old prepared sources on future authorized runs; this can cause local re-extraction. Successful generation checkpoints still require their existing exact source/configuration/reference identity. Validation-held checkpoints continue to use the existing implementation-version policy. No migration or rewrite of historical outputs/checkpoints occurred. New failure-history fields are additive. Rollback consists of stopping use of the optional citation entrypoint and restoring the changed source files from the recorded task baseline; no Git commands or state migration are required.

Remaining limits:

- Milestone 4 editorial readiness and qualified clinical review remain unresolved. This work does not establish complete-document clinical understanding or safe publication.
- The citation review contract is a programmatic trusted-caller boundary. A production reviewer identity/authentication/audit workflow and UI are not implemented. Reading agreement alone must never create an attestation.
- Citation-only recovery without a readable DOI, ambiguous bibliographic ownership, damaged identifiers, tables/figures with unproven relationships, missing passages and uncorroborated scanned numeric findings remain held. No promise that every corrupted/ambiguous PDF can yield chunks is made.
- Synthetic columns and table-like geometry are a limited layout corpus, not broad publisher/general scanned-PDF reliability evidence. Real deployed model vision/schema/output capabilities are unknown.
- Renamed uploads do not share generation checkpoints. Unexpected orchestration failures preserve successful candidate files but may require restart before a final review package is saved.
- Early identity/extraction failures lack a completed document-coverage assessment. The legacy corrupted-PDF result exposes extraction stage with a generic diagnostic code, not a newly invented PDF-format subcode.

## Protected state and final status

A before/after SHA-256 manifest of **15 protected source/prompt/configuration files**, including `.env`, the existing prompts, clinical validation/planning/editorial modules and provider/storage modules, found **zero changed or missing files**. Baseline copies for the edited pre-existing recovery/orchestration files are under `tmp/milestone5/baseline/`. Source syntax and report consistency were checked after writing this report.

This milestone did **not** enumerate/read/hash the existing PDF queue, historical output tree or live database for testing or verification. Therefore it does not claim a fresh database query proves the paused state. No queue worker/service was started, queue pause flag changed, existing PDF/checkpoint/output modified, live database opened, paid request made or production ingestion initiated. Tests used redirected temporary runtime state. Work stops after Milestone 5.

**PARTIAL — offline reliability implementation and its final regression checks pass; Milestone 4's incomplete clinical prerequisite, authenticated source-review integration and unverified real-PDF/deployed-provider reliability remain explicit limits. No clinical approval or production-readiness claim is made.**

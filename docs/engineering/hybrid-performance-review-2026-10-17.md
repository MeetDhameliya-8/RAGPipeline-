# Reusable hybrid PDF ingestion: performance correction

The user requested a reusable long-term ingestion path, not exceptions for two named PDFs. The implementation contains no filename-specific extraction, generation or validation branch. The two reported problem files are regression cases, supplemented by synthetic vector-heavy, scanned, blank, reference-only, mixed-source, cache-integrity and interrupted-processing tests.

## Measured causes

The 15-page regression document contains 13,477 native drawing objects, including 11,652 on one page. Warning insertion scanned the growing warning list for every new drawing. Each drawing's processing route also copied the entire page's warning list. Layout validation then used repeated full Pydantic block comparisons to check page membership. The resulting source-evidence representation grew quadratically, exceeded the unchanged 256 MiB uncompressed storage limit, and prevented database storage after successful generation.

Stack samples confirmed warning insertion as an extraction hotspot. Previous service logs showed roughly 20 minutes between initial extraction and generation planning on restart, followed by several more minutes of repeated checks and an output-writing failure. Neither successful Azure connectivity nor a longer timer could resolve that local representation problem.

## Implemented changes

- `src/evidence.py`: an ephemeral warning-ID index speeds insertion. Public evidence equality excludes the derived index. Warnings retain their original IDs, locations, classifications, critical flags and unresolved state.
- `src/native_layout.py`: a localized route references its own block warnings and applicable page-wide warnings. Full page routes still retain all page warnings. Every drawing, localized warning and document-level coverage warning remains present. Page/block membership uses ID sets rather than repeated model-list comparisons. This changes bookkeeping growth from quadratic to linear for the measured vector-heavy case.
- `src/preparation_cache.py`: separate native and visual preparation checkpoints with PDF-byte, filename, implementation and installed dependency fingerprints. Visual checkpoints also bind the Azure deployment configuration. Complete metadata/source files are published before their checksummed manifest. A changed source, implementation, dependency or relevant provider setting invalidates reuse; damaged/incompatible entries fall back to preparing the source again. Existing source-map loading recomputes evidence and optional package proofs before accepting cached data. No pickle or executable cache is used.
- `src/hybrid_routing.py`: explicit page recovery decisions distinguish witnessed native packages, damaged/sparse source text, unresolved native prose corroboration, unresolved paragraph order, reference lists and other review holds. Lack of a generated chunk alone is not the routing criterion. Pages with unresolved prose still receive recovery; readable-looking text is not treated as a layout certificate. Cached transcriptions on other pages remain reusable without authorizing a new API request.
- `src/visual_source.py`: at most two simultaneous transcription requests, using the same configured Azure deployment and existing per-request retry/backoff. Page rendering and evidence assembly remain sequential, preserving the per-page pixel budget and deterministic pass order. Both source passes are still checked. A completed pass survives failure of the other pass. Cache JSON is published atomically. Cache-only reuse never calls the provider.
- `src/pipeline.py`: uses verified completed preparation before re-extraction, records preparation timing/reuse and page-route diagnostics, and avoids loading both native and visual checkpoints when the completed visual checkpoint suffices. Dry runs remain offline and do not create preparation checkpoints.
- `src/package_generation.py`: progress counts visited source packages rather than regeneration attempts, so completed-package progress cannot exceed the planned package total.

The source package eligibility/witness rules, clinical generation prompt, five content fields, numerical/claim/citation checks, hard source-length limits, rejection quarantine, human-review requirement, SQLite storage verification and production upload block remain in force. An experimental native-witness adjustment was not retained. Performance gains are not obtained by silently declaring ambiguous pages complete or weakening a clinical check.

The storage size cap was not raised. No dependencies, external OCR vendor or production backend contract were added. Uploaded PDFs, completed packages, database rows and earlier candidate/transcription caches were preserved. A local database/queue backup was taken before stopping the worker.

## Measurements

The 15-page native preparation completed in about 20.15 seconds with cProfile enabled after the drawing-bookkeeping fix; a later unprofiled native preparation/checkpoint write took about 11.07 seconds. Profiling overhead and different cache states mean these are distinct measurements, not a single universal timing claim.

The 70-page file's image-selection count changed from 69 to 51. Eighteen reference-list pages avoid new image reading. Unresolved prose and paragraph order continue to receive recovery. Saved page transcriptions from the interrupted earlier run remain available.

Final replay with the actual Azure generation/transcription methods replaced by network-blocking guards:

| Case | Preparation reused | Total replay | Preparation | Resumed candidates | Stored review drafts | Uncompressed source map |
|---|---|---:|---:|---:|---:|---:|
| 15 pages, first measured replay | Native + visual | 7.86 s | 2.19 s | 9 | 10 | 34,194,417 bytes |
| 15 pages, repeated replay | Native + visual | 7.97 s | 2.23 s | 9 | 10 | 34,194,417 bytes |
| 70 pages, first measured replay | Native + visual | 16.48 s | 4.99 s | 59 | 57 | 23,820,444 bytes |
| 70 pages, repeated replay | Native + visual | 15.68 s | 4.58 s | 59 | 57 | 23,820,444 bytes |

All rows used existing preparation/generation work and made zero actual Azure calls. They are restart/replay measurements, not first-upload completion promises. The script labels its first pass `cold`; its recorded reuse flags establish that this is **not an empty-cache cold start**. For the 70-page document, eight uncached generation/regeneration requests were blocked by the offline guard. Its stored subset and read-back succeeded, but that replay did not finish the outstanding source work. The 15-page case required no new generation/transcription requests and stored all ten accepted drafts.

Source maps loaded successfully after saving. SQLite read-back passed for both replayed packages. Whole-document clinical coverage remains incomplete; no clinician approval or production publication is claimed.

## Verification and continuation

Synthetic tests cover linear warning-link and serialized-size bounds at 100 and 400 drawings, drawing/warning retention, legacy/new evidence equality, checkpoint checksum/source/provider invalidation, native/reference/blank routing, unresolved paragraph recovery, bounded concurrent passes, cache-only network prohibition, one-pass outage recovery and pipeline restart skipping extraction/generation.

The full regression suite passed 518 tests with seven fixture-dependent skips and nine subtests before the final checkpoint-load optimization. The extra fixture skips relative to earlier milestones reflect the user's requested runtime-PDF cleanup and different uploaded corpus. Final regression and live continuation results are recorded below.

The corrected service recovered the 15-page queue job into the local review-ready state, preserved the 70-page document's 57 stored partial drafts, and resumed the remaining source packages through the existing queue. Runtime profiles, stack samples, backups and detailed replay results remain in ignored `tmp/hybrid-performance/`; medical outputs remain in ignored output storage.

Fresh scanned or layout-damaged documents still require paid Azure processing. Two-request concurrency does not remove the number of source checks. Authentication, quota, timeouts, malformed/unreadable inputs and unresolved clinical evidence can still require bounded recovery or review. General clinical accuracy and a universal maximum processing time are not established by these tests.

API usage was previously authorized by the user. Actual cost has not been reconciled against Azure billing. Implementation references consulted: official [Python profiling](https://docs.python.org/3.13/library/profile.html), [concurrent futures](https://docs.python.org/3.13/library/concurrent.futures.html) and [Pydantic serialization](https://pydantic.dev/docs/validation/latest/concepts/serialization/) documentation. The installed runtime and actual local tests determined compatibility.

## Final live and regression verification

- Final full suite after the checkpoint-load optimization: **518 passed, 7 skipped, 9 subtests passed in 43.64 seconds**. All skips are missing runtime/source fixtures; they are not counted as passes. Targeted checkpoint/queue/visual tests also passed after the last functional change.
- The actual live 70-page queue job resumed through saved source and candidate work, attempted the outstanding generation/regeneration work against the authorized Azure deployment, and reached `LOCAL_REVIEW_READY` in **86.12 seconds**, with **63 stored review drafts** and verified database/embedding read-back. The result remains a partial clinical/source package, not a complete-document approval.
- A subsequent real cached 15-page recovery/read-back completed in **7.91 seconds**, retaining **10 stored drafts**. Its queue state is review-ready. This uses the same pipeline/cache/storage helpers and actual source checks; no Azure request was needed for its compatible completed units.
- At the final snapshot: **7 review-ready jobs, 1 active extraction, 22 queued, 0 FAILED and 0 ACTION_REQUIRED**. All seven ready-package read-backs passed, totaling **108 stored review drafts**. The five previously ready jobs retained their IDs, statuses and stored counts. No production write occurred.
- The server was restarted with the corrected build and the queue resumed. `/admin` and `/bot` return HTTP 200. Remaining uploads continue automatically, including a different 124-page guideline. Its first-upload completion time has not been measured in this review.

The two fast completion numbers are explicitly **recovery/resumption timings with earlier work reused**. A new PDF without those caches must still pay its native extraction and necessary API latency. These results must not be presented as eight seconds or 86 seconds for every fresh PDF.

Detailed live evidence: ignored `tmp/hybrid-performance/final-verification.json`, `live15-verification.json`, `replay-results.json`, queue/database backups and profiling artifacts. Source text, generated medical content and credentials are not copied into this engineering report.

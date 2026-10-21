# Ingestion latency and source rejection — 2026-10-19

The bulk queue remains paused. The user authorized live verification of the three previously rejected PDFs. Runtime PDFs, clinical outputs, diagnostics and database snapshots remain in ignored folders.

## Root causes

The three previous failures made 73, 59 and 30 generation requests although their citation metadata could not pass reference validation. Every request failed at that boundary. Spacing differences, publisher citations with an inline DOI, and expanded/abbreviated journal names caused false identity conflicts or unresolved identity. The 124-page job spent about 989 seconds preparing evidence, and about 1,478 seconds overall. The 30-page job took about 587 seconds overall.

The planner also missed a standalone REFERENCES heading inside a larger native block. It submitted bibliographic entries for clinical drafting. An empty model response was classified as a schema error and could trigger unnecessary operational retries.

## Implementation

- `generation_preflight.py` checks source identity before paid image recovery or generation, and again at the direct package-generation boundary. Genuine ambiguity blocks with zero new provider calls.
- Citation comparison normalizes spacing/case. A DOI labelled after a complete front-matter publisher citation can establish ownership. When bibliography differs but one focal DOI is explicitly source-supported, only that DOI is retained; disputed bibliography is cleared and marked for review. Explicit conflicting DOIs remain blocked. No outside facts are introduced.
- `prompts/gastroagi_source_package_rules.md` describes passage-level drafting, exact source wording and the five fields. It contains 1,917 characters versus 8,293 in the article prompt. Strict whole-article generation and custom prompt paths remain supported.
- Admin generation runs at most two independent package requests concurrently, submitting only one bounded group at a time. Workers own their counters/candidate files. Merging preserves source order, deduplication and ownership. Authentication/quota errors stop further groups. Direct callers default to sequential behavior.
- Request provider time and pipeline generation wall time are recorded separately from preparation. Concurrent request durations are not document wall time.
- Source-package plan v2 holds bibliography after standalone headings embedded in native blocks. Identical owned source passages retain candidate IDs. Persisted v1 plans still recompute under their original rules; historical review artifacts remain readable.
- A typed empty response with the correct source filename and integer zero count becomes `SOURCE_PASSAGE_HELD`. It stores no content, cannot pass clinical validation, is checkpointed and is not automatically retried as a service error. Malformed empty responses remain errors.

Native witness criteria, paired visual corroboration, source/numeric/reference validation, rejection quarantine, database read-back, clinician review and the production upload block remain enforced. No deployment, API route, reasoning setting, OCR vendor or dependency changed.

## Measurements and limits

The first 30-page retest finished in 168.45 seconds: preparation 31.48 seconds, generation 132.67 seconds, 32 stored drafts with verified read-back. It reused page transcriptions but generated against the new prompt (61 requests including corrections). This predates the reference-boundary correction and is not an empty-cache benchmark.

The corrected reference boundary reduced its plan from 59 to 32 packages. Its next run finished in 62.00 seconds, storing 32 drafts with verified read-back, three new generation/correction requests and 31 resumed candidates. Two rows created during the first retest from reclassified packages were explicitly held out of retrieval. Their source artifacts and audit history were retained.

An offline native/plain extraction experiment on two-page samples from three documents improved some line matches, but did not increase witnessed source packages or reduce image recovery. It was not shipped. It does not justify dropping evidence checks to report faster completion.

Focused tests cover reference ownership/conflicts, zero-call preflight, two-request concurrency, deterministic ownership, resumption, authentication stopping, typed abstention and v1 compatibility. One full-suite run hit an existing six-second FIFO test deadline while live verification was consuming CPU; the final quiet-suite outcome is recorded below.

Long scanned or layout-damaged first uploads still incur visual-recovery/API latency. Successful storage does not establish complete PDF coverage or clinical approval. Recovery timings must not be advertised as first-upload guarantees.

Implementation references: Python 3.13 concurrent.futures documentation and pypdf extraction documentation. Installed APIs and deterministic/live tests determine compatibility. Detailed results remain in ignored `tmp/latency-2026-10-19/`.
## Final live verification

All three previously rejected documents produced stored review drafts with verified database read-back:

| Document size | Main retest | Stored drafts | Preparation | Generation |
|---|---:|---:|---:|---:|
| 30 pages | 62.00 s | 32 | 30.95 s | 27.16 s |
| 124 pages | 408.21 s | 57 | 154.58 s | 245.75 s |
| 76 pages | 127.69 s | 27 | See runtime report | See runtime report |

Every row reused earlier page transcriptions. The 30-page row also resumed 31 generation candidates. The larger two runs performed new generation against the corrected source identity and compact prompt. These timings do not establish fresh-upload performance.

The 124-page run retained 14 source-validation failures, one typed source hold and one malformed response as explicit per-unit outcomes. The 76-page run retained two source-validation failures and one source hold. No failing passage was treated as approved content.

Validation rejections now have compatible per-unit hold checkpoints after the bounded correction attempt. These bind source/prompt/provider identity plus an implementation fingerprint; a rule change invalidates reuse. A cached hold stores no clinical content. The sixteen actual failed validation outcomes from the main live runs were checkpointed from their saved failed repair assessments, rather than paying to rediscover them. A subsequent 124-page recovery completed in 19.47 seconds, with one provider request, 57 resumed candidates, fourteen resumed validation holds, 57 stored drafts and verified read-back. Its remaining malformed response became a source hold, so it no longer schedules a transient retry.

Final quiet regression suite: **532 passed, 7 fixture-dependent skips, 9 subtests passed in 47.68 seconds**. Focused boundary tests also passed. The earlier FIFO deadline failure occurred during simultaneous live CPU work; the final full run passed without changing that test or its deadline.

The failed-source hold mechanism is an explicit stop for clinical/source review, not a claim that missing coverage was solved. All generated chunks remain drafts. The nineteen unrelated queued uploads remain paused.

Final storage audit: all thirty uploaded PDFs and all 111 original chunk content hashes were preserved. Eleven ready-package source maps and database/embedding read-backs verified. The database contains 227 review drafts available to local retrieval and two explicitly held rows excluded from retrieval. Queue state is eleven local-review-ready jobs, nineteen queued jobs, zero active jobs, and paused=true. Production writes remain zero. The Admin/BOT server is online with the bulk queue paused.

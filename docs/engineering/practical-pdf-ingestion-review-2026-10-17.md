# Practical PDF drafting, recovery and local retrieval — implementation review

2026-10-17. This follows the user's subsequent request to fix the real PDF → generated chunks → local database → answer workflow, including explicit authorization to use the necessary approach and live Azure tests. It extends beyond the earlier P1-C2.2 review checkpoint. No production backend integration or clinical approval is claimed.

## Verified root cause

All 30 original queue failures had `EXTRACTION_BLOCKED`: zero eligible independent clinical-concept units reached Azure. They were not API reading-time failures. The strict planner remains available, but independent clinical-concept certification is no longer confused with permission to draft from witnessed source prose in the local review workflow.

## Implemented behavior

The Admin uses practical source-package drafting by default. The CLI exposes the same path with `--draft-packages`; existing strict CLI and opt-in multi-view paths remain available.

Native drafting preserves PDF identity, original and alternate evidence, exact native spans, unique agreement from both retained extraction families or ordered native-line witnesses, source warnings and explicit held regions. Complete source sentences can survive damage elsewhere in a block. Packages group adjacent compatible native prose under bounded, verified reading order. Damaged glyphs/operators, unresolved table/figure relationships, ambiguous overlap/rotation, insufficient source length and unclassified material remain held. No synthetic whole-document buffer or invented clinical facts is used to fill a length target.

Only when native packages are unavailable, the configured Azure deployment can read rendered source page images. Its PNG-image route was verified by an actual request; PDF-file input support was not assumed. Visual recovery is bounded to three pages and two transcriptions per page, with a rendering pixel budget. Source PDF hashes are checked before and after rendering. Source page images and both immutable transcriptions are retained in a local cache. Only unique paragraphs matching exactly after whitespace normalization in both transcriptions can become visual draft evidence. The two passes use the same deployment; their agreement is not independent clinical verification. Original native buffers and all unresolved warnings remain intact.

Visual drafts have explicit page/image/transcription provenance and mandatory source-review status. Numerical claims are checked against retained native evidence as well as the transcription. A number appearing only in two model transcriptions does not pass the native numerical check. Unknown source references are never invented. Unreadable, encrypted, unsupported, uncorroborated or citation-unverifiable documents can still require source review rather than produce a trusted answer.

The existing Azure-only adapter, strict five-field generation schema, citation binding, actual claim/numerical validators, 90-word hard minimum, output writer, SQLite review store and answer retrieval are reused. Every practical draft is `REVIEW_REQUIRED`. No failed chunk becomes an approved clinical record.

Generation now retains immutable per-package candidate attempts, failures and validation reports. Compatible completed candidates resume without another provider call. Source, prompt, provider configuration and input fingerprints prevent incompatible reuse. A failed package does not discard earlier completed packages. Rejected candidates receive at most one source-constrained regeneration, requesting coherent source wording without new recommendations or numerical claims. Passing subsets survive; rejected candidates remain quarantined. This is LLM regeneration, not a raw-text replacement presented as model generation.

Queue progress shows actual package progress and elapsed time. It distinguishes stored drafts from a source-review hold. The Admin displays Passed / Review and stored counts so a review draft is not presented as clinically approved. Historical output folders are preserved; practical retries create separate review packages and use stable attempt caches.

## Files and contracts

- `src/source_packages.py`: `NativeTextWitness`, `SourceHold`, `SourcePackage`, `SourcePackagePlan`, deterministic native packaging and full read-back recomputation; visual paragraph references remain separate from native certificates.
- `src/visual_source.py`: typed transcription/provider/page-evidence interfaces, bounded page rendering, two-pass comparison, immutable local extraction cache and unresolved visual-source provenance.
- `src/package_generation.py`: existing generator integration, request isolation, resumable compatible candidates, actual per-package validation, bounded regeneration and rejection quarantine.
- `src/providers/azure.py`: configured-deployment image transcription using the existing transport/retry/response safeguards. No new provider, deployment, SDK or external OCR service was introduced.
- `src/adaptive_ingestion.py`, `src/chunk_generator.py`, `src/pipeline.py`: practical drafting configuration, existing pipeline orchestration, targeted visual fallback and bounded source validation.
- `src/source_map.py`, `src/evidence_storage.py`: optional package plan persistence, original warnings/holds and explicitly incomplete coverage.
- `src/chunk_validator.py`: bounded per-chunk source packets, mandatory draft/visual review findings and extra native numerical checks for visual text.
- `src/local_rag_chat.py`: Admin default route, source-review status, package progress and clear stored/review counts.
- Tests: native generation/storage/retrieval, immutable recovery/resumption, one bounded correction, visual agreement/disagreement, cache/identity tampering and unsupported visual numerical claims.

## Test isolation incident and correction

An existing autouse queue test called `_reset_processing_queue_for_tests` against the real local database. Running that suite cleared the active queue. The attempted database backup already contained no queue rows, so the original queue UUIDs could not be restored. The 30 recovery filenames were reconstructed from the preserved `EXTRACTION_BLOCKED` output/source-map artifacts and enqueued as new recovery jobs. Original PDFs, historical failure artifacts and stored source chunks were retained.

`tests/conftest.py` now isolates local runtime globals and captured path defaults in per-test temporary directories. The reset helper refuses the real user database. A regression verifies that refusal. Subsequent full suites ran while the real recovery queue remained intact. This incident is reported explicitly; preservation of the original queue IDs is not claimed.

## Clinical and operational limits

Stored content remains a local review draft. There is no clinician approval, production API write, new ChromaDB collection, new embedding model or production retrieval claim. The existing local deterministic hashing/retrieval system is used. Not every source concept, table, figure or page is represented by the surviving packages. Coverage stays incomplete and all missing/uncertain evidence remains visible.

A universal promise that any PDF and any question will succeed would be false. The implemented behavior recovers usable source-backed drafts, continues after localized failures and identifies unreadable or unverified material explicitly. Two agreeing extractors or transcriptions can share errors; human source/clinical review remains necessary. General clinical error rates are unmeasured.

The official [file-input documentation](https://developers.openai.com/api/docs/guides/file-inputs) describes text and image processing for compatible routes. The implementation separately verified the actual configured Azure image route and kept its existing endpoint/API version. No assumption about capabilities was substituted for that live check.

Final live queue, database, retrieval and regression measurements are appended below.

## Final live verification

| Measure | Verified result |
|---|---:|
| Originally failed queue PDFs reprocessed | 30 |
| Final queue state | 30 LOCAL_REVIEW_READY; 0 FAILED; 0 pending |
| Stored reviewable draft chunks across these packages | 115 |
| Independent post-run SQLite/embedding read-back checks | 30/30 |
| PDFs requiring the targeted visual fallback | 2 |
| Visual-derived drafts among the stored set | 4 |
| Real visual-cache resumption | 2 PDFs; 0 transcription calls; 0 generation calls |
| Protected historical files modified | 0/302 |
| Incorrect complete-document claims | 0/30 |
| Clinical approvals / production writes | 0 / 0 |

Each stored chunk was checked against its package's actual validation report and belonged to PASS or REVIEW_REQUIRED, then its database row and embedding were read back. Every current source-map PDF hash matched the current input PDF bytes. All source maps loaded and recomputed their package evidence successfully. Rejected candidates were not included in these stored files.

An actual HTTP request to the running `/chat` endpoint returned `ANSWERED_FROM_LOCAL_REVIEW_DB` with 4 citations. Its answer mode was `extractive_synthesis`: the application used its source-bound extractive synthesis rather than allowing an unsupported generated answer. This demonstrates working retrieval/answer assembly, not independently adjudicated medical answer accuracy.

Both `/admin` and `/bot` returned HTTP 200 after restart. The Admin's Passed / Review column was confirmed in the served HTML. The service remains on `http://127.0.0.1:8766` with the final practical drafting, bounded regeneration and visual fallback code.

The three-PDF live pilot preceded the recovery batch and stored seven drafts. Those pilot PDFs are separate from the 30 recovered queue items; they are not added to the 30-PDF success denominator. The previously reviewed 30-PDF engineering corpus and this user-upload queue also differ; their results are not conflated. Paired visual transcription agreement and passing deterministic checks do not count as human medical adjudication.

API calls were authorized for this task. Actual cost was not independently reconciled against Azure billing. Visual requests use the same configured Azure deployment and API version. There were no external OCR vendors or production database writes. Native source holds, visual uncertainty, partial coverage, source-grounding warnings and human review remain visible.

### Per-PDF stored results

| Source PDF | Stored drafts | Source route | Read-back |
|---|---:|---|---|
| `PIIS0016508515010860_1792213118_1.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508516000044_1792213118_1.pdf` | 1 | Native + visual recovery | Verified |
| `PIIS0016508516002936_1792213118_1.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508516349265_1792213118_1.pdf` | 2 | Witnessed native prose | Verified |
| `PIIS0016508516353902_1792213118_1.pdf` | 6 | Witnessed native prose | Verified |
| `PIIS0016508517301312_1792213118_1.pdf` | 1 | Witnessed native prose | Verified |
| `PIIS0016508517359668_1792213118_1.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508517360663_1792213118_1.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508518345700_1792213118_1.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508518350935_1792213118_1.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508518354349.pdf` | 7 | Witnessed native prose | Verified |
| `PIIS0016508519335553.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508519335735.pdf` | 4 | Witnessed native prose | Verified |
| `PIIS0016508519335802.pdf` | 8 | Witnessed native prose | Verified |
| `PIIS0016508519356859.pdf` | 6 | Witnessed native prose | Verified |
| `PIIS0016508519367149.pdf` | 5 | Witnessed native prose | Verified |
| `PIIS0016508519412468.pdf` | 6 | Witnessed native prose | Verified |
| `PIIS0016508519413437.pdf` | 7 | Witnessed native prose | Verified |
| `PIIS0016508519415266.pdf` | 4 | Witnessed native prose | Verified |
| `PIIS0016508520305837.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508520349581.pdf` | 3 | Native + visual recovery | Verified |
| `PIIS0016508520350022.pdf` | 5 | Witnessed native prose | Verified |
| `PIIS0016508521030699.pdf` | 8 | Witnessed native prose | Verified |
| `PIIS0016508522001020.pdf` | 2 | Witnessed native prose | Verified |
| `PIIS0016508522002384.pdf` | 3 | Witnessed native prose | Verified |
| `PIIS0016508522005042.pdf` | 2 | Witnessed native prose | Verified |
| `PIIS0016508522006096.pdf` | 4 | Witnessed native prose | Verified |
| `PIIS0016508522011635.pdf` | 4 | Witnessed native prose | Verified |
| `PIIS0016508523002585.pdf` | 2 | Witnessed native prose | Verified |
| `PIIS0016508524003627.pdf` | 1 | Witnessed native prose | Verified |

Detailed runtime evidence: [post-run verification](../../tmp/final-live-ingestion-verification.json), [zero-call visual cache proof](../../tmp/real-visual-cache-verification.json), and the actual [answer check](../../tmp/live-answer-check.json). Source text, transcriptions, rendered pages and generated chunks remain in ignored runtime storage, not repository documentation.

### Regression verification

Targeted source/visual/generation/queue/storage safeguards: **71 passed in 13.59 s**. The full offline suite passed **496 tests**, with **2 original fixture-dependent skips** and **9 subtests**. The five original NEJM PDFs remain unavailable, so their original release checks remain unresolved. A final full run after the cache-integrity assertion is recorded separately below.

All 30 recovered documents still have incomplete source coverage and require clinician/source review before clinical approval or production publication. Universal success for arbitrary unreadable, encrypted, uncorroborated or citation-unverifiable PDFs is not claimed.

Final full-suite command after all source/cache/queue changes: `python -m pytest -q -p no:cacheprovider --tb=short` — **496 passed, 2 skipped, 9 subtests passed in 64.01 s**. The active local queue remained intact throughout this run.

Page rendering was checked against the installed PyMuPDF API and the official [Page.get_pixmap documentation](https://pymupdf.readthedocs.io/en/latest/page.html#Page.get_pixmap), and exercised on actual source pages and synthetic scanned-page tests. No dependency changes were required.

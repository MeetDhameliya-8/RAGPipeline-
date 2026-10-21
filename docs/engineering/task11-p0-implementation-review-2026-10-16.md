# TASK11 P0 implementation review

Date: 2026-10-16. This is the requested review checkpoint before P1.

P0-A through P0-D are implemented around the existing Azure-only generator. No canonical evidence framework, persisted generation-unit recovery, OCR, external document service, extra model, production ingestion or ChromaDB change was introduced. Topic, Subtopic, Section, Reference and Original Text remain the only clinical content fields. All new diagnostics and evidence information is separate metadata.

## 1. Exact change boundaries

| Increment | Files/functions changed | Before → after |
|---|---|---|
| P0-A | `src/chunk_generator.py`: `GeneratedArticleModel`, `GeneratedChunkModel`, `GeneratedDocumentModel`, `parse_model_json`, `_unique_json_object`, `validate_generation_schema`, `normalize_generation_result`, `_lower_key_dict`, `_generate_single_batch` | Content coercion/count replacement/extra-envelope loss → strict original types, keys and declared counts before normalization; duplicate JSON keys, alias collisions and nonpositive/duplicate operational numbers reject. Missing clinical content cannot be filled. Missing references enter controlled binding; absent operational numbers are assigned locally. Malformed article objects produce typed `ChunkGenerationError`. |
| P0-B | `src/pdf_extractor.py`: `_normalize_for_citation_matching`, `extract_document_reference_metadata`, `_select_reference_metadata`, `_reference_metadata_compatible`; `src/chunk_generator.py`: `generate_chunks`, `build_generation_input`, `_verified_reference_from_extraction`, `_validate_generated_reference_against_source`, `_citation_candidates`, `_citation_components`, `_citation_components_match`, `validate_generation_content` | DOI/page concatenation and metadata self-validation → protected DOI tokens; source-supported/unresolved/conflict identity; raw page spans; independent source checking; focal DOI/bibliography/issue ownership checks. Labeled DOI candidates in a separate layout column survive joined-column text. Unlabeled inline cited identifiers and competing identities cannot authorize binding. No new filename or journal-specific repairs. |
| P0-C | New `src/numeric_evidence.py`: `NumericEvidence`, `numeric_evidence`, `local_numeric_findings`, `_quantity_context`; `src/chunk_validator.py`: `_validate_numbers_against_source`, `source_claim_issues`, `_validate_source_grounding`, `_contradicts_source`, `_negative_result`, `_endpoint_terms`, `_is_claim_sentence`, `_recommendation_negation_conflict`, `_source_support_sentences`, `_source_sentence_records`, `_best_supported_source_sentence`, `_build_source_evidence` | Global number presence and loose contradiction matching → localized numeric evidence, explicit ambiguity, sign/operator/unit checks in aligned propositions, endpoint/scope/named-arm matching and recommendation-negation checking. Validation continues after early warnings. Non-exact claims remain review-required. Exact spans identify their full-text buffer hash. A bounded memory-only sentence index avoids repeated document tokenization. |
| P0-D | `src/providers/base.py`: `ChunkGenerationError`, `GenerationResponse`; `src/providers/azure.py`: `AzureChunkGenerator.generate`, `_create_chat_completion`, `_extract_chat_message_content`; `src/chunk_generator.py`: `generate_chunks`, `_generate_single_batch`; `src/pipeline.py`: `main`, `process_pdf`, `build_generation_diagnostics`, `build_pipeline_failure_report`; `src/local_rag_chat.py`: `build_chunk_count_summary`, `_terminal_queue_status_for_result` | Preflight text called “sent,” lost earlier candidate counts and incomplete CLI totals → separate plan/attempt/candidate/schema/source-PASS/review/rejected/stored/approval counts and explicit unknown counts. Sanitized Azure fingerprints, finish outcome, response ID, token usage and transport attempts accompany ingestion generation. Truncation/refusal/filter/malformed completions reject even if text is parseable JSON. Partial CLI jobs are counted and return exit 2; hard failures return 1. Zero read-back is authoritative. |

Additional files:

- New regressions: `tests/test_p0_raw_schema.py`, `tests/test_p0_citation_identity.py`, `tests/test_p0_clinical_evidence.py`, `tests/test_p0_accounting.py`.
- Existing tests adjusted: `tests/test_generator.py` now supplies complete raw metadata, independent citation issue evidence and normal provider finish reasons; `tests/test_adaptive_ingestion.py` supplies the DOI in independent source evidence; `tests/test_task11_regression.py` now expects extractor conflicts to block identity. The five original NEJM citation expectations and ABC-02 source assertions remain intact.
- New `tools/replay_reliability.py`: read-only extraction/saved-candidate benchmark. It never calls generation and writes metrics only to a new explicitly supplied destination; it refuses to overwrite one.
- Updated behavior documentation: `docs/architecture.md`, `docs/clinical-validation.md`, `docs/azure-integration.md`, `README.md`.

“Source-supported” means deterministic checks found source support; it does not mean bibliographic verification or clinician approval. Existing minimum-only dependencies were preserved; no new dependency was added.

## 2. Test-first execution and results

| Increment | Defect reproduced before correction | Passing checkpoint |
|---|---|---|
| Raw schema | Initial new tests: 45 failed, 1 passed. Duplicate-key rejection also reproduced before its fix. | Relevant tests: 104 passed, 2 skipped, 4 subtests. Full suite then: 286 passed, 2 skipped, 9 subtests. |
| Citation identity | Initial new tests: 10 failed, 1 passed. Later legacy-ownership, unassociated bibliography and layout-column cases also failed before correction. | Relevant tests: 81 passed, 2 skipped, 4 subtests. Full suite then: 297 passed, 2 skipped, 9 subtests. |
| Clinical evidence | Initial new tests: 13 failed, 5 passed. Leading decimals, named arms, empty evidence, quantity context and recommendation negation each reproduced before correction. | Initial relevant tests: 66 passed. Full suite then: 315 passed, 2 skipped, 9 subtests. Final clinical/validator/stability targeted run: 72 passed. |
| Accounting/outcomes | Initial new tests: 10 failed. Unknown candidate counts, missing finish reason and explicit zero read-back reproduced before correction. | Initial relevant tests: 104 passed, 9 subtests. Full suite then: 325 passed, 2 skipped, 9 subtests. Final accounting/pipeline targeted run: 37 passed, 5 subtests. |

**Final complete suite: 338 passed, 2 skipped, 9 subtests passed in 22.20 seconds.** No tests were deselected. The cache provider was disabled to avoid the earlier cache permission warning.

Commands used:

```powershell
python -m pytest -q -p no:cacheprovider tests/test_p0_raw_schema.py
python -m pytest -q -p no:cacheprovider tests/test_p0_citation_identity.py
python -m pytest -q -p no:cacheprovider tests/test_p0_clinical_evidence.py
python -m pytest -q -p no:cacheprovider tests/test_p0_accounting.py
python -m pytest -q -p no:cacheprovider --tb=short
python -m tools.replay_reliability --metrics <new-runtime-metrics-path.json>
```

Runtime checked: Python 3.13.7, pypdf 6.9.2, PyMuPDF 1.27.2.2, openai 3.26.1, Pydantic 2.13.5. Strict mode was verified against the installed package and [official Pydantic guidance](https://pydantic.dev/docs/validation/latest/concepts/strict_mode/). Azure ingestion response checks follow [official completion/refusal guidance](https://developers.openai.com/api/docs/guides/structured-outputs). The SDK exposes `response_format` and both token-limit parameter names; exact configured deployment support remains unverified, and native structured output is not enabled.

The two skipped tests need the original NEJM PDFs. A read-only search of local attachments, Downloads, Documents and Desktop, including 23 ZIP inventories, found none of the five exact filenames. A generic two-page NEJM file had none of their article identifiers and was not substituted. No external download or paid request was attempted. Complete release regression coverage therefore remains unverified.

## 3. Protected PDF findings

The historical baseline remains **30 PDFs / 350 pages; 21 REVIEW_REQUIRED and 9 FAILED saved packages**. Before edits, fresh extraction plus old-validator replay of the unchanged saved candidates produced **22 REVIEW_REQUIRED / 8 FAILED**. That differs from the historical reports because extraction had already changed before this milestone.

Final P0 clinical replay produces **18 REVIEW_REQUIRED / 12 FAILED**, evaluated independently of earlier extraction/reference gates. Five previously failing fresh-baseline packages now require review instead of failing; nine warning-only fresh-baseline packages now fail, primarily because later recommendation/grounding statements are no longer skipped after an earlier warning. These are engineering findings requiring human adjudication, not proof of clinical correctness or harm.

Observed numerical glyph/superscript disagreements remain review-required; missing values and explicit conflicts are not silently repaired. Correct endpoint distinctions are not treated as contradictions merely because another endpoint has a negative result. Old nonempty references are never silently rewritten to a different citation.

**SHA-256 preservation check: all 302 original files under input_pdfs, processed and output were unchanged.** No original PDF moved, and no saved package was overwritten. No live Azure requests, production writes or clinical approvals occurred. Final extraction/clinical replay took 73.82 seconds; live model time/cost was not measured. No baseline timing was captured, so no comparative speed claim is made.

Stage counts overlap when tested independently: **9 extraction blockers, 14 saved-reference identity failures, 12 clinical-check failures**. Raw schema replay rejects none of the 30 existing packages. Applying gates in order gives **9 extraction blocks, 11 additional reference blocks, 5 additional clinical blocks, and 5 review-only draft packages**. Generation/transport outcomes remain untested live.

| PDF stem | Fresh old-validator replay | P0 clinical replay | Saved reference | Sparse visual route | First applicable replay stage |
|---|---|---|---|---|---|
| PIIS0016508511011073 | REVIEW_REQUIRED | FAILED | source-supported | no sparse blocker | clinical_validation |
| PIIS0016508512006737 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508512006762 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508512011869 | REVIEW_REQUIRED | REVIEW_REQUIRED | source-supported | blocked | extraction |
| PIIS0016508512013169 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508512015430 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | blocked | extraction |
| PIIS0016508513006999 | FAILED | FAILED | source-supported | blocked | extraction |
| PIIS0016508513008445 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508513008469 | FAILED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508513014078 | REVIEW_REQUIRED | FAILED | blocked | blocked | extraction |
| PIIS0016508513015266 | FAILED | REVIEW_REQUIRED | source-supported | no sparse blocker | draft_review |
| PIIS0016508514000766 | REVIEW_REQUIRED | REVIEW_REQUIRED | source-supported | blocked | extraction |
| PIIS0016508514006568 | FAILED | REVIEW_REQUIRED | source-supported | no sparse blocker | draft_review |
| PIIS0016508514007252 | REVIEW_REQUIRED | REVIEW_REQUIRED | source-supported | no sparse blocker | draft_review |
| PIIS0016508514009007 | REVIEW_REQUIRED | FAILED | blocked | no sparse blocker | reference_identity |
| PIIS0016508514012086 | REVIEW_REQUIRED | REVIEW_REQUIRED | source-supported | blocked | extraction |
| PIIS0016508515001547 | REVIEW_REQUIRED | REVIEW_REQUIRED | source-supported | no sparse blocker | draft_review |
| PIIS0016508515001997 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | blocked | extraction |
| PIIS0016508515004485 | REVIEW_REQUIRED | FAILED | source-supported | no sparse blocker | clinical_validation |
| PIIS0016508515005764 | REVIEW_REQUIRED | FAILED | source-supported | no sparse blocker | clinical_validation |
| PIIS0016508515010860 | REVIEW_REQUIRED | FAILED | blocked | no sparse blocker | reference_identity |
| PIIS0016508516000044 | REVIEW_REQUIRED | FAILED | blocked | no sparse blocker | reference_identity |
| PIIS0016508516002936 | REVIEW_REQUIRED | FAILED | source-supported | no sparse blocker | clinical_validation |
| PIIS0016508516349265 | REVIEW_REQUIRED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508516353902 | REVIEW_REQUIRED | REVIEW_REQUIRED | source-supported | no sparse blocker | draft_review |
| PIIS0016508517301312 | FAILED | FAILED | source-supported | no sparse blocker | clinical_validation |
| PIIS0016508517359668 | FAILED | REVIEW_REQUIRED | source-supported | blocked | extraction |
| PIIS0016508517360663 | FAILED | FAILED | source-supported | blocked | extraction |
| PIIS0016508518345700 | FAILED | REVIEW_REQUIRED | blocked | no sparse blocker | reference_identity |
| PIIS0016508518350935 | REVIEW_REQUIRED | FAILED | blocked | no sparse blocker | reference_identity |

Remaining clinical failures: eleven documents have source-grounding/recommendation failures (1011073, 3006999, 3014078, 4009007, 5004485, 5005764, 5010860, 6000044, 6002936, 7301312, 8350935, using abbreviated filename suffixes); 7360663 has a source-fidelity failure. These failures require source review. The 14 reference failures retain old nonempty bibliographic identities that cannot pass current focal-source checks, including concatenated DOI/page suffixes; they are not repaired automatically.

These columns are separate diagnostic replays, not successful end-to-end jobs. A clinical REVIEW_REQUIRED status cannot override an earlier extraction or reference blocker. No generated clinical text is copied into this document.

## 4. Known clinical and operational limits

1. Local lexical matching does not prove all endpoint, arm, comparator, timeframe, population or recommendation-strength relationships. Non-exact material associations remain REVIEW_REQUIRED. Deterministic PASS is never approval.
2. Legible extracted signs/units may still be wrong. No symbol or dose is restored from medical expectations. Scientific notation, complex equations, unusual units, multi-page tables and superscript geometry need richer evidence and reviewed fixtures.
3. Citation identity remains heuristic source support. Explicit labels, front-matter scope and conflict checks improve attribution but do not replace bibliographic review. A nonempty old DOI with an appended page suffix is rejected rather than “fixed” silently.
4. Sparse visuals still block nine documents. Dense figures can evade the old sparse-page heuristic. Columns, table/header/footnote binding, reference truncation and missing sections remain P1 concerns.
5. Generation-unit results are still held in memory; a later unit failure does not return a complete package. P0 preserves accounting, not durable unit recovery. Existing force/failed-package replacement remains a P1 preservation risk and was not invoked against protected packages.
6. Output reuse is not yet compatible with every prompt/model/extractor/validator change. The offline replay deliberately bypasses reuse and never writes existing packages.
7. Completion diagnostics added here cover nonstreaming ingestion generation. Existing review-assistant streaming, production backend contracts, publication/version rollback and live deployment budgets are separate boundaries.
8. There is no clinician-adjudicated held-out corpus, complete NEJM release run, or approved live Azure benchmark. Offline test success is not a clinical-fidelity or production-readiness claim. No Git repository was available for a commit/diff identity.

## 5. Proposed P1 boundaries

After this review checkpoint:

1. Adapt the source map with a shared sidecar: PDF-byte identity, extraction/config version, document-scoped pages/blocks, named raw/clean buffers, reversible spans, reading-order/region metadata and unresolved warnings. Do not replace the framework.
2. Map generation ownership to source-anchored sections/concepts with explicit population, arm, endpoint, timeframe, table/figure/footnote and qualification dependencies. Reconcile generated/reviewed/excluded/unresolved concepts before calling the document complete.
3. Retain extraction candidates and selection reasons; introduce feature-driven native/layout routing with protected tests. Keep critical image-only evidence blocked. OCR and external services need separate authorization and validation.
4. Persist immutable unit attempts and validation outcomes atomically. Resume unfinished compatible work, revalidate saved candidates without unnecessary regeneration, invalidate relevant source/evidence/prompt/model/config changes, and never mix incompatible attempts or delete previous work.
5. Define Azure capability and full request/output budgets only after approved exact-route verification. Evaluate native structured outputs without replacing local clinical checks.

Add independently selected scanned/mixed, different-journal, multi-column, table-heavy, figure-heavy and long-guideline PDFs with clinician-adjudicated evidence. Measure preserved/related/unresolved/omitted evidence, unsafe accepts, false rejects, coverage, recoverability, time and cost. Synthetic/mocked success is not evidence of cross-format clinical generalizability.

## 6. Updated as-built versus target

| Boundary | As built after P0 | Remaining target |
|---|---|---|
| Extraction | Existing hybrid native extraction/blockers; citation-specific raw spans | Canonical evidence, region/column/table/figure relations and conditional routing |
| Planning/generation | Bounded paragraph/sentence contexts; Azure only; unchanged five fields | Source-anchored concepts, dependencies and measured clinical coverage |
| Raw schema | Strict original types/keys/counts, controlled aliases/binding, typed errors | Provider-native schema only after deployment verification |
| Source identity | Protected DOI parsing, source-supported candidates/spans, conflict and ownership checks | Richer identity and explicit reviewer adjudication of unusual layouts |
| Clinical validation | Local numeric evidence, uncertainty routing, aligned conflicts, scoped contradiction checks; no early-warning short circuit | Full relational evidence and held-out sensitivity/false-alarm assessment |
| Diagnostics/status | Observed/unknown counts, sanitized completion metadata, partial CLI exit 2 | Durable request/unit lifecycle and verified deployment budgets |
| Recovery | Existing draft subsets and queue restart; truthful earlier-unit counts | Compatible per-unit persistence, immutable attempts and atomic package promotion |
| Approval/publication | Mandatory human gate; no inferred approvals; production blocked | Verified approval/backend/staging/retrieval/publication/rollback contracts |

**Additional-model decision:** no extra model or agent is justified now. Deterministic defects and missing evidence explain the demonstrated failures. A future evidence-citing verifier must show measured benefit, acceptable false alarms, reviewer burden and cost on an adjudicated held-out set. It cannot override missing evidence or approve itself.

**Review stop:** P0 is complete. P1 canonical evidence and persisted recovery have not started. Missing NEJM fixtures and unperformed approved live/clinical evaluations remain explicit release gaps.

# GastroAGI TASK11 — Verified Failure Audit

**Evidence basis:** Supplied source files, three additional test files, the Admin Panel screenshot, five original NEJM PDFs, and the proposed TASK11 flowcharts.

**Important:** These findings are based on source inspection and original-article review. The current application has not been executed as part of this handoff.

## 1. Observed Admin Panel Failures

Five PDF jobs were shown as failed, with no stored chunks.

| Source PDF | Observed failure |
|---|---|
| NEJMoa0808145.pdf | Chunk 1 missing `reference` |
| NEJMoa0903230.pdf | Chunk 1 missing `reference` |
| NEJMoa0904492.pdf | Chunk 1 missing `reference` |
| NEJMoa0908721.pdf | Chunk 5 reports unsupported numbers `66.5`, `73.5` |
| NEJMoa0908821.pdf | Chunk 1 missing `reference` |

The Admin Panel showed zero generated, validated, and stored chunks. This reporting is not sufficient to prove that Azure returned zero candidate chunks.

## 2. Verified Finding F-01: Citation Binding Is Not Deterministic

**Files:** `src/chunk_generator.py`, `src/pdf_extractor.py`

The observed generation path is:

`build_generation_input()` → provider generation → `parse_model_json()` → `validate_generation_schema()` → `normalize_generation_result()` → `validate_generation_content()`.

`build_generation_input()` includes detected citation metadata and instructs the LLM to repeat that citation for every chunk.

However, `normalize_generation_result()` does not fill individual chunk references from an authoritative source record.

`validate_generation_content()` rejects blank reference fields.

An absent reference key is rejected earlier by the Pydantic schema. A present but blank reference produces the observed content-validation error.

**Consequence:** An otherwise usable generated chunk can fail because the model did not repeat bibliographic metadata already available to the application.

**Caution:** `extract_reference_metadata()` derives candidate citation metadata from document text. Its output must be checked against the actual article before being treated as verified.

**Recommended action:** Separate source citation verification from LLM text generation. Deterministically bind verified article-level references while rejecting missing authority and conflicting citations.

## 3. Verified Finding F-02: Numeric Matching Uses Cleaned Source Text

**Files:** `src/chunk_generator.py`, `src/chunk_validator.py`

The generation-stage `_validate_generated_numbers_against_source()` builds a number set using `NUMBER_RE.findall(source_text)`.

The source text supplied by `validate_generation_content()` is `extraction["full_text"]`.

The later validator `_validate_numbers_against_source()` also checks numeric presence in its supplied source text.

The existing check does not establish that a number belongs to the correct clinical endpoint, treatment arm, population, or timeframe.

**Consequence:** It can reject a genuine source number omitted during cleaning. Conversely, it can accept an incorrect clinical association when the numeric value appears elsewhere in the document.

**Recommended action:** Compare raw page evidence, cleaned source text, and local clinical context. Never resolve numeric disagreements by simply disabling the check.

## 4. Verified Finding F-03: ABC-02 Numeric Values Exist in the Original PDF

**PDF:** `NEJMoa0908721.pdf`  
**Article:** Valle et al., *Cisplatin plus Gemcitabine versus Gemcitabine for Biliary Tract Cancer*.  
**Citation:** N Engl J Med. 2010;362:1273–1281.  
**Location:** PDF page 4, journal page 1276, Results → Treatment Compliance.

The article reports:

- `66.5%`: Patients receiving three cycles of gemcitabine alone during the first 12 weeks.
- `73.5%`: Patients receiving four cycles of cisplatin plus gemcitabine during the first 12 weeks.

These are treatment-compliance findings, not overall-survival rates.

**Verified:** Both numbers are present in the original PDF.

**Not yet verified:** Whether the installed `pypdf` extractor, cleaner, and validator actually see them, and whether the failed generated Chunk 5 assigned them correctly.

**Required investigation:** Trace the values through raw page extraction, cleaned page text, `full_text`, Azure request context, and the validator's source text.

## 5. Verified Finding F-04: Generation Errors Prevent Downstream Candidate Accounting

**File:** `src/pipeline.py`

`process_pdf()` calls `generate_chunks(extraction)`.

When `generate_chunks()` raises `ChunkGenerationError`, processing returns a generation failure before `CHUNK_BATCH_COMPLETED` and the normal `validate_chunks()` path.

The successful generated-document object is not returned to the orchestrator.

**Consequence:** The Admin Panel may display zero generated chunks despite the provider having returned a parseable draft before a later check rejected it.

**Recommended action:** Separate candidate generation, schema assessment, source verification, and acceptance status. Preserve an auditable count of candidates even when final validation fails.

Do not equate “candidate generated” with “valid clinical knowledge.”

## 6. Verified Finding F-05: Rejected Evidence Can Incorrectly Complete a Source Section

**File:** `src/source_map.py`

`mark_processed_sections_from_validation()` derives linked section IDs from `validation_report["source_evidence"]`.

It does not check that the corresponding chunk has an acceptable validation status before marking the section `processed`.

**Consequence:** A rejected chunk with linked evidence can incorrectly make a section appear completed, potentially compromising checkpoint resumption and coverage reports.

**Recommended action:** Make completion status contingent on explicit eligible chunk states and coverage confirmation. Unknown or missing chunk status must not be treated as approval.

## 7. Verified Finding F-06: Section Planning Does Not Yet Control Generation

**Files:** `src/source_map.py`, `src/pipeline.py`, `src/chunk_generator.py`

A section map and `section_batches()` capability exist.

The observed orchestration still calls `generate_chunks(extraction)`, and `build_generation_input()` constructs a full cleaned-article prompt.

**Consequence:** Section plans and checkpoint records do not yet establish independent resumable generation units.

**Recommended action:** Integrate bounded section generation only after immediate citation and extraction failures are stabilized, while retaining document-level evidence context where needed.

## 8. Verified Finding F-07: Production Indexing Is Not Connected

**Files:** `src/local_rag_chat.py`, `src/local_chunk_store.py`

`database_upload_status()` reports `BLOCKED_UNVERIFIED_BACKEND`.

The available local review database is SQLite with local hashing-based embeddings, not a verified production ChromaDB ingestion path.

**Consequence:** Successful local chunk generation must not be described as production RAG integration.

**Recommended action:** Preserve this boundary. Production backend discovery and integration require a separate milestone and explicit permission.

## 9. Other Observable Risks

The supplied PDFs contain two-column layouts, figures, tables, references, footers, line-wrap hyphenation, and occasional figure-production artifacts.

These make clinical source preservation and context-aware numerical verification necessary.

Existing tests use many synthetic source strings. Some validator tests provide generated chunk text itself as `source_text`, which cannot independently demonstrate source grounding.

The tests establish useful isolated behavior but do not substitute for a five-PDF end-to-end benchmark.

## 10. Items Still Requiring Investigation

- Installed dependency versions and actual provider capabilities.
- The exact Azure request/response contract and prompt contents.
- Whether failed provider responses contain blank references, absent keys, or both.
- Whether ABC-02 values survive the installed extraction path.
- Whether PDF cleaning removes critical clinical paragraphs or table context.
- Whether retries preserve checkpoints without duplicate generation.
- Whether the existing validation status semantics are sufficient for safe candidate storage.
- Whether the actual GastroAGI backend is compatible with the five-field output.

## 11. Architecture Decision

**Do not add multiple autonomous agents at this stage.**

Prefer a bounded, deterministic repair strategy with one existing Azure generation stage:

Verified citation → reliable extraction → candidate generation → strict schema validation → source-grounding verification → bounded targeted recovery → human review.

Add model-assisted secondary checking only if a controlled benchmark demonstrates a specific need.

The priority is to correct data provenance and validation boundaries, not to make failed outputs pass by weakening checks.
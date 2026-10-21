# GastroAGI TASK11 — Five-PDF Regression and Acceptance Plan

**Test scope:** PDF extraction, source citation, Azure generation interface, clinical schema validation, numerical grounding, checkpoint recovery, and local workflow reporting.

**Execution policy:** Offline and mocked tests by default. Live Azure and production ingestion require separate explicit authorization.

## 1. Required Source Fixtures

Use the original five PDFs supplied for the GastroAGI audit.

Keep copyrighted source documents in a properly authorized local fixture directory. Do not automatically commit restricted PDFs into a public repository.

Suggested local fixture directory, if compatible with existing project conventions:

`tests/fixtures/nejm/`

Fixture names may be normalized for testing, but original source identity and checksums must be retained.

| Fixture | Reference | Clinical regression focus |
|---|---|---|
| NEJMoa0808145.pdf | Shaheen et al. NEJM 2009;360:2277–2288 | Barrett dysplasia, radiofrequency ablation |
| NEJMoa0903230.pdf | van der Gaag et al. NEJM 2010;362:129–137 | Preoperative biliary drainage versus early surgery |
| NEJMoa0904492.pdf | Colombel et al. NEJM 2010;362:1383–1395 | SONIC treatment groups and week-26 remission |
| NEJMoa0908721.pdf | Valle et al. NEJM 2010;362:1273–1281 | ABC-02 outcomes and exact compliance percentages |
| NEJMoa0908821.pdf | van Santvoort et al. NEJM 2010;362:1491–1502 | PANTER primary composite endpoint and mortality |

Do not substitute external summaries for the actual uploaded original-article evidence.

## 2. Extraction and Provenance Tests

For each PDF:

- Confirm readable pages and expected bibliographic identity.
- Confirm nonempty raw extraction and cleaned text.
- Preserve original page numbers and section associations.
- Identify suspiciously missing, scrambled, duplicated, or truncated content.
- Preserve numerical findings along with their clinical context.
- Flag tables and figures whose content is not reliably captured.
- Ensure citation discovery identifies the focal article, not a study merely cited in its bibliography.

For the ABC-02 article, specifically check PDF page 4.

Verify that `66.5` and `73.5` are detectable in the original source and determine precisely whether the current installed extraction and cleaning processes retain them.

Record the result at each stage:

1. Raw page text.
2. Cleaned page text.
3. Full cleaned article text.
4. Source-section record.
5. Azure generation input.
6. Numerical-validator source representation.

**Pass:** Critical evidence is retained with provenance or the document is explicitly held for extraction review.

**Fail:** Critical evidence is silently omitted while the pipeline reports clean extraction.

## 3. Reference Binding Tests

### R-01: Missing reference key with verified citation

Mock Azure JSON with one otherwise valid chunk that omits `reference`.

Expected: The controlled pre-final-validation enrichment stage attaches the authoritative verified citation and final validation passes.

### R-02: Blank reference value

Mock `reference: ""` with independently verified article metadata.

Expected: The verified citation is attached deterministically.

### R-03: Capitalized reference key

Mock `Reference` rather than `reference`.

Expected: Supported key normalization works without introducing duplicate fields.

### R-04: No verified citation

Provide incomplete or conflicting source metadata.

Expected: No reference is fabricated. The chunk cannot pass final validation.

### R-05: Conflicting DOI

Provide a reference containing a DOI belonging to another article.

Expected: Conflict is rejected or sent to explicit source-identity review.

### R-06: Citation from bibliography

Put another paper's citation in a reference list.

Expected: It must not replace the focal article's identity.

### R-07: Other required fields missing

Omit `original_text`, `topic`, or another clinical field.

Expected: No reference-enrichment logic silently fills unrelated missing content.

## 4. Clinical Numerical Integrity Tests

### N-01: ABC-02 treatment compliance

Required associations:

- Gemcitabine alone: `66.5%`, three cycles, first 12 weeks.
- Cisplatin plus gemcitabine: `73.5%`, four cycles, first 12 weeks.

Expected: Both findings are accepted only when the correct evidence and group assignment are preserved.

### N-02: Reversed treatment groups

Intentionally swap the percentages or treatment descriptions.

Expected: Validation does not accept the incorrect association merely because both numbers occur in the article.

### N-03: Fabricated number

Introduce an unsupported clinical percentage.

Expected: Explicit unsupported-number failure.

### N-04: Evidence lost during cleaning

Supply raw page evidence containing a true number but cleaned text without it.

Expected: Extraction/cleaning discrepancy is recorded. It must not be automatically labeled as a fabricated clinical claim.

### N-05: Number in a different endpoint

Reuse a genuine source number while assigning it to an unrelated endpoint.

Expected: The incorrect claim fails grounding or requires clinical review.

### N-06: Valid formatting variation

Test supported variations in spaces, punctuation, hyphenation, and numeric formatting.

Expected: Meaning-preserving normalization works without merging distinct values or units.

### N-07: Table-only evidence

Use a number found exclusively in a table not reliably extracted.

Expected: Extraction review or quarantine; no unsupported automatic approval.

## 5. Five-Paper Clinical Assertions

Use these as source-verification anchors, not as instructions to force the generator to output fixed sentences.

### Barrett's esophagus — Shaheen

- Low-grade dysplasia eradication at 12 months: 90.5% with ablation versus 22.7% with sham control.
- High-grade dysplasia eradication: 81.0% versus 19.0%.
- Do not confuse dysplasia eradication with intestinal metaplasia eradication or cancer incidence.

### Pancreatic-head cancer — van der Gaag

- Serious complications: 39% with early surgery versus 74% with preoperative biliary drainage.
- Preserve the trial population and the distinction between overall serious complications and surgery-related complications.

### Crohn's disease — SONIC

- Week-26 corticosteroid-free remission: 56.8% with combination therapy, 44.4% with infliximab alone, and 30.0% with azathioprine alone.
- Preserve the immunosuppressant/biologic-naive study population.
- Do not confuse clinical remission with mucosal healing.

### Biliary tract cancer — ABC-02

- Median overall survival: 11.7 months with cisplatin plus gemcitabine versus 8.1 months with gemcitabine alone.
- The treatment-compliance values 66.5% and 73.5% have separate meanings from survival outcomes.

### Necrotizing pancreatitis — PANTER

- Primary composite outcome of major complications or death: 40% with the step-up approach versus 69% with open necrosectomy.
- Mortality was 19% versus 16%, without a statistically significant difference.
- Do not incorrectly claim that the trial demonstrated a mortality reduction.

All assertions must be checked against the original source before being treated as validated results.

## 6. Chunk and Source Checkpoint Tests

### C-01: Rejected chunk with evidence span

A rejected chunk is linked to a valid source section.

Expected: Section is not marked successfully processed.

### C-02: Passing chunk with evidence span

A passing chunk is linked to a source section.

Expected: The section may be completed only if its required concept coverage is satisfied.

### C-03: Unknown chunk status

The validation report lacks a corresponding acceptable chunk status.

Expected: Fail closed; do not assume approval.

### C-04: Partial document

Some sections are validated and others rejected or deferred.

Expected: Completed work is retained, missing coverage is reported, and the document is not falsely marked fully complete.

### C-05: Changed source

The PDF content or source mapping configuration changes.

Expected: Incompatible checkpoints are invalidated.

### C-06: Duplicate retry

The same generation unit is processed again after an interruption.

Expected: Stable identity, no duplicate accepted chunks, and correct audit trail.

## 7. Orchestration and Admin Panel Tests

### O-01: Chunk 5 rejected

Mock a generated document containing several candidate chunks, with Chunk 5 invalid.

Expected: Candidate counts and per-chunk outcomes are recorded accurately. No rejected candidate becomes approved or searchable.

### O-02: Malformed provider output

Expected: Parse failure, bounded recovery, and explicit diagnostic category.

### O-03: Azure timeout / quota / authorization

Expected: Correct classification; only transient failures receive bounded retries; no provider fallback.

### O-04: Output writing failure

Expected: Explicit failure and safe preservation of previously valid artifacts.

### O-05: Failure and retry

Expected: Failed units are distinguishable from previously completed units; no false progress or duplicate work.

### O-06: Production database boundary

Expected: `BLOCKED_UNVERIFIED_BACKEND` remains effective unless a separately verified and authorized backend integration is implemented.

### O-07: Local review database

Expected: Draft storage is never presented as explicit clinical approval or production publication.

## 8. Test Execution

Start by inspecting the repository's real dependency environment.

If the tests remain in the existing `tests/` layout and pytest is installed, a targeted command is:

`python -m pytest tests/test_generator.py tests/test_pdf_extractor.py tests/test_validator.py tests/test_source_map.py tests/test_pipeline_helpers.py tests/test_local_rag_chat.py -q`

Then run the complete repository test suite using the documented project command, for example:

`python -m pytest -q`

If test dependencies, browser tooling, or optional integrations are unavailable, report them explicitly. Do not mark those tests as passed.

Do not issue real Azure requests merely because pytest tests are present.

## 9. Acceptance Gate

The repair may be considered ready for clinician review only when:

- All four observed citation-failure cases have a safe, tested resolution.
- ABC-02 values are correctly traced through extraction and validation.
- Fabricated numeric data and wrong clinical associations still fail.
- Each generated chunk has the five-field schema.
- Source evidence and reference identity are traceable.
- Invalid chunks cannot complete checkpoints.
- Candidate counts and job states are accurate.
- Partial failures remain non-destructive.
- Unapproved chunks cannot enter production retrieval.
- All regression tests have been executed and documented.

## 10. Final Required Test Report

For each of the five PDFs, record:

- Pages extracted.
- Critical extraction warnings.
- Verified article citation.
- Candidate chunks generated.
- Schema-valid chunks.
- Source-supported chunks.
- Quarantined/review-required chunks.
- Source coverage status.
- Actual local storage status.
- Production publication status.
- Remaining clinical review requirements.

The benchmark should not rely on an arbitrary target number of generated chunks.

**Success means reliable, auditable, source-faithful processing—not merely zero error messages.**
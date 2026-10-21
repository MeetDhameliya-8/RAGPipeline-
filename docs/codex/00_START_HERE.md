# GastroAGI TASK11 — Codex Repair Mission

**Project:** GastroAGI Clinical Knowledge Ingestion Pipeline  
**Mission:** Repair the existing PDF-to-clinical-chunk workflow while preserving source fidelity, existing functionality, and production safety.  
**Current phase:** Milestone 1 — Source Integrity and Reference Recovery  
**Status:** Implementation authorization is limited to local, test-driven repairs. No production deployment is authorized.

## 1. Primary Objective

Make the five original NEJM PDFs process reliably into clinically accurate, source-reviewable knowledge chunks.

The final target workflow is:

PDF intake → source identity verification → page-aware extraction → source mapping → Azure chunk generation → five-field validation → clinical evidence verification → human approval → backend ingestion → embeddings → retrieval verification.

The immediate task is to resolve the demonstrated extraction, citation, and checkpoint defects.

Do not assume that successful schema validation proves clinical accuracy.

## 2. Required Reading Order

Before changing code:

1. Read the repository root `AGENTS.md`, if present.
2. Read `docs/codex/01_VERIFIED_FAILURE_AUDIT.md`.
3. Read `docs/codex/02_IMPLEMENTATION_ROADMAP.md`.
4. Read `docs/codex/03_REGRESSION_TEST_PLAN.md`.
5. Inspect the current repository implementation, prompts, dependencies, existing tests, and saved failure reports.
6. Consult official documentation matching the installed dependency versions.

If an instruction file is missing or empty, stop and report the problem.

If these documents conflict with actual source code, identify the discrepancy rather than assuming the documents represent the current implementation.

## 3. Relevant Existing Files

Inspect these paths if present:

- `src/pdf_extractor.py`
- `src/chunk_generator.py`
- `src/chunk_validator.py`
- `src/source_map.py`
- `src/pipeline.py`
- `src/local_rag_chat.py`
- `src/local_chunk_store.py`
- `src/providers/azure.py`
- `src/providers/base.py`
- `src/providers/registry.py`
- `prompts/gastroagi_chunk_rules.md`
- Relevant `tests/test_*.py` files
- `requirements.txt`, `pyproject.toml`, or the actual dependency manifest.

Verify file existence, class interfaces, function signatures, and dependency versions before implementing changes.

## 4. Confirmed Baseline Findings

Codex reported the following during Milestone 0:

- Four of five NEJM jobs failed with a missing `reference` field.
- Current `pypdf` extraction did not reliably expose focal article citation metadata for those four PDFs.
- The ABC-02 article contains `66.5` and `73.5`, but the existing `pypdf` extraction did not retain them.
- Local PyMuPDF extraction recovered both values on PDF page 4.
- The ABC-02 DOI was truncated by extraction to `10.1056/NEJMoa09`, rather than the full `10.1056/NEJMoa0908721`.
- Schema/content validation can reject model output before controlled reference enrichment occurs.
- `mark_processed_sections_from_validation()` can mark source sections processed even when linked chunks are rejected.
- The current local pipeline is not verified for production GastroAGI ingestion.

Reported baseline test results:

- Targeted tests: 126 passed, 9 subtests passed, 1 warning.
- Full suite: 155 passed, 9 subtests passed, 1 warning.
- The warning concerned permission to write `.pytest_cache`.

These are historical baseline results reported by Codex. Do not treat them as proof that a changed implementation works.

## 5. Mandatory Engineering Rules

### Preserve Clinical Integrity

- Never invent references, DOIs, clinical findings, effect sizes, denominators, confidence intervals, or treatment recommendations.
- Preserve study population, comparator, dose, intervention, endpoint, timeframe, and limitations.
- Do not accept a numerical claim merely because its number appears somewhere in a source document.
- Preserve raw evidence and traceable page or section provenance.
- Route unresolved clinical uncertainty to review.
- Never use arbitrary raw-text splitting as a silent replacement for failed clinical chunk generation.

### Preserve the Five-Field Contract

Every final clinical chunk must have:

1. `topic`
2. `subtopic`
3. `section`
4. `reference`
5. `original_text`

Keep the existing verified casing and serialization conventions. Store internal provenance and processing metadata separately.

Typical clinical content should be 150–250 words covering one coherent concept.

### Preserve Production Safety

- Do not connect to or modify a production database.
- Do not publish chunks to ChromaDB.
- Do not invent FastAPI endpoints or backend schemas.
- Do not bypass explicit human approval.
- Do not make live Azure requests without separate authorization.
- Do not move, delete, or overwrite source PDFs destructively.
- Do not expose API keys, credentials, PHI, or confidential source documents.
- Preserve the current `BLOCKED_UNVERIFIED_BACKEND` behavior.

### Avoid Unnecessary Complexity

- Keep Azure as the existing active generation provider.
- Do not introduce new autonomous AI agents during Milestone 1.
- Do not introduce LangGraph simply to repair extraction or validation defects.
- Prefer minimal, deterministic Python modifications.
- Preserve existing public interfaces and test compatibility wherever possible.

## 6. Required Problem-Solving Workflow

For each repair task:

1. Reproduce the existing failure.
2. Identify the precise defective function and affected data.
3. Add a regression test that demonstrates the failure.
4. Implement the smallest safe correction.
5. Execute targeted tests.
6. Execute the relevant existing test suite.
7. Verify outputs and clinical evidence independently.
8. Document changed files and remaining risks.
9. Stop if a clinical or production safety boundary cannot be verified.

Do not claim tests passed unless they actually ran.

## 7. Current Authorization: Milestone 1

Perform the following tasks in order:

**Task 1 — Checkpoint Integrity**

Fix rejected or unknown chunks incorrectly marking sections `processed`.

**Task 2 — PDF Extraction Reliability**

Benchmark existing `pypdf` output against PyMuPDF for the five NEJM articles. Restore missing clinical evidence and complete bibliographic identifiers through a measured extraction improvement.

**Task 3 — Verified Reference Binding**

Bind authoritative article references to missing or blank chunk references before final strict validation. Reject conflicting or unverified citations.

**Task 4 — Regression Validation**

Run targeted and full tests, inspect the outputs, and report precisely what has been fixed.

Do not implement new production ingestion features during these tasks.

## 8. Repository Protection

Milestone 0 reported that the project checkout was not recognized as a Git repository.

Before modifications:

- Establish a non-destructive backup or an approved version-control baseline.
- Record original file checksums or retain original copies of files to be changed.
- Do not initialize Git or alter repository history without authorization.
- Keep diagnostic and generated test artifacts separate from production data.

## 9. Milestone Completion Requirements

For each completed task, provide:

- Root cause and source-code evidence.
- Exact files/functions changed.
- Tests executed and actual results.
- Before/after behavior.
- Clinical integrity checks.
- Remaining risks or assumptions.

For extraction changes, show a before/after comparison for all five PDFs.

## 10. Definition of Success

Milestone 1 succeeds when:

- Verified article citations can be preserved and correctly bound.
- The ABC-02 numerical evidence is retained in its proper clinical context.
- Unsupported claims remain blocked.
- Rejected chunks cannot falsely complete checkpoints.
- Existing pipeline functionality remains intact.
- Regression tests pass.
- No unauthorized Azure or production operation has occurred.

Passing Milestone 1 does not establish that the full RAG pipeline is production-ready.

Production ingestion, retrieval verification, and controlled publication remain separate later milestones.
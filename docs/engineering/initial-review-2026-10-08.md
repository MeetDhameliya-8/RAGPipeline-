# Initial TASK11 Alignment Review

Date: 2026-10-08.

This review inspected the existing GastroAGI PDF-to-RAG chunk pipeline without changing production Python code, database/vector contents, source PDFs, processed PDFs, or generated medical chunk payloads.

## What Was Inspected

- `AGENTS.md`
- `README.md`
- `requirements.txt`
- `.gitignore`
- `prompts/gastroagi_chunk_rules.md`
- `config/admin_selectors.example.json`
- `docs/engineering/repo-baseline.md`
- `docs/engineering/task11-rag-guidelines.md`
- `skills/gastroagi-rag-engineering/SKILL.md`
- `skills/gastroagi-rag-engineering/references/clinical-safety.md`
- `skills/gastroagi-rag-engineering/references/rag-architecture.md`
- `skills/gastroagi-rag-engineering/references/testing.md`
- `src/pipeline.py`
- `src/pdf_extractor.py`
- `src/chunk_generator.py`
- `src/chunk_validator.py`
- `src/admin_uploader.py`
- `tests/conftest.py`
- `tests/test_generator.py`
- `tests/test_validator.py`
- `tests/test_pdf_extractor.py`
- `tests/test_pipeline_helpers.py`
- `tests/test_admin_uploader.py`
- `output/testing/validation_report.json`

Secrets in `.env` were not printed or copied into documentation.

## AGENTS.md Verification

`AGENTS.md` contains the project's core rules for this review:

- Mission: reliable, testable ingestion of gastroenterology and hepatology PDFs into clinician-reviewable GastroAGI chunks.
- Medical accuracy: the PDF is the source of truth; do not invent or repair clinical facts silently.
- Output contract: every content chunk has exactly `Topic`, `Subtopic`, `Section`, `Reference`, and `Original Text`; `chunk_number` is metadata only.
- Engineering standards: small typed modules, deterministic helpers, tests, explicit contracts, and separate ingestion, validation, storage, retrieval, and admin boundaries.
- Dependency discipline: PyMuPDF/PyPDF, LangChain, ChromaDB, FastAPI, and Pydantic are preferences only when intentionally added or already present.
- Security: no credentials, browser state, logs, source PDFs, processed PDFs, or generated medical outputs in source files or documentation.

## Verified Architecture

The current implementation is a CLI-first clinical chunk generation pipeline:

1. `src.pipeline.main` parses CLI arguments and dispatches processing.
2. `src.pdf_extractor.extract_pdf` reads the PDF with `pypdf.PdfReader`, extracts page text, removes repetitive margins/page numbers, detects DOI/citation hints, removes reference sections and boilerplate conservatively, and returns extraction diagnostics.
3. `src.chunk_generator.generate_chunks` builds the LLM input from cleaned article text and verified citation metadata, loads `prompts/gastroagi_chunk_rules.md`, calls the configured provider, parses JSON, and normalizes common key casing.
4. `src.chunk_validator.validate_chunks` enforces required fields, word count, classification quality, section quality, duplicate detection, reference leakage checks, affiliation/source-cleaning checks, abbreviation warnings, and source-number checks.
5. `src.pipeline.save_outputs` writes `chunks.json`, `chunks.md`, and `validation_report.json` for human review only after generation returns structured output.
6. `src.admin_uploader` can load reviewed chunks, re-run validation, populate configured Admin Panel fields with Playwright, verify values, and stop before final upload.

There is no implemented FastAPI service layer, LangChain/LangGraph orchestration graph, ChromaDB vector store, retrieval API, embedding contract, vector collection schema, or database persistence boundary in the current codebase.

## Dependency Findings

Declared in `requirements.txt`:

- `pypdf>=4.0`
- `openai>=1.0`
- `python-dotenv>=1.0`
- `playwright>=1.45`
- `pytest>=8.0`

Installed in the inspected Python 3.13.7 environment:

- `pypdf` 6.9.2
- `openai` 3.13.0
- `python-dotenv` 1.2.3
- `playwright` 1.62.0
- `pytest` 9.1.1
- `PyMuPDF` 1.27.2.2, installed but not declared
- `pydantic` 2.13.5, installed as a transitive dependency of `openai` but not declared for project use

Not installed and not declared:

- `langchain`
- `langgraph`
- `fastapi`
- `chromadb`

Local API checks:

- `pypdf._page.PageObject.extract_text` supports `extraction_mode`, including the current `extraction_mode="layout"` usage.
- `openai.OpenAI(...).responses.create` exists locally and supports `model`, `input`, `temperature`, and `text` parameters.

## Official Documentation Accessed

Official documentation pages were opened during this review and recorded in `skills/gastroagi-rag-engineering/references/official-docs.md`.

Accessed pages included:

- Python 3.13 documentation: https://docs.python.org/3.13/
- Python latest documentation: https://docs.python.org/3/
- pypdf text extraction: https://pypdf.readthedocs.io/en/stable/user/extract-text.html
- OpenAI Python SDK: https://github.com/openai/openai-python
- LangChain Python overview: https://docs.langchain.com/oss/python/langchain/overview
- LangGraph Python overview: https://docs.langchain.com/oss/python/langgraph/overview
- FastAPI documentation: https://fastapi.tiangolo.com/
- Chroma documentation: https://docs.trychroma.com/docs/overview/introduction
- PyMuPDF documentation: https://pymupdf.readthedocs.io/en/latest/
- Pydantic documentation: https://docs.pydantic.dev/latest/
- pytest documentation: https://docs.pytest.org/en/stable/

These references do not authorize implementation by themselves. Future feature work must re-open the relevant docs and verify local package versions before relying on specific APIs.

## Workflow Trace

The existing sample workflow was traced without writing outputs or moving PDFs:

- Source PDF: `input_pdfs/Testing.pdf`
- Extracted pages: 16
- Raw extracted characters: 260234
- Cleaned article characters: 48236
- Detected reference metadata: `doi:10.1136/gutjnl-2014-308363`
- Characters prepared for model input: 48453
- Prompt loaded: yes
- Provider path: OpenAI
- Model configured: `gpt-4.1`
- Generator mode diagnostic: `REAL LLM`

During extraction, `pypdf` printed repeated warnings that rotated text was discovered and output may be incomplete, but the pipeline's own `extraction_warnings` list was empty for this PDF.

The saved prior validation report at `output/testing/validation_report.json` shows the exact observed failure point:

- Status: `FAILED`
- Rule: `GENERATION_FAILED`
- Failure: OpenAI rejected the configured API key with `401 invalid_api_key`
- Chunks returned: 0
- No valid clinical chunks were generated, validated, indexed, uploaded, or substituted.

Because generation failed, the workflow did not reach successful chunk validation, Admin Panel population, FastAPI serving, retrieval, or ChromaDB indexing. API and ChromaDB stages are not implemented in this repository.

## Test Results

Command run:

```powershell
python -B -m pytest -q -p no:cacheprovider
```

Result:

```text
22 passed, 1 subtests passed in 0.22s
```

The `-B` flag and disabled cache provider were used to avoid creating bytecode or `.pytest_cache` artifacts during this review.

## Known Bugs And Gaps

- The current configured OpenAI credential path produced `401 invalid_api_key` in the saved validation report; no successful LLM chunk generation is demonstrated in this checkout.
- `pypdf` reports rotated text warnings to stderr, but those warnings are not captured in `extraction_warnings`, so extraction quality diagnostics may be less complete than the console output.
- `README.md` references `.env.example`, but no `.env.example` file was present in the inspected file list.
- The repository is not a Git checkout from the inspected working directory, so `git status --short` failed with `fatal: not a git repository`.
- LangChain, LangGraph, FastAPI, and ChromaDB are neither installed nor declared; all RAG service/vector-store work remains future implementation.
- PyMuPDF and Pydantic are installed in the broad Python environment but are not declared project dependencies; production code should not depend on them until they are intentionally added.
- There is no persistent embedding model contract, Chroma collection name, document ID strategy, metadata schema, update/delete behavior, retrieval API, or human-review-to-indexing gate beyond the existing guidance docs.

## Initial Review Changes Made

- Added `skills/gastroagi-rag-engineering/references/official-docs.md`.
- Updated `skills/gastroagi-rag-engineering/SKILL.md` to require official documentation and local API verification before library implementation work.
- Added this review record at `docs/engineering/initial-review-2026-10-08.md`.

No production code or database/vector contents were modified.

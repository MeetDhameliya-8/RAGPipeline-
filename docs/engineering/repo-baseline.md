# Repository Baseline

Last inspected: 2026-10-09.

## Current Structure

- `src/`: pipeline implementation modules.
- `tests/`: unit tests for generator, validator, PDF extraction, pipeline helpers, and admin uploader.
- `prompts/`: GastroAGI chunk generation prompt.
- `config/`: admin selector example configuration.
- `input_pdfs/`, `processed/`, `output/`, `logs/`: local runtime folders ignored by Git except `.gitkeep`.

## Implemented Modules

- `src.pipeline`: CLI orchestration, extraction, generation, validation, output writing, failure reports, and no-move test generation.
- `src.pdf_extractor`: PDF text extraction, conservative cleaning, and citation metadata detection.
- `src.providers`: provider interface, Azure OpenAI adapter, and runtime provider registry. Azure is the only environment-selectable runtime inference provider.
- `src.chunk_generator`: clinical prompt loading, generation input construction, JSON parsing, strict generated-output schema validation, and normalization.
- `src.chunk_validator`: deterministic validation of schema, clinical chunk quality, source fidelity, and duplicate/reference issues.
- `src.admin_uploader`: safe Playwright form population after review; it must not click final Upload automatically.

## Declared Project Dependencies

`requirements.txt` currently declares:

- `pypdf>=4.0`
- `openai>=1.0`
- `pydantic>=2.0`
- `python-dotenv>=1.0`
- `playwright>=1.45`
- `pytest>=8.0`

The broader Python environment may contain additional packages such as PyMuPDF, Pydantic, Flask/Django, PDF utilities, pandas, and scientific libraries. Do not rely on undeclared packages for project code unless the dependency is intentionally added to `requirements.txt` and covered by tests.

## Current Non-Goals

- No implemented LangChain ingestion graph yet.
- No implemented FastAPI service layer yet.
- No implemented ChromaDB persistence layer yet.
- No committed RAG database schema, collection schema, embedding model contract, or retrieval API contract yet.

Treat those as future architecture options from TASK11 guidance, not existing code.




# GastroAGI PDF-to-RAG Clinical Knowledge Pipeline

## Mission

Build a reliable, testable ingestion pipeline that converts gastroenterology and hepatology PDFs into GastroAGI retrieval-ready clinical knowledge chunks for clinician review. Preserve existing behavior unless the user explicitly asks for implementation changes.

## Medical Safety

The source PDF is the source of truth. Do not invent, strengthen, generalize, or fill clinical facts from outside knowledge. If doses, outcomes, time points, statistics, recommendations, or bibliographic details cannot be verified from extracted text, make validation flag the uncertainty instead of repairing it silently.

Generated chunks are drafts only. Human medical review is mandatory before publication or Admin Panel upload.

## Output Contract

Every GastroAGI content chunk has exactly five content fields:

- Topic
- Subtopic
- Section
- Reference
- Original Text

`chunk_number` is pipeline metadata only. Do not treat it as an Admin Panel content field or RAG document payload field unless a later schema explicitly says so.

## Engineering Rules

Use small typed Python modules, deterministic helpers, Pydantic models when introducing structured contracts, and automated tests for extraction, generation, validation, persistence, retrieval, and API boundaries. Prefer PyMuPDF/PyPDF extraction, LangChain orchestration, ChromaDB vector storage, and FastAPI service boundaries only when those dependencies are intentionally added to this repo; do not invent APIs, database schemas, vector collections, or admin selectors.

Before adding RAG features, inspect the current repository, declared dependencies, existing prompts, output JSON shape, and validator rules. Keep ingestion, validation, storage, retrieval, and admin automation as separate boundaries.

## Security

Never expose credentials. Do not store API keys, cookies, browser profiles, session state, logs, input PDFs, processed PDFs, or generated medical outputs in source files or documentation. Use `.env` or a secure secret mechanism; keep secrets masked in logs and validation reports.

## Local References

For RAG engineering tasks, use the reusable project skill at `skills/gastroagi-rag-engineering/SKILL.md`.

Reference docs:

- `docs/engineering/repo-baseline.md`
- `docs/engineering/task11-rag-guidelines.md`


# TASK11 RAG Engineering Guidelines

This document transfers the TASK11 engineering intent into repository-level guidance without changing implementation code.

## Preferred Stack Direction

When the project intentionally expands beyond the current chunk pipeline, prefer:

- Python for pipeline logic and testable service code.
- PyMuPDF or PyPDF for PDF extraction, selected by measured extraction quality.
- Pydantic for explicit request, response, chunk, metadata, validation, and persistence contracts.
- LangChain only for orchestration steps that benefit from its abstractions; keep domain validation outside opaque chains.
- ChromaDB for local vector storage only after collection names, metadata fields, embedding model, and persistence paths are explicitly defined.
- FastAPI for service boundaries only after CLI behavior and data contracts are stable.
- Pytest/unittest tests around every boundary that can silently corrupt clinical content.

## RAG Boundary Design

Keep these boundaries separate:

- PDF ingestion and extraction.
- Text cleaning and citation metadata detection.
- Clinical chunk generation.
- Deterministic validation.
- Human-review output files.
- Optional vector embedding and storage.
- Retrieval/query API.
- Admin Panel automation.

Do not let retrieval storage bypass chunk validation or human review. Do not upload or index failed chunks as if they were production-ready.

## Clinical Data Rules

Use the PDF and extracted text as the source of truth. Preserve uncertainty. Prefer failed validation over guessed clinical repair. Do not infer missing publication metadata, sample sizes, outcomes, recommendations, dosing, or safety findings from outside medical knowledge.

## Change Discipline

Before implementation changes:

1. Inspect current code and tests.
2. Identify the contract being changed.
3. Add or update focused tests.
4. Keep changes small and reversible.
5. Run the relevant test command and report failures honestly.

Do not introduce LangChain, FastAPI, ChromaDB, or new schemas merely because TASK11 mentions them. Add them only when the requested feature requires them.

# RAG Architecture Reference

Use this reference before adding retrieval, embedding, vector storage, or API features.

## Boundary Order

Recommended flow:

1. PDF extraction and quality diagnostics.
2. Conservative text cleaning and citation metadata extraction.
3. Clinical chunk generation from the complete cleaned article context.
4. Deterministic validation.
5. Human-review output files.
6. Optional embedding and vector storage after review gates are satisfied.
7. Retrieval/query API over reviewed content.

Do not allow vector storage or API serving to bypass validation and review.

## Dependency Discipline

The current project may not yet declare LangChain, FastAPI, ChromaDB, PyMuPDF, or Pydantic. Add dependencies only when the feature requires them, and update tests and documentation with the contract they support.

## ChromaDB Guidance

Before creating a collection, define:

- collection name
- embedding model
- stable document ID strategy
- chunk payload fields
- metadata fields
- persistence path
- duplicate/reindex behavior
- deletion/update behavior

Do not invent these values silently.

## FastAPI Guidance

Before adding endpoints, define request/response models, validation errors, status codes, and whether the endpoint can mutate files, vector stores, or admin systems. Keep CLI behavior stable unless explicitly changing it.

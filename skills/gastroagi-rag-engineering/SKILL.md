---
name: gastroagi-rag-engineering
description: Engineer GastroAGI PDF-to-RAG ingestion features with clinical source fidelity, explicit contracts, Azure-only runtime inference, and tested Python boundaries. Use for GastroAGI pipeline, RAG, chunking, extraction, validation, vector storage, or API work.
metadata:
  short-description: GastroAGI PDF-to-RAG engineering
---

# GastroAGI RAG Engineering

Use this skill when developing or reviewing GastroAGI PDF-to-RAG ingestion work. The goal is to preserve clinical source fidelity while evolving the pipeline toward tested extraction, validation, storage, retrieval, and API boundaries.

## Start Here

Before changing code, inspect the repository structure, declared dependencies, current prompts, output schema, and tests. In this repository, also read:

- `AGENTS.md`
- `docs/architecture.md`
- `docs/azure-integration.md`
- `docs/clinical-validation.md`
- `docs/ingestion-contract.md`
- `docs/project-decisions.md`
- `docs/engineering/repo-baseline.md`
- `docs/engineering/task11-rag-guidelines.md`
- `skills/gastroagi-rag-engineering/references/official-docs.md`

If those files are absent in another project, infer only from the local code and ask before introducing new persistent schemas or service APIs.

## Core Constraints

- The source PDF is the source of truth.
- Azure OpenAI is the only environment-selectable runtime inference provider in this repository.
- Do not invent clinical facts, bibliographic metadata, APIs, vector collection schemas, database schemas, admin selectors, or payload fields.
- Generated chunks are drafts for clinician review.
- Failed validation must block production ingestion, indexing, or Admin Panel upload.
- Secrets, PDFs, generated medical outputs, logs, browser state, and API keys must stay out of source control.

## Engineering Preferences

Use small typed Python modules and tests around every boundary. Prefer Pydantic when adding structured contracts. Prefer PyMuPDF/PyPDF extraction based on measured quality. Use LangChain, FastAPI, and ChromaDB only when the requested feature explicitly needs orchestration, service boundaries, or vector persistence and the dependency/contracts are added deliberately.

When implementing or changing behavior that depends on a third-party library, consult the relevant official documentation for the installed major/minor version when web access is available, then verify the specific API against the package installed in the local environment. A stored documentation URL is only a pointer; do not treat it as evidence that the contents have been read in the current implementation pass.

Keep these responsibilities separate:

- extraction and cleaning
- provider transport and configuration
- clinical generation and JSON normalization
- validation
- human-review file output
- vector embedding/storage
- retrieval/query handling
- admin automation

## Supporting References

- Read `references/clinical-safety.md` when changing generation, validation, ingestion, or upload behavior.
- Read `references/rag-architecture.md` before adding embeddings, vector storage, retrievers, or APIs.
- Read `references/testing.md` before adding or changing tests.
- Read `references/official-docs.md` before implementing Python, LangChain, LangGraph, FastAPI, ChromaDB, PyMuPDF, Pydantic, or pytest features.

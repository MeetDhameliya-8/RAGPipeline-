# Phase 6 Framework Selection

This document evaluates whether additional frameworks genuinely improve the GastroAGI PDF-to-RAG clinical chunk pipeline. The default decision is to prefer simple, typed, testable Python modules unless a framework removes real complexity, improves reliability, or establishes a necessary boundary.

## Current Decision Summary

No new dependency is required for Phase 6 itself. The current declared stack remains sufficient for the implemented workflow:

- PDF extraction and cleaning
- Azure OpenAI-backed generation
- deterministic validation and source-grounding checks
- human-review package output
- safe Admin Panel form population after review
- automated tests

Do not add dependencies merely to make the project look more advanced. Do not introduce a multi-agent architecture for this pipeline. Human clinical review is the required review boundary, not an agent swarm.

## Selection Criteria

A candidate dependency should be added only when it satisfies all applicable criteria:

- It supports a clearly defined pipeline boundary.
- It improves correctness, reliability, maintainability, or observability more than a small local module would.
- Its data contracts are documented before implementation.
- It can be tested deterministically without live credentials or medical source files.
- It does not bypass extraction quality checks, validation, source-grounding, or human review.
- It does not add hidden storage, retrieval, or upload behavior.

## Candidate Decisions

| Component | Decision | Add now? | Why |
| --- | --- | --- | --- |
| Azure-compatible model SDK | Keep current OpenAI SDK Azure path | No new dependency | The project already uses the declared `openai` package with `AzureOpenAI` for Azure OpenAI chat completions. This is necessary for generation and connectivity checks. Keep provider handling explicit and fail closed when Azure config is missing or invalid. |
| Pydantic | Keep and continue using | Already declared | Pydantic provides strict structured contracts for generated documents and Layer 1 technical validation. This directly protects the five-field chunk contract and validation report quality. |
| PyMuPDF | Defer | No | Current extraction uses `pypdf`. Add PyMuPDF only if measured extraction quality improves on real PDFs or if page/block/span provenance cannot be captured well enough with current extraction. Do not switch without fixtures and comparison tests. |
| FastAPI | Defer | No | The CLI workflow is sufficient today. Add FastAPI only after request/response schemas, mutation rules, authentication expectations, validation failure behavior, and file/vector side effects are defined. |
| LangChain | Defer with strict entry criteria | No | Use LangChain only if it clearly improves orchestration over small Python functions, such as standardized model wrappers, prompt composition, retrieval chains, or tracing that the project actually needs. Do not put clinical validation inside opaque chains. |
| LangGraph | Defer; likely unnecessary now | No | Use LangGraph only if the workflow needs resumable state, conditional branches, checkpoints, retryable long-running jobs, or human-review interruption that cannot be handled cleanly by current modules. Current batch CLI processing does not justify it. |
| ChromaDB | Defer until reviewed-content retrieval exists | No | Add ChromaDB only after collection name, embedding model, stable document IDs, metadata fields, persistence path, duplicate/reindex behavior, and deletion/update behavior are defined. Vector indexing must be downstream of validation and explicit human approval. |
| pytest | Keep | Already declared | pytest is necessary for deterministic coverage around extraction, generation, validation, review package output, and admin automation boundaries. Expand tests as features expand. |

## Dependency-by-Dependency Rationale

### Azure-Compatible Model SDK

Current necessity: required.

The pipeline needs an Azure-compatible SDK for model generation and connectivity diagnostics. The current `openai` dependency is already declared and supports the Azure OpenAI path used by `src.chunk_generator.AzureChunkGenerator`. Keep this dependency and avoid adding parallel SDKs unless Azure API compatibility, authentication, or deployment constraints require it.

Required tests before changing this dependency:

- provider selection
- missing configuration failures
- sanitized error handling
- retry behavior for transient API failures
- no fallback to unconfigured providers

### Pydantic

Current necessity: required.

Pydantic is justified because the project has explicit schemas: generated article metadata, chunk records, validation reports, and future API/retrieval contracts. It helps prevent malformed JSON from being mistaken for reviewable clinical content.

Use Pydantic for:

- external or model-generated structured data
- request/response contracts if APIs are added
- persistence contracts if retrieval storage is added

Avoid Pydantic for trivial internal helpers where plain typed functions are clearer.

### PyMuPDF

Current necessity: not yet proven.

PyMuPDF may improve extraction quality, page-level provenance, block layout, and text spans for some PDFs. It should be evaluated with side-by-side fixtures before becoming a dependency. The source PDF remains the source of truth, so an extraction switch must improve measured fidelity rather than merely offer a richer API.

Entry criteria:

- current `pypdf` extraction is materially degraded on representative PDFs
- PyMuPDF improves text order, page provenance, table handling, or span reliability
- tests cover extraction quality warnings and source evidence mapping

### FastAPI

Current necessity: not yet present.

FastAPI is useful only when the pipeline needs a service boundary, such as an internal review API, retrieval API, or controlled ingestion endpoint. A web service would add operational and security responsibilities, so it should not be introduced while the CLI workflow is sufficient.

Entry criteria:

- stable request/response models
- explicit validation failure status codes
- authentication and authorization expectations
- clear rules for file writes, vector writes, and admin actions
- tests for all API boundaries

### LangChain

Current necessity: not yet proven.

LangChain can be helpful for prompt orchestration, model abstraction, retriever composition, tracing, or standardized document processing. At the current stage, local Python modules are more transparent and easier to test. LangChain should not replace deterministic validation or make clinical source-fidelity checks opaque.

Entry criteria:

- repeated orchestration code becomes meaningfully complex
- the project needs a retriever chain with documented inputs and outputs
- LangChain abstractions reduce code while preserving source provenance and testability
- validation remains outside the chain

### LangGraph

Current necessity: not justified.

LangGraph should be considered only for durable, interruptible workflows. Examples include resumable PDF processing, checkpointed generation, conditional retry flows, and human-review interruption with restart after approval. The current CLI batch pipeline can handle failures and review output without a graph runtime.

Entry criteria:

- workflows require persisted state across process restarts
- human review must pause and resume an automated workflow
- branch logic becomes too complex for straightforward Python orchestration
- checkpoint tests prove recovery behavior

### ChromaDB

Current necessity: deferred.

ChromaDB may be appropriate for local vector storage after chunks pass validation and human review. It must not be introduced before the RAG storage contract is defined.

Required contract before adding:

- collection name
- embedding model
- document ID strategy
- chunk payload fields
- metadata fields
- persistence path
- duplicate and reindex behavior
- deletion/update behavior
- approval gate preventing unreviewed chunks from being indexed

### pytest

Current necessity: required.

pytest is essential for protecting clinical content boundaries. It should remain the main test runner for deterministic tests. Add focused tests before changing extraction, generation, validation, review packages, retrieval storage, APIs, or admin automation.

## Explicit Non-Decision: Multi-Agent Architecture

Do not introduce a multi-agent architecture for this pipeline at this stage. The current system needs source fidelity, deterministic validation, auditable review files, and explicit human approval. Multiple agents would add coordination complexity without improving clinical correctness. Optional second-model review may assist Layer 2, but it is not independent proof of correctness and must not overwrite deterministic failures.

## Implementation Policy

When proposing any new dependency:

1. Document the boundary it supports.
2. Update `requirements.txt` deliberately.
3. Add or update tests for that boundary.
4. Update `docs/engineering/` with the contract and failure behavior.
5. Confirm the dependency does not bypass validation or human review.

As of this Phase 6 decision, no additional framework is approved for immediate addition.

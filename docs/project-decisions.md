# Project Decisions

## Verified Decisions

- Azure OpenAI is the only environment-selectable runtime inference provider.
- Unused legacy OpenAI generation code was removed in the 2026-10-15 stability audit; Azure transport remains in `src.providers.azure`.
- No Swan provider is implemented or selected.
- Provider transport is separated from clinical generation in `src.providers`.
- Clinical generation prompts, output fields, PDF extraction, validation, review package output, and Admin Panel safety behavior are preserved.
- Routine tests use mocked provider responses and must not perform paid API requests.
- Human review is mandatory before upload, indexing, or ingestion.
- Long-PDF source mapping is implemented as a separate deterministic layer; it does not alter the five Admin Panel fields.

## Rejected Or Deferred Approaches

- LangChain and LangGraph are not introduced because the current workflow does not require graph orchestration, checkpoints, or multi-agent state.
- FastAPI is deferred until a service boundary is actually needed.
- ChromaDB is deferred until the real backend contract, embedding model, collection names, and metadata schema are known.
- `/openai/v1` Azure endpoint migration is deferred because the current deployed Azure configuration uses the existing endpoint style and API version.
- Automatic provider fallback is rejected for clinical safety and reproducibility.

## Implementation Outcomes

- Azure-specific configuration, SDK setup, retries, error mapping, and connectivity checks are isolated in `src.providers.azure`.
- The provider registry keeps Azure as the only active runtime provider.
- `--check-llm` is token-bounded and non-clinical.
- Generator schema validation remains in place; schema consolidation was reviewed and deferred because the generator and validator schemas serve distinct validation checkpoints.

## Remaining Risks

- Source-grounding heuristics are useful review signals, not proof of clinical accuracy.
- Full section-by-section Azure generation for long PDFs remains future work; source mapping, coverage reports, provenance, and checkpoint state are implemented.
- Backend ingestion cannot be implemented safely until the real backend is inspected.
- Live Azure compatibility should be verified only with approved low-token checks and approved clinical generation runs.




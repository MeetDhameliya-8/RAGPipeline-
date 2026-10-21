# GastroAGI Pipeline Architecture

## Verified Current Behavior

The project is a CLI-first PDF-to-clinical-chunk pipeline. `src.pipeline` orchestrates PDF extraction, Azure-backed generation, deterministic validation, human-review package output, and optional safe Admin Panel form population.

Implemented boundaries:

- `src.pdf_extractor`: compares pypdf and PyMuPDF page extraction, selects one representation per page, preserves raw page text, cleans conservatively, and detects citation metadata.
- `src.providers`: contains provider interfaces, the Azure OpenAI adapter, and the runtime provider registry.
- `src.adaptive_ingestion`: plans complete bounded paragraph/sentence inputs; unresolved sparse visual/encoding evidence and indivisible oversized inputs block generation.
- `src.chunk_generator`: generates sequential bounded contexts, validates raw JSON types/keys/counts before controlled normalization/reference binding, and records observed per-unit counts. A failed later unit still prevents a complete package.
- `src.numeric_evidence`: retains lexical numeric values, signs/operators/units and suspected extraction ambiguity for local evidence checks.
- `src.chunk_validator`: applies technical validation, clinical quality checks, source-grounding heuristics, duplicate checks, and source evidence reporting.
- `src.source_map`: builds section-level source maps, page-range provenance, coverage reports and section-state records. These records do not yet provide persisted generation-unit resumption.
- `src.pipeline`: writes draft/review/subset/source-map/state/coverage artifacts. Generation failure saves failure diagnostics and available source-map/state/coverage, without returning a complete draft package. CLI partial/incomplete jobs return exit 2.
- `src.admin_uploader`: safely populates the Admin Panel after review and stops before final Upload.
- `src.local_rag_chat`: localhost upload/queue API, Admin page, and Clinical Assistant UI.
- `src.local_chunk_store`: SQLite draft storage, deterministic hash embeddings, relevance filtering, request-scoped conversation history, and grounded answer assembly.

Azure OpenAI is the only runtime inference provider. The unused legacy OpenAI adapter and duplicated transport helpers were removed in the 2026-10-15 stability audit.

## Implemented But Not Fully Clinically Validated

- Deterministic source-grounding checks flag unsupported numbers, weakly supported claims, missing qualifications, and possible contradictions.
- `review_package.md` links generated chunks to available source evidence where exact spans or page matches can be found.
- `--check-llm` performs an opt-in, non-clinical, token-bounded provider connectivity check.

These safeguards support review but do not prove clinical accuracy.

## Planned Future Functionality

- Unify existing bounded generation with the section map and canonical evidence; add durable compatible per-unit recovery.
- Stronger source-grounding review over source-section evidence.
- Backend ingestion after the real GastroAGI API, authentication, embedding model, metadata schema, and retrieval behavior are inspected.
- Retrieval verification after approved ingestion.

## Unknown Or Unverified Contracts

- GastroAGI production backend routes, authentication, database schema, and embedding model.
- ChromaDB collection names and metadata fields, if ChromaDB is used by the actual backend.
- Update/delete propagation rules for indexed clinical content.
- Production chatbot retrieval behavior; the local assistant has tested ranking, internal citation validation, and a collapsed References panel.

## Stability Audit

See [the P0 implementation review](engineering/task11-p0-implementation-review-2026-10-16.md) for current changes, evidence limits, tests and as-built/target matrix. The [workflow audit](engineering/workflow-audit-2026-10-15.md) and earlier milestone documents describe historical baselines. P1 canonical evidence and generation-unit persistence remain deferred at the requested review checkpoint.





# Phase 7 Database Ingestion Architecture

This document records what was inspected for database ingestion and defines the approved-only ingestion architecture that should be used only after the real GastroAGI backend contract is available. It does not implement ingestion, does not create database schemas, does not define production API routes, and does not upload or modify clinical data.

## Inspection Result

The current `gastroagi-chunk-pipeline` working copy does not contain the production GastroAGI backend, ingestion service, FastAPI routes, ChromaDB client, embedding pipeline, retrieval API, chatbot implementation, or database schema.

Inspected sources:

- `src/` implementation modules
- `tests/`
- `requirements.txt`
- `README.md`
- `AGENTS.md`
- `docs/engineering/`
- `PipelineGuide/`
- available Codex app projects

Confirmed local findings:

- No implemented database ingestion adapter exists in `src/`.
- No implemented FastAPI service layer exists in this checkout.
- No ChromaDB dependency is declared in `requirements.txt`.
- No ChromaDB collection name, embedding model, persistence path, metadata schema, update/delete behavior, or retrieval API contract is committed here.
- The TASK11 planning docs explicitly state that production FastAPI ingestion contracts, ChromaDB collections, embeddings, and chatbot retrieval integration were not established in this working copy and must be rechecked against the actual GastroAGI backend.
- The Codex app project list available to this task does not include a separate GastroAGI backend repository.

Therefore the real backend has not been inspected in this task. Phase 7 implementation must remain blocked until the backend repository, API documentation, staging endpoint, or read-only backend inspection access is provided.

## Backend Facts That Must Be Determined Before Implementation

Do not implement ingestion until each item below is answered from the real backend, not from assumptions.

| Question | Current status in this repo | Required evidence |
| --- | --- | --- |
| Which ingestion endpoint actually exists? | Unknown | Backend route, service method, CLI command, or documented job interface. |
| How authentication works | Unknown | Auth mechanism, token/service account requirements, scopes, least-privilege role, staging vs production separation. |
| How metadata is represented | Unknown | Actual backend model/schema for source document, chunk metadata, approval status, source spans, references, active/candidate version. |
| Which embedding model is used | Unknown | Model name/deployment, provider, dimensions, normalization, distance metric, preprocessing. |
| How chunk IDs are generated | Unknown | Existing ID algorithm or accepted external ID contract. |
| Which ChromaDB collections exist | Unknown | Collection names, active/candidate separation, persistence path, tenant/environment boundaries. |
| How duplicate documents are handled | Unknown | Backend duplicate key rules, DOI/checksum/version behavior, idempotent upsert semantics. |
| How updates and deletions propagate | Unknown | Revision, deactivation, tombstone, delete, reindex, rollback, and cache invalidation behavior. |
| How chatbot retrieves indexed content | Unknown | Retrieval filters, active-status logic, metadata constraints, ranking, answer context construction, citation validation. |

## Non-Negotiable Safety Constraints

- Only explicitly approved chunks are eligible for ingestion.
- Failed validation, `REVIEW_REQUIRED` without approval, missing approval metadata, or unsupported source claims must block ingestion.
- The five Admin Panel content fields remain `Topic`, `Subtopic`, `Section`, `Reference`, and `Original Text`.
- Internal provenance and workflow metadata must remain outside those five content fields unless the real backend schema says otherwise.
- No direct writes to production ChromaDB or any production database are allowed from this pipeline unless the backend team explicitly approves that path.
- No real clinical chunks may be uploaded or indexed during architecture discovery.

## Approved-Only Ingestion Workflow

This is the target workflow once the actual backend contract is verified.

### 1. Approved Package Input

Entry conditions:

- `chunks.json` exists and matches the five-field contract.
- `validation_report.json` exists.
- `review_package.md` exists for human audit.
- Validation status is `PASS` or another backend-approved status.
- `human_review.approval_required` is true.
- `human_review.approved` is true.
- `human_review.ingestion_allowed` is true.
- Approval includes reviewer identity, timestamp, rationale or approval note, and approval scope.

If any condition is missing, ingestion must stop before contacting the backend.

### 2. Stable Chunk Identifiers

Use the backend's existing chunk ID algorithm if one exists. If the backend accepts caller-provided stable IDs and approves the approach, use deterministic IDs derived from canonical source identity and chunk identity.

Candidate local identity inputs, pending backend approval:

- source document stable ID or DOI
- source version or checksum
- source span or section identity when reliable
- normalized `Topic`, `Subtopic`, `Section`, `Reference`, and `Original Text`
- pipeline schema version

A candidate ID may be a namespaced SHA-256 digest of a canonical JSON object containing those inputs. This is not a production schema; it is a proposed deterministic strategy to discuss with the backend owner.

### 3. Idempotent Operations

Every ingestion submission should include an idempotency key approved by the backend contract.

Candidate idempotency inputs:

- ingestion job type
- approved package checksum
- source document ID/version
- sorted chunk IDs
- approval record ID or approval timestamp

Retrying the same approved package with the same idempotency key must not create duplicate active documents. Backend responses should distinguish `created`, `updated`, `unchanged`, `skipped_duplicate`, and `rejected` when supported.

### 4. Duplicate Protection

Duplicate protection should happen before backend submission and again inside the backend.

Pre-submit duplicate checks:

- same source checksum and DOI/version already staged or active
- same stable chunk ID appears more than once in the package
- same reference and near-identical `Original Text` already exists in prior approved outputs
- same source document revision was previously rejected or superseded

Backend duplicate behavior must be verified before implementation. Do not assume Chroma upsert semantics are sufficient clinical duplicate protection.

### 5. Retry-Safe Submission

Use bounded batches and retry only retryable failures.

Rules:

- Never retry authentication, authorization, schema, validation, or approval failures as transient errors.
- Retry only network timeouts, explicit 429/rate-limit responses, and documented 5xx transient failures.
- Preserve per-chunk status after partial failures.
- Resubmit only failed or unknown-status chunks using the same idempotency key or backend-approved replay mechanism.
- Never switch to a different backend, collection, embedding model, or route during retry.

### 6. Audit Logging

Audit logs must be content-light and secret-safe. They should avoid full clinical chunk text unless the backend audit system explicitly requires protected storage.

Record:

- ingestion job ID
- operator or service identity
- reviewer approval identity and timestamp
- source document ID/version/checksum
- package checksum
- chunk IDs and status counts
- backend endpoint/service name, not secret URL tokens
- idempotency key hash, not raw secrets
- request/response status categories
- accepted/rejected counts
- retrieval verification result
- activation or rollback decision

Do not log API keys, bearer tokens, browser profiles, source PDFs, full chunks, cookies, or PHI.

### 7. Partial-Failure Handling

A batch must never be marked successful unless every required chunk has a terminal accepted or unchanged status and retrieval verification passes.

Partial failure states:

- `PENDING_SUBMISSION`
- `SUBMITTED`
- `PARTIALLY_ACCEPTED`
- `RETRY_PENDING`
- `REJECTED`
- `INDEXED_CANDIDATE`
- `RETRIEVAL_VERIFIED`
- `ACTIVE`
- `ROLLED_BACK`

For partial failures:

- Keep the candidate package inactive.
- Preserve per-chunk backend error details.
- Do not move source PDFs to a final completed state.
- Do not activate a partially indexed candidate collection.
- Allow safe replay after correction or backend recovery.

### 8. Retrieval Verification

After backend acceptance and candidate indexing, run retrieval checks before activation.

Verification should cover:

- exact DOI/reference lookup
- exact section title lookup
- clinically meaningful paraphrase query
- important numeric finding query
- negation-sensitive query when the source contains negative or non-significant results
- duplicate/revision exclusion query
- unapproved-content absence check
- citation metadata integrity check

Activation must remain blocked if retrieval verification fails, if unapproved chunks are visible, or if citations point to the wrong source/version.

## Backend Discovery Checklist

Before writing code, obtain read-only or staging-safe answers to these questions:

1. Where is the backend repository or API definition?
2. Is ingestion HTTP, internal service call, CLI job, queue message, or admin-only UI action?
3. What authentication and authorization mechanism is required?
4. Is there a staging environment with production-like retrieval and no production writes?
5. What is the exact request and response schema?
6. Are caller-provided IDs accepted, or does the backend generate IDs?
7. What metadata fields are required and optional?
8. What embedding model and preprocessing are used?
9. Does the backend embed on ingest, or must callers provide embeddings?
10. Which ChromaDB collections exist, and how are active/candidate versions represented?
11. How are duplicates detected and reported?
12. How are updates, deletions, retractions, and superseded documents handled?
13. How does the chatbot filter approved/active content?
14. How can retrieval verification run against candidate content without exposing it to users?
15. What audit records are required for compliance and rollback?

## Implementation Boundaries Once Backend Is Verified

When the real backend contract is available, implement ingestion in a separate module, for example `src/ingestion/`, without changing generation or validation behavior.

Expected module boundaries:

- approval gate loader
- package canonicalizer and checksum helper
- stable ID helper, only if backend-approved
- backend client interface
- retry/idempotency policy
- audit event writer
- retrieval verification client
- tests with fake backend responses

Do not add FastAPI, ChromaDB, LangChain, LangGraph, or direct vector writes unless the verified backend contract requires them and the dependency decision is documented.

## Current Phase 7 Decision

Status: blocked for implementation.

Reason: the real GastroAGI backend is not present in this working copy or available as a saved project in this task. The required endpoint, authentication, metadata representation, embedding model, chunk ID strategy, ChromaDB collections, duplicate behavior, update/delete propagation, and chatbot retrieval behavior are unknown.

Approved work now:

- document backend discovery requirements
- document the approved-only ingestion workflow
- keep review-gated output packages intact
- add tests only for local gates that do not contact a backend

Not approved now:

- production uploads
- staging uploads without a provided staging contract
- direct ChromaDB writes
- invented API routes
- invented database schemas
- invented embedding configuration
- chatbot retrieval modifications

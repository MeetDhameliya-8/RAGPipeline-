# GastroAGI Milestone 6 — Approval and Production RAG Prerequisite Review

Date: 20 October 2026. Result: **BLOCKED before implementation**.

The actual deployed GastroAGI backend or an authoritative production/staging API contract was not available in the inspected checkout or identified in the available project inventory. Milestones 4 and 5 also remain PARTIAL. No approval workflow, ingestion adapter, API, embedding configuration, collection, schema migration or publication mechanism was implemented.

This follows the task's mandatory prerequisite: **“If backend code or a verified contract is unavailable, stop with a BLOCKED report.”** General stack preferences, the local review chatbot and historical target architecture do not establish a production contract.

## Acceptance prerequisite

| Milestone | Status in its existing report | Implication |
|---|---|---|
| 1 — Diagnostics | PASS | Existing offline acceptance recorded. |
| 2 — Throughput | PASS | Existing offline acceptance recorded. |
| 3 — Concept planning | PASS | Existing planning/provenance acceptance recorded; not clinical publication approval. |
| 4 — Editorial generation | PARTIAL | General clinician-ready paraphrase fidelity remains unestablished; three intended examples rejected; qualified clinician review not performed. |
| 5 — Fresh PDF recovery | PARTIAL | Offline reliability tests pass, but clinical prerequisite, authenticated source-review integration and real-PDF/deployed-provider reliability remain unresolved. |

The requirement that Milestones 1–5 have passed their agreed acceptance tests cannot currently be certified. This task did not redefine their acceptance criteria or change their report statuses. The Milestone 5 result of 703 tests plus 9 subtests passed, with 2 deselected cases, is prior recorded evidence; those tests were not rerun for Milestone 6.

## Discovery performed and evidence limits

Read current project instructions and the project RAG engineering skill; reviewed the milestone reports, declared dependencies, README, architecture, ingestion contract, Azure/clinical-validation documentation, project decisions, baseline/TASK11 guidance, local RAG documentation and historical Phase 7 architecture. Inspected current local storage, retrieval, server and pipeline approval-gate source, related test fixtures and test isolation. Checked source/configuration/documentation inventories for backend/API/deployment specifications and consulted the Codex saved-project inventory.

No separate production GastroAGI backend project was identified in that inventory. The relevant available checkout is this PDF-to-draft pipeline. No OpenAPI/Swagger specification, authenticated backend ingestion service, verified production collection definition or staging deployment contract was found in the scoped repository discovery. Unrelated projects were not inspected. This is not a claim that no backend exists elsewhere on the computer or in the organization.

Key evidence:

- `docs/ingestion-contract.md` explicitly distinguishes the local SQLite review store from production and lists production routes/authentication/embeddings/collections/idempotency/retrieval as unverified.
- `docs/engineering/phase7-database-ingestion-architecture.md` is a target design, explicitly conditional on obtaining the real backend contract. Its example strategies are not deployed schemas or endpoints.
- `requirements.txt` declares the PDF/provider/validation/testing/browser stack; it does not declare FastAPI, ChromaDB or a production embedding client. Dependency absence alone would not prove the external backend's technology; current source inspection also found no production adapter to verify.
- `src/local_rag_chat.py:309`, `database_upload_status()`, returns `BLOCKED_UNVERIFIED_BACKEND` with `enabled=False`.
- `src/local_chunk_store.py:99`, `init_db()`, defines local SQLite review tables. `src/local_chunk_store.py:363`, `retrieve()`, retrieves drafts. These are source-code observations; the actual live database was not opened.
- `src/pipeline.py:925`, `build_human_review_gate()`, returns approval required, approved false and ingestion allowed false. This is an output gate, not a durable authenticated approval writer.

A request for the actual backend repository or authoritative API/staging specification was sent to the user during discovery. No backend location or verified contract had been supplied when this report was prepared. No speculative endpoint discovery, network probing, browser login or backend mutation was attempted.

## Mandatory backend contract verification

| Required fact | Actual production verification | What must be supplied/inspected |
|---|---|---|
| Authenticated administrative ingestion routes | **Unavailable** | Deployed-version route/service implementation or authoritative specification; accepted methods and error behavior. |
| Request and response schemas | **Unavailable** | Versioned models, required metadata, validation rules, per-item batch results and readback responses. The five clinical fields alone do not define the transport contract. |
| Database and collection structure | **Unavailable** | Backend persistence models/migrations, ownership boundaries, environment/tenant separation and collection lifecycle. |
| Embedding service/model and dimensions | **Unavailable** | Existing embedding implementation, model/deployment/version, dimensions, normalization, preprocessing and distance metric. |
| ChromaDB integration | **Unavailable** | Confirmation whether production uses ChromaDB; actual client/service path, collection metadata and supported operations. No collection name is inferred. |
| Chunk identifiers and idempotency | **Unavailable** | Backend identity/revision rules, duplicate handling, replay semantics, updates and partial-acceptance behavior. |
| Approval permissions | **Unavailable** | Authenticated reviewer/operator identities, roles/scopes, approval authority, revision binding and revocation semantics. |
| Retrieval filtering | **Unavailable** | Actual production retrieval path and server-enforced approval/hold/revision/tenant filters, including revocation/cache propagation. |
| Deployment and staging | **Unavailable** | Deployed build/version, an isolated staging target and its authoritative configuration and permitted test operations. |

No production contract was verified. Local prototype facts below must not fill any of these unknowns.

## Local approval and retrieval boundary findings

Verified from source, without opening a database:

- The local store is SQLite, with `chunks`, `embeddings` and `ingestion_audit` tables. These are existing local review tables, not proposed or verified production tables.
- Embeddings are deterministic `local_hashing_v1`, **256 dimensions**. They are not a verified production embedding model and do not establish ChromaDB compatibility.
- Local chunk loading assigns `review_status="REVIEW_REQUIRED"`, including technically passing validation. Local storage timestamps and content hashes are not authenticated clinical decisions or approved source revisions.
- The inspected local persistence does not implement the requested durable reviewer identity, approval decision time, approved source revision, fidelity/coverage completion and revocation workflow.
- `retrieve()` selects validation statuses `PASS` and `REVIEW_REQUIRED`, with local embedding algorithm/dimension checks. It does **not** require clinical approval. That behavior intentionally supports internal draft review.
- The BOT page describes itself as “Source-grounded clinical review chat”; status/citation data carries review information, and local database status identifies `production_backend=False` and `review_required=True`. The page also uses a “Clinical Assistant” label, so presentation alone must not be treated as an authorization boundary.
- The server uses `BaseHTTPRequestHandler`/`ThreadingHTTPServer` and defaults to loopback. Its configurable host binding is not authenticated clinical-approval access control. No production identity/permissions integration was found.
- The existing production upload control remains disabled. Nothing was changed to make the draft retriever a production-approved retriever.

**Security/clinical risk:** a deployment or integration that exposed this draft chatbot as approved production knowledge would bypass the required clinical approval filter. The current checkout's disabled production uploader does not prove that an external production chatbot filters correctly. That external boundary is unverified. Do not infer approval from `PASS`, local storage/readback success, source-review attestations from Milestone 5, or the presence of an answer/citation.

The explicit internal review experience is preserved unchanged. Changing it or adding a parallel approval schema before inspecting the real backend would contradict this milestone's stop condition and risk inventing an incompatible authority model.

## Approval architecture and integration outcome

**No new architecture was instantiated.** The following are requirements to bind to the actual backend's verified persistence/authentication patterns, not table names, fields, routes or a claimed implementation:

1. Persist an authenticated reviewer decision bound to the exact source revision and approved chunk content, with decision time and explicit source-fidelity/coverage completion. Content/source changes must invalidate the applicability of an earlier approval according to the backend's verified rules.
2. Enforce approval and absence of source holds at the submission boundary and again in the production retrieval authority. Preserve an explicitly separate internal draft-review experience.
3. Reuse the real backend's ingestion/embedding implementation, identity and replay rules. Partial/unknown outcomes need readback/reconciliation before retry; a successful HTTP response alone cannot establish correct ingestion.
4. Verify stored text/reference/metadata and embedding configuration, then use controlled retrieval questions and negative controls for unapproved, held, superseded and revoked material. Revocation must propagate through the actual retrieval/cache path.

Reviewer authentication, transaction boundaries, idempotency design and revocation propagation cannot be selected safely from the local prototype alone. All implementation stages A–C are **blocked**, rather than simulated against an invented backend contract. No manual Admin Panel copying has been replaced in this milestone.

## Tests and evidence classification

**Milestone 6 tests executed: none.** This was prerequisite discovery and a documentation-only stop. No new test suite or fake production contract was created. Existing relevant unit tests/fixtures were read, not executed. The source of 36 existing Python modules was parsed with `ast.parse` successfully; syntax parsing is not an approval, ingestion or retrieval test.

| Required Milestone 6 scenario | Result this task |
|---|---|
| Unauthorized ingestion/approval | Not implemented or tested: actual authentication/permission contract missing. |
| Duplicate/idempotent ingestion | Not implemented or tested against production/staging. Local draft upsert tests are not production proof. |
| Interrupted batches and partial failure | Not implemented or tested against a verified backend. |
| Incorrect retrieval and approved-content retrieval | Not implemented or tested against a verified production/staging retrieval path. |
| Exclusion of unapproved/held content | External production behavior unverified; local review retrieval intentionally permits drafts. |
| Approval revocation | Not implemented or tested: authority and propagation contract missing. |
| Source-content/revision mismatch | No Milestone 6 approval/ingestion test; prior source checks do not prove production approval revision binding. |

Evidence classification:

- **Static source/document inspection:** performed, findings above.
- **Unit-tested Milestone 6 implementation:** none; blocked before implementation.
- **Staging integration-tested:** none; no verified staging contract/environment supplied.
- **Production-verified:** none; no production requests, writes or retrieval tests performed.

These are unexecuted/blocked scenarios, not passed tests or pytest skips. Historical milestone results are identified separately and are not represented as current Milestone 6 verification.

## Exact files changed and protected state

Only deliverable added: `docs/engineering/GASTROAGI_MILESTONE_6_REPORT.md`.

Inspection evidence added under `tmp/milestone6/`: `inspection-manifest.json` and `protected-verification.json`, containing hashes/counts only. Before/after verification covers **80 existing source, test, prompt, configuration and requirements files**, with zero changed or missing files. There are no implementation changes or new dependencies, routes, collections, database tables or migrations.

The real queue, live database, user PDFs, historical output/checkpoint directories and `.env` were not opened, enumerated for test inputs, modified or migrated. No server/queue worker was started and no pause/resume operation or automatic publication was activated. No Azure request or backend network request was made. The paused queue was left untouched; no claim of a fresh live-database pause-state verification is made.

## Required handoff to unblock

Supply a read-only backend repository location at the deployed revision, or an authoritative versioned API specification plus persistence, embedding, authentication, approval and retrieval contracts. Identify the staging environment and its permitted test boundary; credentials should be supplied through the approved secret mechanism, not pasted into this report/chat.

Resolve Milestones 4 and 5's remaining agreed acceptance requirements or explicitly agree a revised scope that does not equate their offline test success with clinical readiness. Then complete the mandatory verification matrix before implementing A–C. Mocked/isolated staging tests come first; production writes, destructive migrations, queue resume and automatic publishing still require their separate explicit authorization.

**BLOCKED — mandatory production/backend contract unavailable and Milestones 4–5 remain PARTIAL. No clinical approval or production integration was implemented or claimed.**

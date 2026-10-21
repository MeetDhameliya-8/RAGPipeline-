# Ingestion Contract

## Verified Current Behavior

There is no implemented production database ingestion in this repository. The pipeline writes local review artifacts only. The Admin Panel helper can populate browser form fields after validation, verifies filled values, and stops before final Upload.

No code currently creates vector collections, writes to ChromaDB, uploads chunks to a backend, indexes clinical content, or modifies a production database.

## Intended Future Contract

An approved ingestion workflow should require:

- validation status that does not fail
- explicit human approval
- stable chunk identifiers
- idempotency keys
- duplicate protection
- retry-safe submission
- audit logging
- partial-failure reporting
- retrieval verification after ingestion

The five clinical content fields remain the payload contract unless the real backend requires and approves a different schema.

## Unknown Or Unverified Backend Details

The following must be inspected in the real GastroAGI backend before implementation:

- ingestion endpoint and HTTP method
- authentication mechanism
- metadata representation
- embedding model
- chunk ID generation rules
- ChromaDB or other collection names
- duplicate document behavior
- update and deletion propagation
- chatbot retrieval path
- production/staging separation

Do not invent routes, schemas, collection names, or database behavior.

## Prohibited Until Separately Approved

- Uploading real clinical chunks.
- Automatically indexing generated chunks.
- Modifying production database records.
- Treating local ChromaDB experiments as production GastroAGI integration.

## Local Review Database Added For Testing

The localhost Admin/BOT workflow now includes a local SQLite review database at `local_db/gastroagi_chunks.sqlite3`. It stores generated, non-failed chunks with stable SHA-256 chunk IDs, idempotent upserts, audit records, and deterministic local hash embeddings for retrieval testing.

This local database is not the production GastroAGI backend. Stored chunks remain draft content with `REVIEW_REQUIRED` status, and production upload/indexing remains blocked until the real backend contract is verified.

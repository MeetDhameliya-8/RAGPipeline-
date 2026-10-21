# Local PDF RAG Builder

The local PDF RAG builder is a reviewer tool for moving from PDF upload to generated GastroAGI chunks, then checking bot-style answers against those generated chunks.

Run it with:

```powershell
python -m src.local_rag_chat --host 127.0.0.1 --port 8766
```

Open `http://127.0.0.1:8766/admin` for the Chunks Admin Panel. Upload a PDF there and click **Generate chunks** for that PDF. Open `http://127.0.0.1:8766/bot` for the separate BOT Page. Generation uses the existing `src.pipeline.process_pdf` path in `test_generation=True` mode, so the source PDF remains in `input_pdfs/` and outputs are written under `output/<article>/`.

Safety boundaries:

- It accepts PDF uploads only.
- It calls the existing pipeline for extraction, Azure generation, validation, source maps, coverage reports, and review packages.
- It lets reviewers ask deterministic local retrieval questions against generated `chunks.json` outputs.
- It stores generated, non-failed chunks in a local SQLite review database for BOT retrieval. This is local test infrastructure, not production GastroAGI ingestion.
- Production database upload is explicitly blocked until the real GastroAGI backend endpoint, authentication, metadata contract, embedding model, duplicate behavior, and retrieval verification path are inspected and approved.

Generated chunks remain draft clinical content and still require explicit human medical review before publication, Admin Panel upload, vector indexing, or production ingestion.


## Local Review Database

Generated chunks are upserted into `local_db/gastroagi_chunks.sqlite3` when the pipeline produces a non-failed `chunks.json`. The local store uses stable SHA-256 chunk identifiers, idempotent upserts, audit rows, and deterministic local hash embeddings (`local_hashing_v1`) so the BOT page can retrieve relevant chunks without paid embedding calls.

All stored chunks have `review_status = REVIEW_REQUIRED`. Failed validation is not indexed. This local database is ignored by source control and must not be treated as production GastroAGI ingestion.

# GastroAGI Clinical Knowledge Chunk Pipeline

Local pipeline for turning GI/hepatology PDFs into GastroAGI clinical knowledge chunks for human medical review.

## Workflow

1. Put PDFs in `input_pdfs/`.
2. Run `python -m src.pipeline`.
3. Review `output/<article-name>/chunks.md`.
4. Review `output/<article-name>/validation_report.json`.
5. Only after review, use the Phase 3 admin helper to populate the Admin Panel.
6. A human clicks the final Upload button.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

For LLM generation, copy `.env.example` to `.env` and set environment variables locally. Do not commit `.env`.

```powershell
copy .env.example .env
```

Azure OpenAI is the only active inference provider. If the Azure environment variables are missing or invalid, generation fails clearly and writes a failed validation report instead of falling back to OpenAI, Swan, or another provider. Use `--dry-run` for extraction/configuration checks that should not call the LLM.

Project engineering guidance lives in `AGENTS.md`, with supporting references in `docs/engineering/` and the reusable skill in `skills/gastroagi-rag-engineering/`.

## Commands

Process all PDFs in `input_pdfs/`:

```powershell
python -m src.pipeline
```

Process one PDF:

```powershell
python -m src.pipeline --file input_pdfs\paper.pdf
```

Dry run without writing outputs or moving the source PDF:

```powershell
python -m src.pipeline --file input_pdfs\paper.pdf --dry-run
```

Real LLM generation test without moving the source PDF:

```powershell
python -m src.pipeline --file input_pdfs\paper.pdf --test-generation --force
```

Reprocess even if output already exists:

```powershell
python -m src.pipeline --file input_pdfs\paper.pdf --force
```

Validate an existing chunks file:

```powershell
python -m src.pipeline --validate-only output\article-name\chunks.json
```

Start the local PDF upload panel:

```powershell
python -m src.local_admin_panel --host 127.0.0.1 --port 8765
```

Open `http://127.0.0.1:8765` to upload PDFs into `input_pdfs/`. This local panel only queues PDFs; it does not call Azure, generate chunks, upload to the Admin Panel, or write to any database.

Start the local PDF RAG builder:

```powershell
python -m src.local_rag_chat --host 127.0.0.1 --port 8766
```

Open `http://127.0.0.1:8766/admin` for the Chunks Admin Panel. Uploading a PDF runs generation and stores non-failed chunks in the local SQLite review database. Open `http://127.0.0.1:8766/bot` for the separate BOT Page that asks source-bound questions against stored chunks. Production database upload remains blocked until the real GastroAGI backend ingestion contract is verified and approved.

Dry-run the Admin Panel form population:

```powershell
python -m src.admin_uploader output\article-name\chunks.json --dry-run
```

Populate the Admin Panel after human review:

```powershell
python -m src.admin_uploader output\article-name\chunks.json --admin-url https://your-admin-url.example/custom-chunk
```

The uploader fills fields, verifies them, and stops before the final Upload button. It prints `FORM POPULATION COMPLETE — HUMAN REVIEW REQUIRED.` and waits so you can inspect the browser.

## Security

TASK11 P0 reliability changes and the protected offline results are recorded in
[the implementation review](docs/engineering/task11-p0-implementation-review-2026-10-16.md).
CLI exit codes are 0 for completed/skipped/dry-run work, 1 when any job fails,
and 2 for partial/incomplete work without a hard failure. Review-required drafts
are not clinically approved. Current per-unit diagnostics do not imply durable
generation-unit recovery; that remains P1 work.

Offline replay of protected PDFs and existing candidates (no model calls):

```powershell
python -m tools.replay_reliability --metrics <new-runtime-metrics-path.json>
```

Choose a new local metrics destination outside source-controlled documentation.
Existing PDFs/packages and existing metrics destinations are never overwritten
by this runner. Replay is not live Azure generation or clinician approval.

The repository ignores `.env`, auth state, browser profiles, sessions, cookies, logs, input PDFs, processed PDFs, and generated outputs by default. Keep medical source files and generated content in the local workflow unless you intentionally decide otherwise.

## Phase Status

Phase 1 is implemented: project skeleton, PDF extraction, prompt architecture, chunk generation, JSON output, Markdown output, validator, and deterministic tests.

Phase 2 basics are implemented: batch processing, logging, CLI options, failure recovery, duplicate checks, and deterministic tests.

Phase 3 safe form population is implemented with configurable selectors and no automatic Upload click.

Phase 4 clinical generation design is documented in `docs/engineering/phase4-clinical-generation-strategy.md`; implementation remains staged, with current code covering extraction, generation, deterministic validation, and review package output.

Phase 5 validation and hallucination-control design is documented in `docs/engineering/phase5-validation-and-hallucination-controls.md`; Layer 1 technical validation, deterministic Layer 2 source-grounding checks, and Layer 3 `review_package.md` output are wired into the pipeline.

Phase 6 framework selection is documented in `docs/engineering/phase6-framework-selection.md`; no new framework dependency is approved for immediate addition, and simple tested Python modules remain preferred.

Phase 7 database ingestion architecture is documented in `docs/engineering/phase7-database-ingestion-architecture.md`; implementation is blocked until the real GastroAGI backend contract is inspected, and no database writes or uploads are performed.

Phase 8 testing and quality benchmarks are documented in docs/engineering/phase8-testing-and-quality-benchmarks.md; routine tests use mocked responses, while live Azure connectivity and full clinical generation remain opt-in with cost controls.








### Practical draft ingestion

The Admin Panel now drafts coherent, source-traceable evidence packages and stores non-failed chunks in the local SQLite review database. When native text is insufficient, its configured Azure deployment can compare two source-page transcriptions under a bounded visual fallback. These are paid Azure calls during generation; source/clinical review remains mandatory.

Uploads start automatically while the queue is unpaused. A server supervisor checks saved work every two seconds, maintains worker heartbeats, and resumes interrupted work after a 120-second expired lease. Closing the browser does not stop processing; the Python server must remain running. Restarting the server resumes pending work. Pause prevents new jobs from starting and survives restart.

Temporary Azure transport/rate-limit errors and malformed responses receive up to four saved delayed retries (30, 120, 600 and 1800 seconds), for five queue attempts total. Completed unit candidates and page transcriptions resume from compatible caches. Authentication/configuration/quota problems need correction in Azure or configuration; unreadable or unverified sources need source review. The Admin shows the reason and offers Retry Job without requiring a code change. Retries never override clinical validation.

Page-image recovery reads every selected page lacking a usable native prose package, sequentially with the existing per-page rendering budget and two-pass corroboration. It does not stop after the first usable page or after page three. Whole-document coverage remains explicitly incomplete: supported prose packages do not certify tables, figures, every claim or clinical accuracy. Document request counts can grow with the source; per-request size remains bounded. Azure usage and cost grow with pages and generated packages.

New practical output reports declare PDF/content-based local storage identities. Their chunk IDs survive output-folder changes and renumbering, preventing repeated retries from inserting the same content again. Historical output IDs are preserved; this is not a migration of historical database duplicates. Content remains review-required in the local database; production integration is still separate.

Source preparation now has checksummed native/visual checkpoints under `output/.preparation-cache/`. Reuse requires matching PDF bytes, filename, preparation implementation and dependency versions; visual preparation also binds the configured Azure deployment. A compatible completed visual checkpoint skips both extraction and page rendering on restart. Invalid checkpoints are rebuilt from the PDF. Dry runs do not write these caches.

Image routing records a reason per page. It retains native packages, recovers damaged/sparse text and unresolved prose/paragraph order, and avoids new image requests for reference-only and other non-prose review holds. Previously completed transcriptions remain reusable without a new request. Transcription uses at most two concurrent requests, retaining two-pass agreement and existing Azure backoff. Drawing-region warnings are indexed and scoped to avoid quadratic bookkeeping growth; every source drawing and unresolved warning remains in the evidence map. See [the hybrid performance review](docs/engineering/hybrid-performance-review-2026-10-17.md) for measured results and limits.

Use `python -m src.local_rag_chat --host 127.0.0.1 --port 8766` and open `/admin` or `/bot`. For the practical CLI path, use `python -m src.pipeline --file <PDF> --draft-packages --test-generation`. Add `--dry-run` instead of `--test-generation` for an offline native planning check; dry-run does not perform visual transcription or generation.

Compatible completed package attempts can resume; localized failures and rejected candidates are retained separately. Source-review holds remain explicit. See `docs/engineering/practical-pdf-ingestion-review-2026-10-17.md` for the implementation and limitations.

Practical drafting now checks the source citation before paid reading, uses a dedicated passage prompt, and generates at most two independent packages concurrently. Bibliographies and typed "no suitable source passage" responses remain review holds rather than repeated paid retries. Existing v1 source maps remain readable; v2 planning recognizes reference headings inside larger blocks. See [the latency and rejection review](docs/engineering/ingestion-latency-review-2026-10-19.md) for verification and timing limits.

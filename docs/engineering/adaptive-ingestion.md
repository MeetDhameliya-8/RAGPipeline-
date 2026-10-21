# Adaptive ingestion, 2026-10-16

The ingestion entry points still call `extract_pdf` and `generate_chunks`. An
internal deterministic planning layer now sits between extraction and Azure
generation. No new HTTP endpoint, persistent database schema, or chunk content
field is introduced.

## Implemented behavior

- `src.adaptive_ingestion` defines typed configuration, plans, and source contexts.
- Text-derived document-type hints distinguish guidelines, systematic reviews,
  qualitative studies, narrative reviews, and original research. These are
  heuristic routing hints and never authoritative citation metadata.
- Contexts use paragraph boundaries and then sentence boundaries for oversized
  prose. The default source budget is 20,000 characters per request. The default
  limit is 80 requests per document. These are character budgets, not guarantees
  about the deployed model's token limit; prompts, metadata, and output need
  additional capacity.
- Every cleaned-source character belongs to one planned context, with explicit
  offsets and a source hash. Planning coverage does not prove clinical coverage.
- Oversized table/figure-like units and indivisible text are blocked instead of
  being sliced into unrelated cells. Tables below this threshold still need
  source-grounding and human review; this is not a table reconstruction engine.
- Sparse visual pages and severely unresolved text encodings receive explicit
  OCR/layout-review blockers before provider calls. This conservative route can
  include a nonclinical illustrated cover. There is no automatic page exclusion.
- Long documents call the existing Azure provider once per planned context.
  Each response follows the existing schema and generation checks. Exact
  duplicate chunks are removed and chunk numbers are reassigned consecutively.
  A failed context rejects the complete generation operation; no partial
  success is returned.
- Article identity hints are searched only in the first two pages, before a
  detected References heading. This prevents the observed late reference-list
  DOI substitutions. It is a conservative heuristic, not a bibliographic
  verifier: front-matter footnotes and unusual publication layouts still need
  review, and legitimate metadata outside these pages remains unknown.
- Excessive whitespace inflation lowers extractor quality scores. Unresolved
  `/C` font codes are now included in quality checks.
- The validation diagnostics include the adaptive plan without source context
  text. The existing five content fields, validators, and human-review gates
  remain in place.

## Validation

Use `python -m pytest -q`. New deterministic tests cover complete source-span
coverage, bounded contexts, sentence boundaries, oversized tables, context
limits, content-derived routing, sparse/encoding routes, no-call blockers,
multi-context merge, failure behavior, duplicate handling, source DOI scope,
and whitespace inflation. Tests mock generation; no live clinical Azure
requests are required.

`python -m src.pipeline --file <pdf> --dry-run` reports the adaptive plan in
diagnostics without making provider calls. For multi-context generation, the
request count can be substantially higher than the old single request.

## Still required for format-independent ingestion

1. A layout intermediate representation carrying blocks, bounding boxes, reading
   order, language, tables, headers, footnotes, and page identity.
2. An intentionally configured local OCR or Azure document/vision adapter for
   raster content. This implementation does not OCR or send page images.
3. Table row/header/footnote binding, continued-table joins, and explicit diagram
   and flowchart reconstruction, with independently verified evidence.
4. Boundary context across generation requests, context-aware deduplication,
   and section-level persisted generation checkpoints. Existing source-map
   checkpoints are not generation-batch checkpoints.
5. Bibliographic verification for metadata outside front matter, multilingual
   column separation, safer reference/appendix discrimination, and a semantic
   coverage evaluator. Cleaner losses cannot be recovered by batching.
6. An approved live Azure benchmark with clinician-reviewed expected facts and
   retrieval queries. Extraction and mocked-generation success do not establish
   clinical accuracy.

The corpus audit measures 30 PDFs and 857 pages. All were processed through
the baseline extractor and source mapper, with representative visual checks
and table probes. This is an ingestion engineering audit, not clinical review
of every claim. No production ingestion or source PDF movement is involved.

Official extraction references consulted:

- https://pymupdf.readthedocs.io/en/latest/recipes-text.html
- https://pypdf.readthedocs.io/en/stable/user/extract-text.html

Local runtime checked: Python 3.13, PyMuPDF 1.27.2.2, pypdf 6.9.2,
Pydantic 2.13.5. Exact installed APIs were inspected locally.

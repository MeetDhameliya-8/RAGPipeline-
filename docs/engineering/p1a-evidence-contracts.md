# P1-A minimal canonical evidence contracts

Design recorded before tests and implementation, 2026-10-16. This foundation is an internal sidecar, not a production schema or a new generation planner.

## Contracts

- `EvidenceDocument`: PDF byte SHA-256 (unknown for legacy text-only callers), identity quality, inspectable configuration plus extractor/version/configuration fingerprint, page registry, named text buffers, transformation mappings and unresolved evidence warnings.
- `TextBuffer`: explicit ID, stage (`raw`, `cleaned`, `canonical`, `section_map`), exact Unicode text and SHA-256. Canonical text is exactly the existing public `full_text`; no new cleaning policy is introduced. Native page candidates and native geometry blocks remain independently identified.
- `EvidenceSpan`: buffer ID and half-open Python Unicode-code-point offsets. These are never PDF-byte offsets. Every span must round-trip by slicing its named buffer.
- `SpanMapping`: source span (or no source for inserted separators), target span and relation (`exact`, `transformed`, `inserted`, `deleted`). Exact mappings require equal text. Transformed mappings identify the complete original region, not an invented character correspondence. Deletions remain recorded. Unmapped content must be visible.
- `EvidencePage` / `EvidenceBlock`: document-scoped page/block IDs, native extraction method/options, candidate buffer IDs, selected candidate/reason, coordinates/rotation/native order when available. Geometry is retained from native APIs; a sorted string is not proof of correct reading order. Text-block and visual-region relations remain unknown unless directly observed.
- `EvidenceWarning`: stable ID, page/optional source span/block, code, critical flag and unresolved status. Table/figure/sign/unit/order/disagreement signals record uncertainty, not reconstructed relationships. Critical unresolved evidence prevents complete-source coverage regardless of processed-section counts.
- Generation units retain the current batch text/ranges and gain an internal named ownership span, unit ID, warning IDs and an empty context-dependency list with `not_planned` status. No concept, table or cross-section dependency is guessed.

## Integration

1. `extract_pdf` keeps all existing public fields/text/selection behavior. Add a JSON-serializable `evidence` sidecar. Retain both native candidates; record pypdf layout/plain fallback and PyMuPDF native geometry without OCR or image interpretation. Hash source bytes and verify the file did not change during extraction.
2. Map selected page raw text to current page-cleaned text, join cleaned pages with explicit separators, then map the joined buffer to unchanged `full_text`. Mapping describes actual string transformations; ambiguous replacements are region links. Alternate candidate/block buffers are not silently treated as the selected text.
3. `build_source_map` keeps section detection/text behavior. Name its reconstructed section-map buffer explicitly rather than pretending those offsets index `full_text`. Link it to the common evidence sidecar and expose traceable source spans. Legacy text-only dictionaries work, with unknown PDF identity explicitly recorded.
4. `build_ingestion_plan` keeps current paragraph/sentence boundaries, budgets and blockers; annotate ownership/dependency metadata against canonical text. No planner rewrite or persisted unit recovery.
5. `chunk_generator` adds unit provenance to existing diagnostics only. Azure request content and five clinical fields are unchanged. `pipeline` includes evidence/coverage summaries and persists the internal sidecar with source-map review artifacts. Coverage distinguishes string coverage, unresolved critical evidence, section processing and clinical review.
6. Readers without the additive fields retain their existing keys. Existing source-map JSON can still be read using explicit legacy/unknown defaults; it must not be upgraded to verified provenance or complete coverage by default.
7. Full PDF extraction resolves citation-span buffer IDs to retained native candidates, preserves the previous page alias as `legacy_buffer_id`, and verifies exact text slices before returning. The standalone citation helper retains page-local aliases because it has no document registry. Conflicting candidate metadata is bound recursively; unresolved bindings fail extraction rather than inventing a source.

`source_trace` retains `exact` versus `region` precision alongside section source spans. Exact means character equality, not clinical verification or proof of a unique occurrence in repeated text. Non-whitespace transformations, including deletions, require review. Geometry remains native evidence; visual inventory failure preserves available text geometry and adds a critical warning. Unit warning IDs have document scope, not a claim that each warning affects every owned character.

## Verification plan

Add failing tests first for buffer integrity, cross-page traces, removed/changed characters, synthetic separators, section/canonical coordinate separation, renamed-identical PDF identity, configuration changes, native multi-column geometry, encoding/sign/unit uncertainty, missing figure evidence, extractor disagreement and unknown dependency metadata. Test that marking every section processed cannot complete coverage with unresolved critical evidence.

Use real synthetic PDFs for geometry and deterministic native extraction, mocks for explicit failure/disagreement cases, and the protected 30-PDF corpus for before/after text/hash/batch/provenance checks. Preserve all existing outputs and PDFs. The five NEJM fixtures remain mandatory release requirements and cannot be claimed verified while absent. No live Azure requests, OCR, extra agents, production writes or recovery implementation.

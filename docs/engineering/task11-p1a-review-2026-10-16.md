# TASK11 P1-A implementation review

Date: 2026-10-16. Engineering implementation complete; stopped at the P1-A review gate. This is not production deployment, clinical validation, or authorization to proceed to durable recovery.

The root instructions, project engineering skill, current P0 report, TASK11 flowcharts and stepwise plan, extraction/source-map/planner consumers, provider boundaries, prompts, dependencies and tests were inspected. The existing source-first clinical checks, Azure transport, mandatory human review and five content fields are preserved.

## Contracts and integration

The [contract document](p1a-evidence-contracts.md) was recorded before regression tests and implementation. New internal Pydantic contracts live in `src/evidence.py`:

| Contract | Meaning |
|---|---|
| `EvidenceDocument` | PDF-byte SHA-256, document identity, inspectable extractor/configuration identity, page/buffer/mapping/warning registries. Legacy text callers explicitly lack verified PDF identity. |
| `TextBuffer` | Independently named raw native candidates, cleaned pages/document, unchanged canonical generation text and reconstructed section-map text; exact text and content SHA-256. |
| `EvidenceSpan` | Named buffer and half-open Unicode code-point offsets. These are not PDF-byte offsets. |
| `SpanMapping` | Exact character-preserving links, coarse transformed-region links, inserted separators and retained deletions. Validation checks bounds, hashes, configuration, exact slices and complete contiguous target accounting. |
| `EvidencePage`, `EvidenceCandidate`, `EvidenceBlock` | Byte-scoped page identity; both native extractors, actual selected extraction mode, selection rationale; page size/rotation, text-block boxes/direction/native order and uninterpreted visual boxes. |
| `EvidenceWarning`, `EvidenceTrace` | Explicit unresolved critical evidence; exact versus region trace precision, synthetic spans and unmapped spans. |
| Existing generation batch/plan extensions | Owned canonical span, deterministic unit identity including content/configuration, document-scoped critical warning IDs, empty context dependencies explicitly marked `not_planned`. No new planner. |

The path is now PDF bytes → unchanged native extraction/selection → raw candidate buffers → unchanged page cleaning → cleaned page/document buffers → unchanged article cleaning → canonical `full_text`. Section detection retains its own behavior and names its reconstructed buffer separately, linking it to cleaned-page evidence. Bounded generation owns canonical ranges, while diagnostics retain provenance outside the five content fields.

Native dictionary geometry uses installed PyMuPDF APIs without image payload extraction or interpretation. Image and drawing inventories supply uninterpreted regions. Available text geometry survives visual-inventory failure. No table cells, figure meanings, caption links, footnote associations, signs or units are reconstructed.

## Exact change boundaries

| File | Functions / models changed or introduced |
|---|---|
| `src/evidence.py` (new) | Contracts above; `text_hash`, `span`, `add_buffer`, `align_buffers`, `join_buffers`, `warn`, `build_evidence_document`, `_add_layout`, `get_evidence`, `_check_span`, `validate_evidence`, `trace_to_raw`. |
| `src/pdf_extractor.py` | `extract_pdf`: byte fingerprint/consistency check, retain both candidates, attach sidecar and bind citation provenance. `_extract_page_text`: optional mode diagnostics while preserving its string return. `_extract_pymupdf_pages`: retain actual mode plus native geometry. New `_native_page_layout`, `_bind_reference_spans`. `extract_document_reference_metadata`: retain extractor identity on citation spans. |
| `src/source_map.py` | Additive fields on `SourceSpan`, `SourceSection`, `SourceDocument`; `build_source_map` names the actual section buffer and stores raw traces; `source_sections_for_validation` includes provenance metadata; `build_coverage_report` validates evidence and refuses complete coverage with unresolved critical evidence or missing legacy evidence. |
| `src/adaptive_ingestion.py` | Additive fields on `GenerationBatch`, `IngestionPlan`; `build_ingestion_plan` annotates ownership/identity/warnings without changing batching. |
| `src/chunk_generator.py` | `generate_chunks`, `_generate_single_batch`: plan/unit provenance in diagnostics; provider input and output contract unchanged. |
| `src/pipeline.py` | `build_generation_diagnostics`: canonical/document/configuration/unit identities and critical warning IDs. Existing `save_outputs` and failure-artifact serialization already persist the extended source map; those functions needed no changes. |
| `tests/test_canonical_evidence.py` (new) | 23 offline regressions described below. Existing test files and original NEJM expectations are untouched. |
| `tools/check_evidence_provenance.py` (new) | `check`, `main`: read-only corpus comparison, reference/source offset checks, artifact-size measurements and protected-file verification; new metrics destinations only. |
| Documentation (new) | `p1a-evidence-contracts.md` and this review. |

No dependency, prompt, provider implementation, public clinical schema, database, approval or production ingestion change was made.

## Backward compatibility

Public extraction remains a dictionary with its existing fields and unchanged text/selection behavior; the evidence sidecar is additive. Canonical text remains exactly `full_text`. Source sections retain their existing text, IDs, detection, hash and offsets; the previously implicit reconstructed coordinate system now has a buffer identity. Existing checkpoint identity/reuse rules are unchanged; generation-unit recovery is not implemented.

Citation provenance identifiers in full PDF extraction are upgraded from page aliases to actual native buffer identities. Previous identifiers remain in `legacy_buffer_id`, and all exact matching native candidates are listed. A caller comparing literal page aliases should use that compatibility field. The standalone citation parser retains its existing page-alias contract. Bibliographic reference strings and reference-selection behavior are unchanged.

Old source-map JSON loads with `evidence=None`, unknown buffer identity and empty source traces. Its processed-section counts cannot imply verified complete source coverage. New internal artifacts have additional metadata and larger files; this is not an exact JSON-byte compatibility guarantee.

Generation text, budgets, batch boundaries and current extraction blockers remain unchanged. New uncertainty warnings prevent a complete-source-coverage claim; they do not weaken clinical validation or authorize publishing a draft. `source_coverage_complete` measures this internal coverage state, never clinician approval. Context dependencies remain unknown, rather than being inferred as absent.

## Test-first execution and actual results

The first 12 new regressions failed against P0 before implementation. Subsequent targeted tests reproduced gaps in non-whitespace loss detection, unselected-candidate encoding, raw-only legacy inputs, trace serialization, citation aliases, configuration integrity, mutated offsets, unit identity and partial visual-inventory handling before their corresponding fixes. Configuration variation and missing-mapping tests also guard invariants already established by the foundation.

Final full suite command:

```powershell
python -m pytest -q -p no:cacheprovider --tb=short
```

Result: **361 passed, 2 skipped, 9 subtests passed, 20.89 seconds**. P0 baseline was 338 passed, 2 skipped, 9 subtests. All 23 new tests pass. No live Azure request was made.

Verified installed environment: Python 3.13.7, pypdf 6.9.2, PyMuPDF 1.27.2.2, Pydantic 2.13.5, pytest 9.1.1 and OpenAI SDK 3.26.1. Existing dependency declarations were not changed. Native geometry APIs were checked against the installed package and the official [PyMuPDF Page](https://pymupdf.readthedocs.io/en/latest/page.html) / [TextPage](https://pymupdf.readthedocs.io/en/latest/textpage.html) documentation; no provider SDK capability change was needed.

The two skips are the unchanged original five-NEJM fixture-reference test and the ABC-02 fixture-dependent assertion. The original five NEJM PDFs remain unavailable; their expectations are mandatory release requirements and remain unverified. Passing synthetic/corpus tests does not close that release gate.

New tests cover named-buffer slicing and hashes; cleaned/transformed/deleted text; cross-page traces and synthetic separators; reconstructed-section versus canonical offsets; ownership/dependency metadata; byte-identical renamed PDF identity; configuration changes/integrity; broken encoding including discarded candidates; real native multi-column geometry; incomplete figure evidence; extractor disagreement; missing mappings and tampering; legacy raw-only inputs and old JSON; citation-span binding; unit identity tied to actual text; and partial visual-inventory failure.

Real synthetic PDFs exercise native extraction and geometry. Controlled monkeypatches exercise disagreement and API failure. The existing mocked generation/provider suite verifies unchanged request processing, failure rejection and five-field output shape. The 30 real local PDFs exercise extraction/provenance, not newly generated clinical output. Approved live Azure tests were not run.

## Before/after source-provenance verification

Before changing source implementation, a runtime-only P0 snapshot recorded every corpus PDF's byte hash, raw/canonical text hashes, page/section counts, batch ranges, blockers and reference identity. A separate manifest protected all 302 existing input, processed and output files. No PDF or generated clinical text is embedded in this report or the verification tool.

Across 30 PDFs / 350 pages:

| Check | Observed result |
|---|---|
| PDF bytes, raw text, canonical generation text | All 30 unchanged by SHA-256 comparison. |
| Page counts, section counts, batch ranges, blockers, selected reference string | All 30 unchanged. |
| Reconstructed section slices | 314 / 314 match their explicitly named section-map buffer. |
| P0 section offsets sliced against `full_text` | Only 19 / 314 matched; this demonstrates the prior coordinate-system ambiguity, not loss of the other source paragraphs. |
| Native candidates | 700 retained: both extractors for all 350 pages. |
| Native text/visual geometry records | 5,970 retained; visual regions are not asserted to be clinical figures. |
| Canonical source tracing | No unmapped spans in any document. Transformed regions and synthetic separators remain distinguished from exact character links. |
| Citation/candidate span bindings | 90 / 90 exact named-native-buffer slice checks, including nested conflict metadata. Counts include the two metadata roles, not 90 unique bibliographic sources. |
| Critical warnings with every section artificially marked processed | All 30 still report incomplete source coverage. |
| Existing source PDFs and output artifacts | All 302 protected files remain byte-identical. |

Critical signals include table relationships, figure/visual content, unverified reading order, encoding/sign/unit uncertainty, extractor disagreement, existing sparse-page blockers and non-whitespace cleaning/section transformations. These are review signals, not automatically adjudicated diagnoses of missing clinical facts. Native drawings include borders/logos, so conservative visual warnings can be false positives.

The verification CLI exits unsuccessfully on changed legacy results, out-of-buffer/invalid mappings, unmapped canonical spans, invalid citation slices, altered protected files or complete coverage despite critical warnings. Runtime metrics are kept in the local temporary audit directory, separately from source and historical outputs.

## Acceptance status, limitations and review decision

P1-A engineering acceptance requires unchanged legacy text/routing/reference identity for every corpus document, 100% named section and citation slice checks, zero unmapped canonical spans, no false complete coverage with critical evidence, unchanged protected files and the passing existing suite. These checks are met; original NEJM fixture verification remains an open mandatory release requirement.

Character equality is not a clinical interpretation or proof of a unique causal occurrence when repeated text allows several alignments. Token alignment is deterministic and uses frequent-token filtering to avoid padding-driven matching blowups; replacements are coarse source-region links. Non-whitespace edits remain critical/unreviewed. This foundation does not certify the cleaner's clinical completeness or automatically resolve any warning.

Measured compact source-map serialization is approximately 2.21–21.27 MB per document, 343.54 MB across the corpus. Existing indented artifact serialization can be larger. The measurements serialize in memory; the audit did not write these review packages over historical outputs. Full-document traces contain 96,244 exact and 98,366 region fragments, not independent clinical claims. These numbers expose substantial metadata overhead and coarse mapping; artifact compaction and warning precision need review before broad adoption. No size acceptance target was supplied, so this is an explicit adoption risk rather than a claimed performance pass.

Reading order is not certified. Native geometry is independent evidence from the native dictionary; its text is not silently equated with the selected extractor's text. Table/header/footnote and figure/caption associations stay unknown. Empty context-dependency lists have `not_planned` status. Existing old output reuse is preserved and does not retroactively enrich historical artifacts; a later authorized run needs a new review artifact/version rather than overwriting prior results.

The flowchart's source-grounded ingestion foundation is stronger, but concept planning, visual evidence resolution, durable generation-unit recovery, independent clinical verification models, production RAG integration and deployment remain outside this checkpoint. No OCR, external document service, extra agent, LangGraph or new production schema was introduced.

**Stopped at the P1-A review gate.** Evaluate warning precision, artifact overhead, offset/tracing behavior and the missing NEJM fixtures before authorizing the next increment.

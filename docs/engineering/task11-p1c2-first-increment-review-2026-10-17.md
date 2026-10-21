# TASK11 P1-C2 first increment review

2026-10-17. Stop at the first P1-C2 implementation gate. This is an opt-in, offline native-layout increment. Default pipeline extraction, Azure generation, clinical validation, human approval and production boundaries remain in place.

**One previously blocked real two-column paragraph now reaches draft generation through seven corroborated native lines. This is a source-recovery improvement, not general PDF adaptability or a clinically accepted chunk. Across all 30 PDFs, the legacy view has one eligible unit and the alternate views also have one, in a different PDF. Replacing every legacy view with its alternate would lose the original candidate. No automatic replacement or aggregate throughput increase is claimed.**

The new candidate's faithful mocked output is 55 words and fails the unchanged minimum-length check. Clinical acceptance and approval remain zero. The existing 150–250-word target is guidance; the hard minimum causing this failure is 90 words. Neither threshold was changed, and no padding was added.

## Design and verified boundaries

The [first-increment design](p1c2-first-increment-design.md) was recorded before implementation. Root instructions, the project engineering skill/safety/testing references, current extractor, evidence, planner, source-map, generator/provider/validator consumers, P1-C1 report and tests were inspected. The existing TASK11 flowcharts remain target-state guidance; this work does not activate their ingestion/publication stages.

The original problem has two parts: selected native page text can interleave columns, and whole-passage agreement cannot establish support for a reconstructed paragraph when both page-level extraction views interleave it. Neither a longer extracted string nor a geometry sort alone proves clinical validity.

New local APIs in `src/native_layout.py`:

- `inspect_pdf_routes(path)` returns native evidence and explicit held/unavailable routes, including for an image-only PDF. It does not fabricate usable text or activate a capability.
- `recover_native_layout(extraction)` returns a separate extraction dictionary with a versioned alternate view. The original dictionary, raw candidates, original canonical/cleaned buffers, mappings and warnings remain available. It is not called automatically by `process_pdf`.

Native dictionary text blocks are ordered through bounded recursive whitespace cuts: column gaps, then horizontal bands. The generic limits are 6 PDF points for a column gap, 3 for a row gap, 1,500 blocks and depth 64. Narrow-gutter regression testing exposed the initial 12-point threshold as overly restrictive. No rule names a publisher, journal, filename, trial or clinical value.

All native text blocks on a reconstructed page are retained exactly. Overlapping text boxes, unresolved page rotation, incomplete geometry or a layout without an unambiguous whitespace cut hold that page. Other pages may still produce alternate views. This increment does not split/repair an internally interleaved native block, remove overlapping author/affiliation material, or infer cross-page reading relationships. Rotated regional text remains represented and cannot acquire a horizontal local certificate.

Reconstruction preserves native hyphens, ligatures, signs, units, control glyphs, references and boilerplate. Separators are mapped as synthetic whitespace. No medical text is repaired from model knowledge. Restored raw material may require later review/cleaning; it is not a raw-text fallback presented as generated clinical content.

## Internal evidence contracts

| Contract | Meaning and validation |
|---|---|
| `EvidenceLine` | Named native-block span, PDF bounding box and direction. Native line ordering is retained through ordered spans. Missing supported geometry is not invented. |
| `EvidenceRoute` | Versioned page/region route, status, source spans/block IDs, linked warnings, reasons, original/alternate buffer IDs and measured comparisons. |
| `NativeLayoutView` | Original and alternate canonical/cleaned IDs, selected page buffers/routes, versioned algorithm configuration and its hash including extractor versions. Original evidence identity/configuration remains intact. |
| `NativeLineSupport` | One native line span and exact, unique corroboration spans in both retained extraction families. |
| `NativeSupportProof` additions | `native-block-lines-v2`, ordered line supports, and explicit matches back into the retained original canonical view where available. The existing whole-passage v1 proof remains supported. |

Every source span uses a named immutable buffer and half-open Unicode offsets. Comparison uses whitespace normalization only; no medical characters are normalized or inferred. Read-back recomputes route block ownership, exact output, deterministic ordering, comparisons, view identities, selected page-buffer associations and source certificates. Omitted/duplicated blocks, changed comparison counts, duplicate routes and modified view/certificate spans reject.

Original canonical text and its mappings are never overwritten. Alternate canonical text has its own content identity and exact mappings through alternate page buffers to native blocks. All 4,666 original buffers in the audit source maps, and all their mappings, were retained unchanged. Original references remain bound to their original source evidence.

Legacy expanded/compressed JSON remains readable with empty/default new metadata. The gzip/checksum/size-bound persistence format is unchanged. Forward compatibility with an older checkout is not promised for new sidecars.

## Routes and uncertainty

| Route | Current local outcome |
|---|---|
| Native text | Retained native view; an empty native view is explicitly held. |
| Native layout | Proposed reconstruction or held with exact geometry/order reason. Reconstructed means an alternate evidence view exists, not clinically verified page order. |
| Table relationship review | Caption/mention region retained; headers, treatment arms, cells, units and footnotes are not inferred. Remains held. |
| Figure review | Caption/page region retained with unresolved clinical interpretation. Remains held. |
| Visual classification review | Native drawing/image signal remains unclassified. A drawing is not automatically a clinical figure or a decorative element. |
| OCR required | Image-only extraction retains diagnostic evidence; the inspection API returns unavailable/unapproved OCR with page/image regions and empty text. No generation request is allowed. |

Missing visual geometry is explicitly labeled unlocalized; a conservative whole-page box is not asserted to be the exact object region. Page and native-block source IDs are retained where available. No OCR, visual model or specialist document service was verified/activated, and no paid call was made.

## Eligibility and clinical safety

Recovered text remains subject to the existing concept ownership, role, scope, group/referent, qualification, context and request-budget gates. Where a whole passage is absent from interleaved page candidates, a v2 local certificate requires:

1. One uniquely matched horizontal native text block with valid geometry and complete sentence boundaries.
2. All non-whitespace owned characters accounted for by native lines.
3. Each contributing line uniquely corroborated, in increasing order, by both pypdf and PyMuPDF native outputs. Repeated/ambiguous matches, conflicting values or unavailable line geometry reject.
4. Existing clinical scope/relationship checks. Geometry does not establish an arm, endpoint, table relationship or a missing qualifier.

The certificate and scoped warning decisions are recomputed on generation/load. Original-view warning locations can be independent only when explicit matching original-view spans establish disjoint support; changing canonical buffer IDs cannot hide a material overlap. Unlocalized critical gaps remain blocked. A negative test uncovered a later font-code loophole when an earlier warning was elsewhere on the page: local support now rejects all observed font-code/control/encoding patterns in the passage, rather than relying on the first warning being exhaustive.

All original warnings remain unresolved. Applicable warnings are either still blocking or recorded as independently scoped by positive source evidence. Additional source-map transformation warnings remain visible. Full-document completeness cannot follow from successful reconstruction, and none of the 30 documents becomes complete even when all source sections are marked processed.

Azure transport, strict raw-response/schema validation, reference binding, numerical/source checks, length rules, five content fields, human approval, storage boundaries and production restrictions were not relaxed. Unsupported numerical claims still fail the actual validator. No durable unit attempts, resume or production RAG integration was added.

## Real-source anchors and before/after evidence

The reviewed PDF has SHA-256 `7b1e531d05683af67766e2d8b45ad0dbbdee84aa1be54c9b44c13aeeb7de934c`. Its page 3 was rendered with Poppler and checked independently of the reconstruction output. The positive paragraph occupies the lower right column; its two analysis-population definitions and restrictions remain within the recovered sentence. A nearby damaged operator remains unresolved.

| Check | Before | After |
|---|---|---|
| Positive paragraph | Other-column material interleaves; complete normalized passage absent from selected canonical text | Seven native lines, fourteen corroboration spans; exact native characters retained |
| Owned canonical range | No contiguous equivalent | `[19419, 19799)` in separately named alternate buffer |
| Paragraph text hash | Not a contiguous passage | `da9b59ea6361f5608020b8b8a37d9e03243a904e38f2a16b16fc7ed5a8a22cf6` |
| Draft-eligible units in this PDF | 0 | 1 |
| Nearby damaged operator | Unresolved | Still the original control code point, still blocked; no visual-to-numeric repair |
| Source qualification ownership | Fragmented | Entire restriction remains owned and required |
| Mock Azure SDK calls / live calls | — | 1 / 0 |
| Reference binding / output fields | Existing protections | Verified reference unchanged; five content fields plus numbering |
| Actual validator, faithful quote | — | `FAILED`, `WORD_COUNT` only: 55 words |
| Added unsupported numerical claim | — | `FAILED`, source-fidelity error |
| Document complete / clinically approved | False / 0 | False / 0 |

Regression metadata in `tests/fixtures/native_layout_anchors.json` records PDF, page, block/span, geometry and text hashes, not medical source text. The tests require the original hash, recovered positive anchor and unchanged blocked negative anchor. If this real fixture becomes unavailable, the tests skip explicitly as an unresolved release check. It was present and all three real-source tests passed in this run.

This is an engineering visual/source check with independent extractor corroboration. It is not independent clinician adjudication. No additional human reviewer, clinical approval or representative clinical error rate is claimed.

For review, original/alternate source excerpts are in the new local audit's `before-after-source-evidence.txt`; the rendered original page is in the local visualization directory. No PDF, source excerpt or generated medical output was embedded in repository documentation or source. Runtime mock output is clearly identified as an unapproved, validation-failed quotation.

## Thirty-PDF results and resource observations

| Measure | Legacy view | Alternate view |
|---|---:|---:|
| PDFs / pages inspected | 30 / 350 | 30 / 350 |
| Generation/adjudication units | 6,629 | 5,454 |
| Eligible draft units | **1** | **1, in a different PDF** |
| Reconstructed native page proposals | — | 139 |
| Held native-layout pages | — | 159 |
| Default native-text pages | — | 52 |
| Source-map warning records, including nonblocking records | 4,323 | 4,638, all originals retained |
| Unresolved coverage-blocking warnings | 4,167 | 4,482, all originals retained |
| Original buffers/mappings retained unchanged | — | 4,666 buffers and every original mapping |
| Original PDF/raw/canonical/reference baseline differences | 0 | 0 for default extraction |
| Alternate canonical characters explicitly owned | — | 3,087,617 |
| Ownership gaps / overlaps | — | 0 / 0 |
| Exact compressed map read-back | — | 30/30 |
| Incorrect complete-source reports | 0 | 0 |
| Modified protected input/processed/output files | 0/302 | 0/302 |
| Real clinically accepted chunks / approvals | 0 / 0 | 0 / 0 |

The legacy view's existing candidate is ineligible in its alternate view. The old view and its valid source evidence remain available, and default processing is unchanged. Two distinct PDFs have a candidate across the two selectable views, but no automatic view union, deduplication or selection policy was implemented. **The alternative views alone do not increase aggregate eligible-unit count.**

The extra characters include raw line wraps, references, boilerplate and other material absent from the prior cleaned canonical view. They are not additional verified clinical knowledge. Fewer units or more extracted characters are not used as accuracy measures.

Route inventories include 2,474 unclassified visual signals, 179 table-review routes and 158 figure-review routes. Of the 298 native-layout routes, 139 reconstruct and 159 remain held: 51 for overlapping native text regions, 96 without an unambiguous whitespace cut, and 12 for page rotation. The original corpus contains no image-only OCR route; an actual generated image-only PDF separately tests that outcome.

Current runtime artifacts are in `C:/Users/Admin/AppData/Local/Temp/gastroagi-p1c2-hmu_8g5z/`. The corpus audit took 198.63 s. Default extraction/map/planning took 111.74 s total; additional recovery/map/planning took 42.41 s. New compressed review maps total 46,785,289 bytes (46.79 MB), compared with the earlier P1-C1 total of 25.22 MB. This includes original and alternate evidence, new line geometry, routes and plans; it is not a same-model serializer comparison. Memory and storage increased to retain integrity.

The largest new artifact is **19,390,723 bytes**. Its canonical view grows from 69,432 to 272,858 characters; expanded components include 34.92 MB of evidence, 9.95 MB of plan metadata and 0.64 MB of source sections. This observed amplification requires resource-budget review before automatic activation. No evidence was discarded to reduce it. The existing bounded storage writer/reader remains in force.

A repeated recovery/map/plan of the reviewed real PDF measured 60,039,087 bytes of incremental traced peak allocation (60.04 MB), taking 10.10 s under profiling. The original extraction dictionary was already resident; native extraction allocations and total process RAM are excluded. This example does not bound the largest artifact's memory usage. These are local observations, not production memory/latency bounds. Inference cost, tokens and remote latency were not measured; there were zero paid inference/document-service calls.

Final code read/validated all 30 new maps, 30 legacy expanded maps, 30 legacy compressed maps and 30 P1-C1 maps. Historical artifacts were not rewritten.

## Exact implementation changes

| File | Functions/contracts changed |
|---|---|
| `src/native_layout.py` (new) | `inspect_pdf_routes`, `recover_native_layout`, `_routes`, `_ordered`, `_comparison`, `_fingerprint`, `validate_layout_views`. |
| `src/evidence.py` | `EvidenceLine`, `EvidenceRoute`, `NativeLayoutView`; additive `EvidenceBlock.lines` and document route/view lists; `_add_layout` retains line spans; `validate_evidence` checks line bounds and new view/route integrity. |
| `src/pdf_extractor.py` | `_native_page_layout` retains line geometry alongside unchanged block text; `PdfExtractionError.extraction` and `extract_pdf` preserve diagnostics on existing classified no-text failures. Default extraction signature, text and rejection behavior remain. |
| `src/draft_support.py` | `NativeLineSupport`, additive `NativeSupportProof` fields/version; `_line_support`; `prove` verifies recovered line support and rejects local glyph corruption; `warning_independent` checks explicit original-view support spans. |
| Tests | `test_native_layout_recovery.py` (16 cases), `test_native_layout_real_source.py` (3 cases), hashed anchor metadata. |
| Audit tool | `tools/check_native_layout.py`: before/after plans, immutable evidence checks, source baseline comparison, complete coverage rejection, new artifact persistence and protected hashes. |
| Documentation | The pre-implementation design and this review. |

`source_map.py`, `concept_planner.py`, `adaptive_ingestion.py`, generator, validator, pipeline, storage codec, prompts, provider configuration/implementation and dependencies are unchanged. Copies of the five inspected original boundary modules were retained in the local audit before editing.

Installed/tested: pypdf 6.9.2, PyMuPDF 1.27.2.2, Pydantic 2.13.5 and pytest 9.1.1. Native dictionary/line APIs were verified locally and against the official [PyMuPDF dictionary API](https://pymupdf.readthedocs.io/en/latest/textpage.html) and [extraction structure documentation](https://pymupdf.readthedocs.io/en/latest/app1.html); typed models use the existing [Pydantic model API](https://docs.pydantic.dev/latest/concepts/models/). No dependency or external deployment change was needed.

## Test commands, failures first, and actual results

The initial ten tests failed before the recovery module existed. Additional failing regressions exposed narrow-gutter rejection, loss of cross-view warning context, unchecked comparison/route metadata, imprecise unavailable-route labels and later font-code corruption. Each was corrected and rechecked. Real anchors were added after original-page inspection; neither an unsafe clinical PASS nor a higher corpus count was manufactured.

```powershell
python -m pytest tests/test_native_layout_recovery.py tests/test_native_layout_real_source.py tests/test_draft_eligibility.py tests/test_concept_planning.py -q -p no:cacheprovider --tb=short
python -m pytest -q -p no:cacheprovider --tb=short
python -m tools.check_native_layout --baseline <original-before.json> --protected <protected.json> --directory <NEW-audit-directory>
```

Final focused run: **72 passed in 8.05 s**. Final full run: **452 passed, 2 skipped, 9 subtests passed in 26.55 s**. No tests were deselected. The five original NEJM fixtures remain unavailable; the same two fixture-dependent test methods skip and remain mandatory release requirements. They are not reported as five successful NEJM tests.

The reviewed positive extraction/eligibility anchor changes from 0/1 to 1/1. The nearby damaged operator stays blocked, the five unsafe layout/source-conflict cases stay held, later font-code corruption stays held, image-only text cannot reach a provider, and unsupported numerical output fails real validation. These software/source checks are not clinical sensitivity, specificity or false-admission rates across an unadjudicated corpus.

## Remaining blockers and next justified step

Native extraction is still unreliable for internally interleaved blocks, overlapping author/affiliation regions, ambiguous block structures, some rotated pages, glyph damage, table relationships and figure contents. Conservative population/context/role gates and document-level warnings remain major false-block risks. Two native extractors can share font-map errors; line agreement alone cannot prove clinical truth. The corpus and these anchors do not constitute a representative independently clinically reviewed held-out benchmark.

Before automatic use of alternate views, the next justified increment is a reviewable **view-selection and dependency policy** that preserves every already-supported legacy unit, admits only independently certified recovered units, and prevents duplicate ownership/coverage across views. Selecting the view with the most units is inadequate. Evaluate that policy against independently reviewed anchors from additional document families and held ambiguous regions. A later local table route should establish explicit cell/header/unit/footnote evidence before releasing dependent concepts.

Coherent generation-unit optimization and editorial length-policy evaluation remain separate work; durable attempts/resume, external OCR/vision, live Azure inference and production RAG remain deferred. Separate capability, data-handling/licensing, budget and authorization checks are required before any external activation.

**Stop after this first P1-C2 increment. No clinical or production deployment approval is inferred.**

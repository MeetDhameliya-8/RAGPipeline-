# TASK11 adaptive PDF reliability audit

Date: 2026-10-16. Scope: architecture and failure analysis only.

The smallest useful next milestone is to strengthen evidence identity, validation boundaries, and persisted generation-unit recovery around the existing Azure generator. A new multi-agent system or document service is not justified by the available evidence.

## 1. Evidence basis and limits

This audit reads the current working files, rather than treating earlier handoffs as implementation truth. Sources inspected include root `AGENTS.md`, the project engineering skill and its safety/testing references, both `PipelineGuide/GastroAGI_TASK11_*.md` documents, implementation modules, the prompt, provider configuration, requirements, existing architecture/engineering documents, and tests.

Executed locally:

- Fresh extraction, adaptive planning, and source mapping for all **30 PDFs / 350 pages** currently in `input_pdfs/`.
- Inspection of all 30 existing validation reports: **21 REVIEW_REQUIRED, 9 FAILED**. These are historical draft-generation results, not approval or current-code success measurements.
- Stage-by-stage number probes for the failed packages; focused comparison of existing candidate sentences with current contradiction matching.
- Poppler rendering and visual inspection of five representative pages: PIIS0016508512015430 p2, PIIS0016508512011869 p10, PIIS0016508511011073 p2, PIIS0016508517359668 p8, and PIIS0016508518345700 p1. This is representative layout inspection, not clinical adjudication of the corpus.
- Offline SDK signature inspection and a synthetic normalization probe, without a provider request.
- `python -m pytest -q`: **240 passed, 2 skipped, 9 subtests passed**, in 28.27 seconds. One pytest cache-write permission warning did not fail tests.

No live Azure requests, ingestion commands, production data writes, or implementation edits were performed for this audit. Temporary page renders were used only for inspection. Source articles and generated chunks are not reproduced here.

No NEJM PDFs exist under this checkout. The two skipped tests are the five-NEJM citation fixture test and ABC-02 page-four evidence test. Their code remains intact, but their actual-PDF regressions have **not** been demonstrated in this environment. No Git repository was available at the working root, so this audit cannot name a verified commit or produce a Git diff.

Existing output reports lack prompt/code/extractor/request fingerprints sufficient to reconstruct their exact historical requests. Accordingly, an old error is distinguished below from a defect reproduced with current code. Existing reports describe Azure generation; no saved raw Azure response or completion finish reason establishes a truncation diagnosis.

### Runtime and dependency evidence

| Component | Declared requirement | Installed in interpreter used for tests |
|---|---|---|
| Python | No project interpreter pin found | 3.13.7 |
| pypdf | >=4.0 | 6.9.2 |
| PyMuPDF | >=1.24 | 1.27.2.2 |
| openai | >=1.0 | 3.26.1 |
| Pydantic | >=2.0 | 2.13.5 |
| python-dotenv | >=1.0 | 1.2.3 |
| Playwright | >=1.45 | 1.62.0 |
| pytest | >=8.0 | 9.1.1 |

LangChain 1.4.4, FastAPI 0.143.0, and ChromaDB 1.5.9 are installed in the broader environment, but are not declared dependencies or the implemented ingestion architecture. Their presence is not authorization to introduce them. Minimum-only requirements leave extraction and SDK behavior insufficiently reproducible.

Sanitized local configuration selects Azure, deployment label `gpt-5.4`, API version `2024-12-01-preview`; a key is configured. The endpoint shape is the existing Azure OpenAI resource style. A deployment label does not establish its actual model/version, supported parameters, context/output limits, or structured-output availability.

## 2. Verified current architecture

| Boundary | Actual execution |
|---|---|
| PDF bytes/intake | CLI reads a file path. Local upload servers validate filename/PDF signature and save bytes. The richer localhost application has a SQLite processing queue. No canonical PDF-byte SHA-256 intake registry or verified license/version manifest controls this path. |
| Native extraction | `pdf_extractor.extract_pdf()` extracts every page with pypdf layout mode, falling back to plain mode on errors or excessive layout inflation. It also runs PyMuPDF over every page using sorted text, with a native-text fallback for inflation. |
| Page selection | `_select_best_page_texts()` chooses one candidate per page using `_page_text_quality_score()` and a five-point improvement rule. Scores emphasize encoding/spacing/text heuristics, not correct column, endpoint, or table relationships. Both libraries run eagerly; this is not yet conditional expensive processing. |
| Cleaning | `normalize_page_text()` removes inferred repeated margins/page numbers, joins wrapped lines and some hyphenation, and detects table-like lines. `clean_article_text()` removes spaced-letter artifacts, strips inline URLs/noise and boilerplate, and truncates at a matching References/Bibliography heading. No reversible raw-to-clean span mapping accompanies these edits. |
| Citation extraction | `extract_document_reference_metadata()` searches the first two front-matter pages before a detected reference heading. `_select_reference_metadata()` compares extractor candidates and warns on conflicts. This is heuristic candidate identification, despite the downstream label “verified.” |
| Extraction gate | `validate_extracted_text_for_generation()` rejects no pages/no text/empty cleaned text. Additional sparse-visual and encoding blockers enter the adaptive plan and stop `generate_chunks()` before provider calls. Other warnings do not form a comprehensive extraction-quality gate. |
| Source map | `build_source_map()` uses cleaned page paragraphs, heading heuristics, cumulative section offsets and page ranges. Tables/figures and oversized sections can be marked deferred. The orchestrator loads section checkpoint state and constructs coverage reports. |
| Generation plan | `adaptive_ingestion.build_ingestion_plan()` independently partitions `full_text` at paragraph/sentence boundaries. Default budget: **20,000 source characters**, at most **80 batches**. Oversized indivisible/table-like units block. Routing hints are content-derived. |
| Request scope | A document fitting one batch sends its entire cleaned text. Longer documents send each bounded batch sequentially. **Current code does not send every long PDF in one request.** Batch ranges are not selected from `SourceDocument`, section checkpoint state, or an explicit clinical-concept plan. |
| Azure request | `_generate_single_batch()` calls `build_generation_input()`, adds article metadata, loads the common prompt and retry feedback, then invokes the selected provider. `AzureChunkGenerator.generate()` submits system/user messages through `AzureOpenAI.chat.completions.create(model=deployment, messages=...)`. Clinical generation supplies neither `response_format` nor an explicit output-token cap. |
| Transport | Azure-only registry; 120-second configured request timeout; two application retries after the initial attempt for classified transient failures. SDK retries are disabled. Auth/quota/invalid-request/deployment failures fail closed; Retry-After/backoff is bounded. Connectivity alone has a small token cap. |
| Model response | Adapter returns only content text; logs usage when available. It does not return finish reason, refusal, response identity, or usage as a persisted generation envelope. |
| Parsing/normalization | JSON parsing accepts fenced/outer-object JSON. Local repair only extracts an already parseable object; it does not complete truncated clinical content. `normalize_generation_result()` precedes schema checks and stringifies content values, defaults fields, and recomputes chunk count. |
| Reference binding | `bind_verified_references()` fills missing/blank references from extracted article identity. `_validate_generated_reference_against_source()` checks DOI presence or citation traceability. It does not prove article ownership of all citation components. |
| Merge | All batch results remain in memory until completion. Exact duplicates across all five fields are removed, chunk numbers reassigned, and article metadata initially comes from the first batch. |
| Clinical validation | `validate_chunks()` applies strict technical models, length/classification/reference/duplicate checks, global numeric-presence checks, lexical claim/recommendation/contradiction heuristics, and post-hoc evidence matching against source/pages/sections. It does not verify every claim's endpoint/arm/time/unit relationship. |
| Persistence | `save_outputs()` writes the complete draft, Markdown review package, validation report, PASS subset when available, local review subset, quarantine subset, source map, section state, and coverage. REVIEW_REQUIRED chunks occur in both local-review and quarantine subsets by design. |
| Job status | Extraction, generation, validation and output-writing failures are distinct return stages. Clinical rejection can return `partial_success` if other chunks are reviewable; full non-failed output returns `processed`. CLI counts only failed/processed/skipped/dry_run, so partial success is omitted from its summary and need not produce a failing exit code. |
| Local UI/storage | `process_uploaded_pdf()` uses no-move generation and separate local storage/read-back checks. Queue progress persists in SQLite; stale jobs can be queued again. PASS/REVIEW_REQUIRED drafts can be searchable in the local review assistant. “Validated” counters can include review-required drafts, so they are not approval counts. |
| Production boundary | Review metadata defaults to approval required/approved false/ingestion disallowed. Production integration reports blocked/unverified backend. The Admin helper fills and verifies five form fields, then stops before Upload; it revalidates without original source evidence and has no durable approval workflow. |

The final content contract remains **Topic, Subtopic, Section, Reference, Original Text**. Lowercase JSON keys encode these five fields; `chunk_number` is operational metadata, not a sixth content field. Future evidence records must remain outside the Admin payload.

## 3. Format-dependent failure diagnoses

### Existing failed packages and current evidence trace

IDs below are exact PDF stems with the common `PIIS00165085` prefix omitted. Each historical failure is recorded under `output/<lowercase-full-stem>/validation_report.json`.

| Suffix | Existing failed stage | Fresh local evidence / diagnosis |
|---|---|---|
| 12006762 | Clinical numeric validation, chunks 1 and 4 | Previously rejected 60, 0.44, 0.49, 0.55, 0.67 now appear in selected raw pages, cleaned pages and full cleaned text. Relevant pages: 1–2. Current extraction improved availability; the historical extraction/request cannot be reconstructed. No basis to call all old values hallucinations. |
| 12013169 | Clinical numeric validation, chunks 4–5 | Previously rejected 2.7, 3.8, 7.8, 9.0, 9.5 now survive selected raw/page-clean/full-clean stages; 9.0 and 9.5 occur on p4. Number presence still does not verify clinical association. |
| 12015430 | Clinical numeric validation, chunk 2 | p2 retains `50,000` in raw, page-clean and full-clean text. Current comma normalization correctly recognizes 50000. Historical failure is not reproduced by current numeric matching. The page render also shows mathematical operators/units that text extraction degrades: retaining the value alone is insufficient. Current plan additionally blocks sparse visual p6. |
| 13006999 | Clinical contradiction validation, chunk 4 | Current matcher treats unrecognized negative wording as a positive signal and matches loosely related negative endpoints. Column-interleaved source sentences worsen the match. This is a reproducible validator false-positive mechanism, not proof the whole candidate is correct. Current plan blocks sparse visual p10. |
| 14006568 | Clinical numeric validation, chunk 2 | p2/3/7 contain the target 75 with an adjacent `w` encoding artifact. It survives all text stages, but `NUMBER_RE`'s Unicode word boundary misses the full number beside a letter. Failure occurs at numeric tokenization, not content removal. Preserve unresolved approximation/operator semantics for review. |
| 17301312 | Clinical contradiction validation, chunk 4 | Current matcher links a primary-result claim with a source passage saying other outcomes did not differ. Endpoint identity is absent from the rule; multi-column sentence interleaving contributes. Do not automatically relabel the draft clinically valid. |
| 17359668 | Clinical numeric validation, chunk 6 | p8 contains 0.20/0.60/0.35 preceded by the extracted glyph U+00FE. Values survive raw/page-clean/full-clean text, but boundary-based number matching misses them. Render confirms a sign-bearing source presentation; sign, percentage, dose-unit and arm relationships require evidence-aware handling. Current plan also blocks sparse visual p13. |
| 17360663 | Clinical contradiction validation, chunk 4 | A candidate distinguishes primary from secondary findings; current matcher still pairs it with a negative primary-outcome passage. Endpoint and negation scope are not represented. Current plan blocks sparse visual p14. |
| 18345700 | Numeric validation chunk 1; contradiction validation chunks 4 and 6 | p2 retains `100,000.1`: a superscript citation marker becomes part of a decimal token, so comma normalization does not recover the intended 100000 match. On p1, the visual abstract separates primary and secondary endpoints, but flat extraction and sentence matching associate negative secondary-result wording with positive claims. The render confirms distinct visual groups; this is not grounds to accept all generated claims. |

These nine packages failed **after generation**, not from saved evidence of empty Azure output. All contain candidate chunks. The current code also has genuine generation-stage blockers that differ from those historical statuses.

### Successful-versus-failed comparison

“Successful” here means a historical review package without ERRORs. All 21 still require review.

- PIIS0016508511011073 is a two-column, historically REVIEW_REQUIRED comparator. Current extraction chooses PyMuPDF on five of six pages and plans two batches. Its layout is not fundamentally simpler than every failed article. Format alone does not predict rejection.
- PIIS0016508512006737 remains a useful no-error comparator, but current citation extraction merges a following page number into its DOI candidate. A non-failed report therefore cannot certify identity quality.
- PIIS0016508512011869 was REVIEW_REQUIRED, but fresh extraction yields an adaptive blocker on p10. The rendered supplementary flow diagram contains substantive branch content while native text contains its caption/header/footer only. **Content disappears at native extraction**, before cleaning, batching or Azure. The block is justified evidence-gap handling, not failed JSON generation.
- Across all 30 PDFs, current extraction returns text, but **9 documents have sparse-visual blockers**. Their affected pages are: 12011869 p10; 12015430 p6; 13006999 p10; 13014078 p10; 14000766 p10; 14012086 p11/p13; 15001997 p11/p13; 17359668 p13; 17360663 p14. Generation would stop before a provider call. The other 21 passing this route are not proven clinically complete.
- Planned inputs span one to four batches. This measures coverage of the cleaned string, not coverage of PDF evidence or clinical concepts.

### Root-cause classification across requested categories

| Category | Finding and confidence |
|---|---|
| Layout/reading order | **Observed.** Retained pypdf two-column lines can combine independent clauses. Scalar quality selection does not test semantic reading order. pypdf also emits rotated-text warnings that are not incorporated as structured page warnings. |
| Tables/figures | **Observed evidence loss and structural gap.** Supplementary flow diagram content is missing from native extraction. Text-rich table/figure pages can evade sparse-page detection; no row/header/footnote or figure-edge representation exists. |
| Scanned/image-only | **Branch exists, representative missing evidence observed; no complete scanned-document benchmark.** Empty whole-document extraction fails. Sparse visual pages can block. Empty pages without a detected visual flag only warn and may be omitted from the joined text. No OCR runs. |
| Citation extraction | **Observed.** Broad digit-whitespace normalization can concatenate DOI suffix and page number. First-two-page scope reduces late-reference contamination but cannot establish source identity. Front-matter cited DOIs/conflicts remain risky. |
| Missing clinical sections | **Not measurable as complete recall today.** No adjudicated section inventory exists. Source mapping uses a different text basis from generation; blanket References truncation and boilerplate removal can omit later relevant material. These are risks to test, not a claim that every removed paragraph is clinical. |
| Generation-unit selection | **Observed architectural mismatch.** Independent paragraph/sentence budgets ignore source-section deferred states and lack cross-boundary table/population/footnote dependencies. Document-type hints do not create concept coverage plans. |
| Azure output limits/malformed output | **Unresolved historical cause; concrete instrumentation gap.** No native output schema, clinical output cap, persisted finish reason/refusal or token budget. No retained evidence proves any of these 30 failures were output truncation. |
| Schema | **Reproduced locally.** Normalization before validation converts null/numeric/list/object content values into strings; recomputes a mismatching declared count; discards extra top-level fields. Synthetic invalid content passes the generation schema/content checkpoints after normalization. Later word/clinical checks may reject it, but strict original-type validation has already been bypassed. Non-object article values or invalid chunk-number conversions can raise outside `ChunkGenerationError`. |
| Numerical/clinical grounding | **Observed.** Unicode boundary artifacts, superscript citation concatenation, weak endpoint/negation matching, and global number sets cause false rejects and leave false-accept opportunities. A source number's existence is not evidence of the correct arm, unit or endpoint. |
| Recovery/checkpoints | **Observed.** Section state loads but does not select generation units; completed batch results are not persisted. A later batch failure loses earlier returned work. Retrying regenerates the package. |

### Additional reliability defects

1. **Two coordinate systems masquerade as one.** `SourceDocument` is built from page text; generation uses article-cleaned `full_text`. In the 1011073 comparator, only one of six section offset ranges slices the generation string back to that section's text. Offsets need an explicit text-buffer identity and mapping; they are not PDF-byte positions.
2. **Coverage overstates completion.** The previous defect that rejected chunks could complete sections is fixed: only eligible PASS chunks contribute. However, a single matching snippet can still mark a whole section processed; deferred sections remain deferred, and coverage does not block package completion.
3. **Citation self-validation.** Reference checking includes extracted metadata itself in the evidence blob. If a generated reference has a DOI found there, the checker returns without verifying every other citation component. Wrong owner or extra fabricated year/journal details are not adequately excluded by presence alone.
4. **Diagnostics are not actual request accounting.** `characters_sent_to_model` measures a full-document `build_generation_input()` result, even for multi-batch requests and before requests happen. Batch instructions and repeated prompt/metadata overhead are absent from that metric. Failure reports default candidate counts to zero even after earlier batch results.
5. **Retry safety is incomplete.** `prepare_output_dir()` removes an existing failed package before saving the replacement. `save_failure_report(force=False)` can overwrite its report while old chunks remain beside it; `force=True` removes the directory. There is no immutable attempt manifest or atomic complete-package promotion.
6. **Cache compatibility is incomplete.** Reuse checks match source content but not prompt, provider/model capability, extractor/cleaner or validator versions. Checkpoint validity includes source-map configuration, but not the full generation configuration.

## 4. As-built versus TASK11 target

The flowcharts and stepwise plan explicitly describe a target. The following status is verified against current code.

| TASK11 stage | Current status | Smallest target alignment |
|---|---|---|
| A.1 registration/checksum/version/license | Partial | Introduce byte identity and an intake manifest separately from cleaned-text hashes; record unknown metadata explicitly. |
| A.2 page-aware extraction | Partial | Preserve native candidates, block coordinates, reading order and transformations; bind tables/figures instead of flattening all evidence. |
| A.3 extraction reliability gate | Partial | Make per-page evidence status explicit, including empty/mixed pages and dense visual regions; propagate blocking reasons before generation. |
| A.4 semantic segmentation/coverage | Partial | Unify source-map and generation coordinates; select units with explicit dependencies and planned/excluded/deferred concepts. |
| A.5 Azure generation | Implemented, partial safeguards | Keep Azure-only transport; add validated capabilities, request/output budgets, response outcome metadata, and optional native schema. |
| A.6 technical gate | Implemented with bypasses | Validate raw types/shape/count before permitted casing/default/binding operations. |
| A.7 clinical source grounding | Partial | Scope evidence to claims/endpoints/arms/units/time; keep unresolved evidence under review. |
| Duplicate/coverage gate | Partial | Existing exact/near-duplicate checks; add semantic coverage reconciliation as an actual gate, not just a report. |
| A.8 review package/human approval | Package implemented; approval lifecycle missing | Keep mandatory review; add evidence-linked adjudication/version identity before any production implementation. |
| A.9 approved staging/stable IDs | Local draft IDs only | Local SHA-based IDs are not an approved staging/version contract. Discover backend separately. |
| A.10–A.13 authenticated ingestion/embeddings/candidate index/retrieval activation | Missing in production | Remain deferred. No production ChromaDB, approved-only activation or rollback service exists here. |
| Pipeline B clinical retrieval | Local prototype only | SQLite/hash embeddings, ranking/filtering, Azure/extractive answer paths and answer checks exist for drafts; not the approved-active ChromaDB/API target. |
| Pipeline C config/transient retries | Implemented | Add response-outcome classification and request budgets without broadening retryable clinical failures. |
| Pipeline C extraction/schema/claim quarantine | Partial | Existing blockers/reports/subsets; add exact-stage evidence and safe per-unit recovery. |
| Pipeline C duplicates/version rollback | Partial/missing | Local hash reuse and draft upserts exist; production candidate rollback remains unimplemented. |
| Pipeline D roadmap | Milestones 1–5 partly realized; 6–8 absent in production | Finish reliability/verification boundaries before backend expansion. |

Documentation drift matters: `docs/architecture.md` and several earlier documents still say long-document bounded generation is future work. It exists in `generate_chunks()` now, though resumable section generation does not. `docs/engineering/adaptive-ingestion.md` reports 857 pages; the current 30-PDF inventory has **350**. Historical inventories and installed-package descriptions must be dated and not reused as current facts.

## 5. Recommended adaptive evidence design

Use one deterministic controller and the current provider seam. Introduce internal contracts incrementally, rather than changing Admin content fields or introducing a database/API schema in this milestone.

### Proposed internal representation

| Record | Minimum responsibility |
|---|---|
| EvidenceDocument | PDF byte hash, extraction-run ID, schema version, source identity candidates and supporting spans, extractor/cleaner versions, immutable text-buffer identities. |
| EvidencePage | One-based PDF page, dimensions/rotation, native candidate methods, selected candidate/reason, quality signals, warnings with severity and region, extraction/review route. |
| EvidenceBlock | Stable within-source block identity, page/bounding box, paragraph/heading/table/figure/caption/footnote role, reading-order links, raw text and canonical text with separate offsets. |
| EvidenceTransform | Raw-buffer span to canonical-buffer span mapping; operation and reason for deletions/joins/normalization; preserve original glyphs and ambiguous alternatives. |
| TableEvidence | Table ID, cells with row/column/header associations, units, caption/footnotes, continuation links and per-cell source spans. Unknown relationships remain unresolved. |
| FigureEvidence | Figure/region ID, caption, surrounding references, evidence-availability status; any later interpretation is a separate approved derived record with source regions and uncertainty. |
| GenerationUnit | Content/config hash, section/concept and evidence IDs, exact owned spans, context-only dependencies, input/output budget, expected coverage and explicit exclusions. |
| UnitAttempt | Attempt ID, request/prompt/model configuration fingerprint, outcome/finish reason, usage, candidate counts, validation result and protected artifact references. No source text in ordinary logs. |

Pydantic models should forbid unknown contract fields. Offsets must name the buffer they address; stable IDs must be scoped to source/version, not filename alone. This is a proposed internal design, not an invented production schema.

### Conditional page/region routing

1. Run a configured fast native extractor and retain coordinates/candidate identity. During initial calibration, continue dual extraction to measure any quality/cost change before switching to a conditional default.
2. Detect missing/garbled text, column discontinuities, rotated content, tables, figure references, low text coverage and extractor disagreement. Scores guide routing; they do not certify clinical correctness.
3. Invoke alternate native/layout extraction for affected pages/regions. Evaluate column ordering and table geometry against protected fixtures. PyMuPDF already offers block/word geometry and table APIs; API availability is not proof of extraction accuracy. [Official PyMuPDF extraction guidance](https://pymupdf.readthedocs.io/en/latest/recipes-text.html).
4. If critical visual evidence remains unresolved, route to explicit review. Use OCR or visual interpretation only through an intentionally configured and approved adapter. No Azure Document Intelligence or other service is automatically added. A local OCR option would still require dependency/language/runtime verification and measurement.
5. Treat covers/decorative pages differently only when an explicit, reviewable exclusion rule records the reason. Do not suppress all sparse-page blockers merely to increase completion rates.
6. Plan generation from accepted evidence and named dependencies. Critical unresolved evidence blocks affected concepts and keeps the document incomplete. Unrelated sound units may be saved as drafts, with that incomplete status clearly preserved.

Cleaning should remove proven layout artifacts while recording transformations. Never convert a damaged sign or dose unit from medical expectation. A numeric parser should retain lexical form, value, sign/operator, unit, citation-marker status and evidence span; it must not merely search a larger global number set.

## 6. Generation, structured outputs and targeted recovery

### Separate planning from prose generation

Start with deterministic sections/blocks and a small source-anchored concept manifest: concept ID, supporting evidence, dependencies, required qualifiers, exclusion reason and lifecycle state. Let an optional bounded Azure planning request suggest concepts only if deterministic planning proves inadequate on a benchmark. It must not invent missing concepts or clinical facts.

Generate final chunks for accepted concepts/units with enough neighboring context to preserve subjects, population, endpoint and table-footnote relationships. Mark context-only spans so overlap does not create duplicate coverage. Keep the usual 4–8 chunk prompt guidance from becoming a per-batch quota. Reconcile planned versus generated/validated/reviewed/excluded concepts after generation; do not force every short concept to 150 words.

### Exact installed SDK versus deployment capability

Offline inspection of `openai==3.26.1` confirms that the instantiated `AzureOpenAI` client exposes `chat.completions.parse`; both `create` and `parse` accept `response_format`, `max_completion_tokens` and `max_tokens`. The existing provider does not use native structured output.

Azure documentation places structured-output API support at `2024-08-01-preview` onward, including later supported APIs. This makes the configured API version a plausible route, **not proof of deployment support**. The retrieved model list does not establish support for this resource's `gpt-5.4` deployment label. The current dated endpoint can remain until separately verified; switching to `/openai/v1` is not a prerequisite inferred by this audit. [Azure structured-output requirements](https://learn.microsoft.com/en-us/azure/foundry/openai/how-to/structured-outputs).

Recommendation: define a capability profile and test the exact existing deployment/API route only in an approved live stage. If verified, supply a strict transport schema for the existing article envelope and five content fields, with internal numbering assigned locally or represented appropriately. Handle required/nullable fields and forbidden extra properties explicitly. Keep strict local schema, reference and clinical checks afterward. JSON mode alone does not enforce field shape. Refusal and incomplete completions need separate handling. [Official OpenAI structured-output guidance](https://developers.openai.com/api/docs/guides/structured-outputs).

Before generation, account for system prompt, metadata, unit text, context dependencies and reserved output capacity using verified deployment limits. An unverified character-to-token guess must not authorize a request beyond those limits. Persist actual request count, input/output usage and outcome; reject length/content-filter/refusal outcomes explicitly. Do not “repair” incomplete clinical JSON by guessing content.

### Recovery that preserves valid work

Current answer: a reviewable subset can survive **clinical validation failure**, but a source section or failed batch cannot be independently regenerated through the normal path. Queue recovery restarts jobs, not completed generation units.

Minimal target:

1. Persist each generated unit's candidate artifacts and validation outcome atomically before starting the next unit. Distinguish generated, structurally valid, clinically PASS, review required, rejected and approved.
2. On retry, verify PDF/evidence/prompt/config/model/validator identities and reuse only compatible completed work. Preserve immutable previous attempts; a changed validator can revalidate saved candidates without a new model call.
3. Retry transient transport failures with existing bounds. Malformed/truncated output gets a bounded unit-level regeneration only under an explicit policy/budget. Missing extraction evidence goes back to extraction/review, not repeated prompting.
4. Regenerate only failed units/concepts, with prior issue feedback and source evidence. Previously accepted clinical content stays unchanged unless its evidence/dependency version changed.
5. Re-run document-wide reference, duplicate and coverage reconciliation before promoting a complete review package. Partial work stays incomplete and cannot acquire production eligibility.
6. Keep human medical approval mandatory; software PASS never implies approval.

An independent clinical verification model is **not required now**. It cannot recover omitted evidence and may share the generator's errors. Evaluate it later as a bounded evidence-citing checker that cannot override deterministic failures. Use a clinician-adjudicated held-out set to compare generator plus deterministic checks versus that same pipeline plus verifier: unsupported-claim sensitivity, false alarms, reviewer time, cost and latency. Suggested adoption gate: at least 20% relative reduction in missed clinically material errors, no new unsafe approvals, and no more than a five-percentage-point increase in false alarms. These are proposed decision thresholds, not measured benefits or proof of independence.

## 7. Exact change surfaces and prioritized minimal plan

No implementation changes are made by this audit. Each row is a small separately reviewable increment with an offline acceptance gate.

| Priority | Existing functions/models likely to change | Deliverable and exit criterion |
|---|---|---|
| P0 | `chunk_generator.normalize_generation_result`, `validate_generation_schema`, `_generate_single_batch`; `Generated*Model` | Validate original types, count and top-level shape before normalization; allow only documented key casing/reference binding operations. Invalid null/list/object fields cannot turn into accepted strings; all malformed shapes produce typed generation failures. |
| P0 | `pdf_extractor._normalize_for_citation_matching`, `extract_document_reference_metadata`, `_select_reference_metadata`; `chunk_generator.bind_verified_references`, `_validate_generated_reference_against_source` | Retain citation candidate spans/ownership/conflicts; stop DOI/page concatenation and validate components independently. Missing identity stays unresolved. Existing NEJM binding/conflict behavior remains protected. |
| P0 | `chunk_validator._validate_numbers_against_source`, `_validate_source_grounding`, `_contradicts_source`, `_source_support_sentences`, `_build_source_evidence` | Add explicit numeric/sign/unit/citation context and endpoint/negation matching. Cover the observed encoding/footnote cases without silently interpreting damaged operators. Distinguish evidence unavailable from genuinely contradicted. |
| P0 | `pipeline.build_generation_diagnostics`, `build_pipeline_failure_report`, `main`; `local_rag_chat.build_chunk_count_summary`, `_terminal_queue_status_for_result` | Truthful per-stage candidate/request counts and explicit partial/incomplete job accounting. REVIEW_REQUIRED is never labeled approved. Partial CLI jobs appear in totals and have a documented non-success completion policy. |
| P1 | `pdf_extractor.extract_pdf`, `_extract_page_text`, `_extract_pymupdf_pages`, `_select_best_page_texts`, `normalize_page_text`, `clean_article_text`, `validate_extracted_text_for_generation`; `adaptive_ingestion.assess_page_text` | Add internal page/block evidence and transformation mapping, then conditional extraction routes behind configuration. Sparse/empty/dense-visual gaps are surfaced. No new OCR/service activation. |
| P1 | `source_map.build_source_map`, `_paragraph_records`, `_sections_from_paragraphs`, `SourceSpan`, `SourceSection`; `adaptive_ingestion.build_ingestion_plan`, `_semantic_spans`, `GenerationBatch`; `chunk_generator.generate_chunks`, `build_generation_input` | Unify buffers and source units, dependency context and concept coverage. Every owned span is accounted for, and tables/footnotes are not separated without an unresolved-evidence state. |
| P1 | `AzureChunkGenerator.generate`, `_create_chat_completion`, `_extract_chat_message_content`; `providers.base.ChunkGeneratorProvider` | Introduce response metadata and capability/budget configuration; mocked refusal/truncation/limit tests pass. Native schema remains opt-in until exact-route approval and verification. Preserve Azure-only selection. |
| P1 | `pipeline.process_pdf`, `save_outputs`, `save_failure_report`, `prepare_output_dir`, `build_retry_feedback`, `reusable_output_matches_source`; `source_map.ProcessingState`, `load_or_initialize_processing_state`, `save_processing_state`, `mark_processed_sections_from_validation`, `build_coverage_report`; `local_rag_chat.retry_queue_job`, `recover_stale_queue_jobs` | Atomic per-unit checkpoints and immutable attempts; retry unit 2 after failure does not recall provider for completed unit 1. Hash mismatch invalidates only defined affected work. Whole-package promotion is atomic. |
| P2 | Generation prompt and review-package rendering; optional small planner/verifier seam | Only after preceding benchmarks, evaluate concept planning/checker benefits with fixed evidence and clinician adjudication. No orchestration framework or autonomous agents needed. |

Begin with P0 defects and a protected corpus manifest; implement P1 one boundary at a time. Update architecture documents and flowchart status annotations alongside each approved change. Record or constrain tested dependency versions before comparing extraction results. Production/backend discovery remains a separate milestone.

## 8. Regression plan, measurable acceptance and remaining risks

| Test layer | Additions | Measurable acceptance criteria |
|---|---|---|
| Unit tests | Raw-schema bypasses, extra envelope fields, mismatching counts, invalid article/chunk-number shapes; DOI plus following page; source-owner conflicts; numeric glyphs/operators/footnote markers; alternate negation forms and primary/secondary endpoint separation; buffer-offset maps | All invalid-type/count fixtures reject before coercion; all protected citation conflicts remain blocked; 100% of span references round-trip to their named buffer; no guessed sign/unit restoration. |
| Mocked model tests | Valid JSON with `finish_reason=length`, refusal/content filtering, schema-invalid output, missing choices/content, request-budget exhaustion, cross-batch reference/classification conflicts, transient versus permanent failures | Every incomplete/refused response has a specific failure outcome; zero calls when evidence/capabilities block; finite request budgets; one source unit's failure does not erase completed unit artifacts. |
| Mocked recovery/persistence | Crash after unit save and before next save; failure after first batch; stale queue recovery; concurrent attempts; changed PDF/prompt/model/validator; failed report beside older chunks | Resume calls only unfinished compatible units; previous valid payload hashes remain unchanged; no duplicate active attempt/unit; no mixed-attempt complete package; counters distinguish candidate/reviewable/PASS/approved. |
| Actual PDF: five NEJM | Restore all five originals securely; parameterize each PDF so one missing fixture does not skip the remaining corpus; retain the ABC-02 compliance evidence assertions and reference tests | All five PDFs execute with **zero skips** in the protected release suite; expected citations and p4 evidence survive extraction/request construction. No live generation implied. |
| Actual PDF: multi-column | Current failed/control pairs with manual block order and endpoint anchors; rotated margins, full-width headings, late references/appendices | Every annotated critical anchor survives or has an explicit blocking reason; no cross-column concatenation in approved extraction for annotated regions; coverage reports cannot claim complete when anchors are unresolved. |
| Actual PDF: scanned/mixed | Image-only article, mixed native/scan pages, blank/decorative page, OCR text overlay | With OCR disabled, zero provider calls for unresolved critical pages; exact page/region reason retained. If OCR is later approved, measure against manually transcribed critical text/numbers rather than judging by text density. |
| Actual PDF: table-heavy | Borderless/ruled/continued tables, merged headers, footnotes, units, row order and negative signs; include p8 of 17359668 | All annotated critical cell-header-unit-footnote relationships are preserved or explicitly unresolved; no evidence-supported status for a deliberately swapped arm/unit. |
| Actual PDF: figure-heavy | Supplementary flow diagram in 12011869 p10; visual abstract in 18345700 p1; plots with textual captions but image-only values | Missing critical figure evidence blocks dependent concepts; caption extraction alone never certifies the figure contents. No figure-derived claim accepted without reviewed evidence. |
| Actual PDF: long guideline | Securely supplied long guideline with recommendation grades, appendices after references, cross-references and multi-page tables; synthetic large-document companion for budget edges | 100% of annotated material recommendations appear in the manifest as generated/validated/review required/excluded with reason; all unresolved critical recommendations keep document incomplete; bounded requests include prompt/output reserve; interruption resumes completed units. |
| Approved live Azure | Separate opt-in exact deployment/API/schema/cap test, then fixed clinical PDF benchmark with cost/timeout/request ceilings and clinician review | Zero unclassified length/refusal/malformed outcomes; 100% final five-field schema compliance; zero accepted seeded wrong-arm/dose/unit/direction claims; compare observed unsupported-claim rate, source coverage and reviewer time to baseline. Report denominators and uncertainty, not only completion rate. |

Keep routine tests independent of real keys and paid inference. Protected real PDFs and candidate artifacts remain outside source control; version a nonclinical fixture manifest/checksums and expected engineering assertions instead. Never use the generated chunk itself as its sole grounding oracle. Existing tests remain required; stricter tests may expose current behavior intentionally, but no acceptance criterion should be “make all existing failures pass.”

Remaining assumptions and risks:

- The available corpus consists of these 30 journal PDFs. No full scanned-document or long-guideline fixture was verified; those branches require supplied/licensed fixtures and adjudicated expectations.
- Historical request text, model finish reasons and prompt/extractor identities are incomplete. Current extraction improvements cannot establish historical hallucination or current live clinical quality.
- Candidate citation metadata can be wrong even when local validation passes. No external bibliographic repair was attempted; unknown source details must remain unknown.
- Layout extraction and OCR can invert signs, lose superscripts, corrupt micro-units and detach footnotes. More extracted text is not automatically safer evidence.
- A lexical false-positive mechanism does not prove the candidate clinically sound. Clinicians must adjudicate affected claims and relationships.
- Per-unit overlap improves context but increases duplication/cost; concept coverage and dependency tracking must be measured on held-out formats.
- Local review search intentionally uses draft content. It must remain clearly distinguished from a production approved-only knowledge version.
- The Admin helper's final human click is a workflow stop, not a version-bound signed approval mechanism. Backend ingestion, publication and rollback remain unverified and out of scope.
- Minimum-only dependencies and missing Git identity reduce reproducibility. Secure artifact retention, dependency version policy and a verified source revision are necessary for a later release benchmark.

This milestone ends with this audit and proposed plan. No production ingestion or large architecture implementation is authorized by it.

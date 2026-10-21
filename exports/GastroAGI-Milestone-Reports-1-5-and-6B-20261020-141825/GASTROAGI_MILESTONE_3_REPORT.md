# GastroAGI Milestone 3 — Source-Grounded Clinical Concept Planning

Date: 19 October 2026. Scope: Milestone 3 only.

Implemented an independent document concept inventory and source-review binding layer before final writing. It separates literal clinical cues, unresolved questions and explicitly reviewed source relationships. Every canonical source character is accounted for, and missing native passages, blank pages and unresolved evidence warnings remain visible as separate concerns. The existing draft-generation route, prompts, source-package ownership and clinical validation remain functional.

Milestones 1 and 2 were reviewed through their reports, current implementation and a fresh prerequisite run: **95 tests passed** across diagnostics, rolling scheduling and existing clinical/adaptive/source-package planning. The earlier reports end in PASS. No live provider, production queue or historical output was used as an experiment.

## Files and boundaries changed

| File | Functions and changes |
|---|---|
| `src/document_concepts.py` — new | `build_document_concept_plan`, `validate_document_concept_plan`, `evidence_fingerprint`, `_inventory`, `_questions`, `_paragraphs`, `_contains`, `_whole_passage`, `_check_review`, `_overlap`; typed internal models described below |
| `src/adaptive_ingestion.py` | Add optional `IngestionPlan.document_concept_plan`; `build_ingestion_plan` constructs the independent inventory before invoking the original planning body, now `_build_ingestion_plan`. Existing batches and source-package/clinical plans are preserved |
| `src/pipeline.py` | `build_generation_diagnostics` retains the internal, quote-free plan. `process_pdf` adds document-region accounting to validation/coverage reports, preserves the older literal-coverage result separately and prevents that result from asserting complete clinical document coverage |
| `tests/test_milestone3_document_concepts.py` — new | 23 test functions covering 45 synthetic cases, including actual generation/validation equivalence and temporary pipeline persistence |
| This report — new | Contracts, examples, verification, compatibility and limits |

Inspected and retained the implementations of `concept_planner.py`, `source_packages.py`, `source_map.py`, `evidence.py` and `draft_support.py`. No provider, scheduler, chunk validator, prompt, extractor, source-ownership checker, approval gate, database schema or dependency was changed. No new agent architecture, service endpoint, review UI or inference route was introduced.

## Planning method and semantic boundary

### Literal inventory

`SourceRegion` partitions the canonical source at paragraph separators, retaining the separators so that the character accounting has no gaps or overlaps. These are **inventory boundaries**, not assertions that paragraphs are meaningful clinical units. The planner does not split final knowledge into arbitrary sentences or force a unit to stop at a page boundary.

The inventory records candidate literal cues for study design, population, intervention, comparator, arm, primary/secondary/other outcome, numerical finding, time point, safety, recommendation, recommendation strength, evidence certainty, limitation, applicability and qualification. Possible source headings can supply a topic cue. Topic/subtopic and all other roles can also be assigned exact anchors through source review.

Every automatic cue is labeled `relationship=literal_cue_only`. Regex detection can suggest a question or identify a review hazard. It **does not establish** that a number belongs to an arm, a treatment applies to a population, an outcome was significant, or a recommendation has a particular strength/certainty. Default question candidates have no bound roles, relationship evidence or inferred context edges. Unknown paragraphs remain `not_yet_assessed`; they are not silently treated as nonclinical material.

Automatic question categories are study scope, primary outcome, secondary outcome, safety, recommendation, applicability and unclassified source statement. The reviewed contract additionally supports comparative outcome questions. Question identity combines the source-backed selection and review record; it is separate from the older generation-unit identity.

### Explicit source-supported question grouping

`ReviewedQuestion` groups complete source regions around a clinical question. It can combine nonadjacent regions and pages when the caller supplies:

- Exact source anchors for the question and assigned roles, including topic and one population scope.
- Explicit owned regions and separate supporting-context regions.
- Complete relationship-evidence passages for selected regions containing clinical cues; isolated keywords are insufficient.
- An opaque source-review ID and a fingerprint of the complete current evidence/section graph.
- Explicit attestations that population/domain/interventions, arm/outcome/time point and qualifications were checked through human source reading.

Outcome/safety questions require an intervention and time point. Comparative questions also require a comparator and outcome. Primary and secondary questions require their corresponding outcome role. Separate population role assignments and overlapping ownership between reviewed questions are rejected. Supporting context can be reused without being counted as another owned occurrence.

This is a **typed source-reading boundary**, not a semantic theorem prover or an authenticated signature service. Software checks source identity, exact spans, complete passages, scope fields and structural consistency. It cannot establish that a caller's human-review attestation is truthful. An annotation must come from actual source reading; neither cue matching nor a model response is permitted to manufacture that attestation. Test annotations are explicitly synthetic examples.

The normal pipeline supplies no review records and therefore does not automatically promote candidates into reviewed clinical relationships. The independent Python API accepts reviewed questions/exclusions; adding a review UI or automatically resolving semantics is outside this milestone. This preserves a useful draft route while making its unresolved document-level understanding explicit.

### Qualifications and uncertainty

Whole passages preserve population, treatment/comparator wording, outcomes, numbers, time points, negation and qualifications together. A neighboring paragraph with safety/qualification/limitation/applicability cues triggers a hold if omitted from a reviewed selection. Including it as complete context retains its source text without claiming that proximity proves a clinical relationship. Shared held context propagates to dependent questions, including questions processed earlier.

Table/figure references and notes, unresolved referents, uppercase abbreviations, damaged text, nonexact/unmapped source regions and unresolved extraction warnings remain held. A source-review record does not clear those extraction/structured-evidence holds. This milestone does not interpret table cells, figure panels or abbreviations, and does not resolve ambiguous clinical statements by guessing.

## Exact internal contracts and provenance

The new Pydantic contracts use `extra=forbid` and hide input values in formatted validation errors. They contain span references and controlled labels, not copied source passages or generated clinical text.

| Model | Fields |
|---|---|
| `LiteralCue` | `role`, `evidence`, `relationship` |
| `SourceRegion` | `region_id`, `basis`, `ownership`, `trace`, `literal_duplicate_native_spans`, `page_numbers`, `section_ids`, `section_relationship`, `cues`, `warning_ids`, `reasons`, `coverage`, `concept_ids` |
| `BoundRole` | `role`, `evidence` |
| `SourceReview` | `evidence_fingerprint`, opaque hexadecimal `review_id`, `basis=human_source_review` |
| `ReviewedQuestion` | `review`, `question`, `question_anchor`, `owned_region_ids`, `context_region_ids`, `roles`, `relationship_evidence`, `same_population_domain_and_interventions`, `arm_outcome_timepoint_checked`, `all_qualifications_included` |
| `ReviewedExclusion` | `review`, `region_id`, `reason`, `source_basis` |
| `ConceptQuestion` | `concept_id`, `question`, `question_anchor`, `owned_region_ids`, `context_region_ids`, `roles`, `relationship_evidence`, `relationship_basis`, `review_id`, `status`, `reasons` |
| `DocumentConceptPlan` | `version`, `plan_id`, `evidence_document_id`, `evidence_config_hash`, `canonical_buffer_id`, `evidence_fingerprint`, `source_warning_ids`, `regions`, `concepts`, `reviews`, `exclusions`, `coverage_counts`, `canonical_character_counts`, `planning_accounted`, `document_complete`, `clinically_approved` |

`version=document-concepts-v1`. `document_complete` and `clinically_approved` are constrained to false. `planning_accounted` describes disposition of the inventory, not generated coverage or clinical approval.

Each canonical region retains its exact named-buffer span and existing raw evidence trace, including page numbers, precision and unmapped/synthetic fragments. Section relationships remain explicitly `raw_overlap_candidates`. Existing section IDs contain the source filename stem; this plan instead exports `section:` plus SHA-256 of the original ID. That reproducible identity resolves through the original source map without exposing the filename in new diagnostic records.

The fingerprint includes every current evidence buffer, mapping, page, warning, configuration and section record, plus source-map configuration/content hashes, source-map buffer identity and page count. A source mismatch, corrupted buffer/mapping, modified PDF identity or stale review is rejected. Validation reconstructs the plan from the **current** source and review/exclusion records and compares the complete result; altered counters, spans or promotions are rejected. There is no object-ID, filename-only or stale proof cache.

Selected native text omitted from the canonical source is retained as held evidence. Separate native blocks receive a duplicate-representation credit only where a unique exact character match on the same page and exact existing source mappings establish textual identity; the duplicate native spans are retained. Ambiguous/repeated matches remain held. This credit is about text representation, not clinical relationships or cross-page semantics. Existing pypdf/PyMuPDF corroboration and source validation are unchanged.

`source_warning_ids` retains references to all evidence warnings, including nonblocking notices. Blocking warnings also appear as separate concerns with their source trace/page scope where available. Concerns without a canonical passage contribute zero canonical characters. Canonical counts and concern counts therefore must not be summed as if they were one measure of PDF length.

None of these fields is added to Topic, Subtopic, Section, Reference or Original Text. `chunk_number` remains operational metadata only.

## Coverage accounting and completion

Every inventoried region has exactly one coverage status:

| Status | Meaning |
|---|---|
| `represented_in_concept` | Owned by an explicitly source-bound reviewed question; not proof of generated coverage |
| `supporting_context_only` | Recognized structural heading or reviewed supporting context; not independent generated clinical work |
| `intentionally_excluded` | Verified separator-only region, or explicit source-reviewed nonclinical/reference exclusion with an exact basis and reason |
| `held_for_source_review` | Known extraction, mapping, relationship or dependent-context concern |
| `not_yet_assessed` | Retained source whose clinical relationships have not been assessed |

Reviewed exclusions allow `reference_list`, `nonclinical_front_matter` and `nonclinical_boilerplate`. They require source identity, an exact basis and a review record. They cannot hide a held region or conflict with a reviewed question. The excluded region and justification remain visible; it no longer appears as an unresolved clinical question.

`coverage_counts` counts regions/concerns by these five statuses. `canonical_character_counts` counts their canonical ownership characters independently. The latter sums to the complete canonical buffer length. Extra native gaps, unlocalized warnings and empty pages remain visible concerns without invented canonical ownership. No chunk-count threshold enters this accounting.

New output locations are `diagnostics.document_concept_plan`, `report.document_concept_coverage` and `coverage_report.document_concept_coverage`. The compact coverage objects contain `plan_id`, the two counter dictionaries, `planning_accounted`, `complete` and `clinically_approved`. Milestone 1 also measures the new monotonic `document_concept_planning` stage inside the existing planning envelope.

The older clinical coverage object preserves its former result as `literal_source_coverage_complete`, adds `document_concept_plan_id`, and uses the document plan's false `complete` value for overall completion. Existing chunk validation status is unchanged. The existing partial-success path saves reviewable drafts and retains the source PDF. A successful literal match or one/four/eight returned chunks no longer represents complete clinical document understanding through that path. The source-review planning layer itself does not claim final writing/coverage completion even if all its regions are accounted for.

## Synthetic examples and measured before/after accounting

All examples below are fictional test data, not medical evidence or recommendations. No real source passage or generated medical output is reproduced here. The measurements are saved as content-free counters in `tmp/milestone3/examples.json`.

### Two arms, different outcomes and a non-significant result

Synthetic methods describe adults with **condition delta**, randomized treatment alpha/placebo, primary remission at 8 weeks and secondary symptom change at 4 weeks. A separate primary-result passage states **40% versus 38%, not statistically significant, p=0.70**. Another passage reports the secondary result, **10 versus 9 points**. These numbers exist solely to exercise source accounting.

Before review, the plan contains unresolved scope, primary and secondary questions. It assigns no number, arm comparison or significance interpretation automatically. A synthetic source-review record owns only the primary-result passage and supplies methods as context. The complete primary passage retains both arm rates, time point, p-value and negation. The secondary passage stays unassessed and visible as its own unresolved question.

| Status | Before: regions / characters | After primary-question binding: regions / characters |
|---|---:|---:|
| Represented | 0 / 0 | 1 / 191 |
| Supporting context | 2 / 18 | 3 / 206 |
| Excluded | 0 / 0 | 0 / 0 |
| Held | 0 / 0 | 0 / 0 |
| Not assessed | 3 / 527 | 1 / 148 |
| Total canonical source | **5 / 545** | **5 / 545** |

`planning_accounted=false` before and after; `document_complete=false` throughout. The plan does not convert the reported non-significant comparison into efficacy or superiority.

### Cross-page population definition

With methods on page 1 and the primary result on page 2, the source contains 395 canonical characters. Before review: two context headings (18 characters), two unassessed regions (377 characters), no represented concept. There is no automatic cross-page relationship.

After an explicit synthetic source-reading record binds the primary question: one represented region (189 characters), three supporting-context regions (206 characters), zero unassessed/held regions. Raw traces remain exact on their respective pages. `planning_accounted=true`, but `document_complete=false` and `clinically_approved=false`: planning is not final chunk coverage or medical approval.

### Conditional guidance and adjacent safety

A synthetic guideline says the panel suggests treatment alpha for adults with condition delta **only after shared discussion**, with a **conditional** recommendation, **low certainty** and exclusion of children. The reviewed example assigns separate literal anchors for recommendation, strength, certainty and applicability while retaining the whole sentence. The detector alone only proposes those roles; it never upgrades a suggestion to a strong recommendation.

An adjacent synthetic safety paragraph limits adverse-event ascertainment to the first two weeks and says delayed events were not measured. Omitting that paragraph holds the primary question with `adjacent_qualification_not_included`, even when a supplied review attestation claims completeness. Including the entire paragraph as source-reviewed context permits a source-bound question while preserving the limitation. Table/figure relationships and extraction uncertainty remain held even if a caller supplies a review record.

## Tests actually executed

Tests/fixtures and live-capable entrypoints were inspected before execution. The executed commands target `tests/` or named modules; the root live Azure diagnostic is excluded. Existing `tests/conftest.py` redirects runtime paths/defaults to temporary directories and rejects Azure SDK client construction unless a fake client is injected. Mocked connectivity-method tests in the existing suite use fake clients; no real connectivity check ran. Browser tests use a temporary localhost server with mocked chat responses.

All pytest commands used `python -B -m pytest`, `-q -p no:cacheprovider --tb=short`, a separate `--basetemp=tmp/milestone3/<run>` and `--junitxml=tmp/milestone3/<artifact>`.

| Selection / artifact | Actual result |
|---|---|
| Prerequisite: `test_milestone1_diagnostics.py`, `test_milestone2_scheduler.py`, `test_concept_planning.py`, `test_source_packages.py`, `test_adaptive_ingestion.py`; `prerequisite-results.xml` | **95 passed, 7.95 s** |
| Initial new module; `focused-results.xml` | **32 passed, 1.91 s** |
| Expanded new module plus the five prerequisite modules; `boundary-results.xml` | **2 failed, 132 passed, 8.99 s** |
| Corrected same selection; `boundary2-results.xml` | **134 passed, 8.62 s** |
| Full `tests/` before final privacy/ledger refinements; `offline-results.xml` | **615 passed, 7 skipped, 9 subtests passed, 44.60 s** |
| Final new module, including native duplicate/ambiguity, warning ledger and shared context checks; `final-focus-results.xml` | **43 passed, 1.88 s** |
| Full `tests/` after privacy/ledger refinements; `final-offline-results.xml` | **619 passed, 7 skipped, 9 subtests passed, 50.90 s** |
| Final full `tests/`, including source-map configuration/content identity and independent review-anchor checks; `identity-final-results.xml` | **621 passed, 7 skipped, 9 subtests passed, 43.83 s** |

The two initial failures identified a false missing-source concern for an identical alternate native block and mutable role aliasing between the output question and stored review record. The implementation now credits only unique exact same-page duplicate text, retains its spans, and independently copies role/evidence records. Both failures were fixed and retested; none was ignored or converted into a skip.

Final review added source-map configuration/content hashes to the review fingerprint and isolated question anchors from their stored review records. Two additional identity-mutation cases and the anchor-mutation assertion passed in the final full suite. Seven skips are the preexisting unavailable real-source fixtures: two multiview anchors, three native-layout regressions and two TASK11 source regressions. They are not passed clinical accuracy checks. The 621-test full-suite result covers the final implementation; no source changes followed that run.

### Acceptance evidence

| Requirement | Executed evidence |
|---|---|
| Two-arm randomized trial, primary versus secondary outcomes and non-significance | Whole primary source remains owned; population/intervention/comparator/time anchors are explicit; secondary question remains separate/unassessed; no inferred significance claim |
| Conditional recommendations, certainty and applicability | Literal and reviewed role anchors remain conditional/low-certainty/excluding children |
| Cross-page definitions | Default has no inferred dependency; reviewed exact page-1 context can support page-2 question |
| Adjacent safety qualifications | Omission holds the question; complete context retains the qualification |
| Abbreviations, references and unproven tables/figures | Parametrized holds; reviewed table example remains held |
| Missing/corrupt source | Native gaps/empty pages remain concerns; changed text, buffers, mappings, sections, warnings, PDF identity, review identity and plan metadata reject reuse |
| No unreviewed scope merges | Different populations/domains/interventions produce separate unresolved candidates; multiple population roles, missing comparator/scope, partial relationship passages and overlapping ownership are rejected |
| Excluded/held evidence remains visible | Source-reviewed reference exclusion retains its reason/span/trace; exclusion cannot clear a hold |
| Existing generation compatibility | Actual source-package generation, provider input messages, batches, output chunks, ownership and final validator reports match with/without the inventory |
| Completion separate from chunk count | One/four/eight mocked passing chunks preserve the old literal-match metric but retain partial-success/source PDF and incomplete document status |
| Privacy and isolation | Filename stem and source text absent from new plan; formatted model errors hide confidential input; review/output role objects are independent; existing source models/ownership remain unchanged |

All current source/test Python files also parsed successfully. No paid calls, clinical benchmarks or live PDF ingestion were executed.

## Compatibility, limitations and unresolved semantic cases

1. **Content compatibility:** Five content fields, prompts, citations, word thresholds, model settings, source-package construction, numbering, deduplication and clinical validation are unchanged. The new metadata does not enter provider messages or Admin Panel content. The new optional ingestion-plan field defaults to None when reading old model payloads or using the legacy text-only route.
2. **Intentional completion change:** The source-backed pipeline no longer treats literal coverage as complete clinical document coverage. Drafts still save normally, with the existing partial-success path retaining the PDF. Human review remains mandatory; no approval or production ingestion gate was weakened. This does not revise historical completed artifacts.
3. **No automatic semantic resolution:** Normal runs produce an inventory and unresolved/held questions. Semantic grouping beyond literal evidence requires explicit source-reading records through the independent API. The older generation planner's heuristic candidate context remains unchanged and is not presented as verified semantic knowledge. This milestone does not provide autonomous medical understanding, an annotation UI, authenticated reviewer identity or a new generation route.
4. **Human annotation risk:** Exact source anchors cannot prove the semantic truth of a role assignment or same-population assertion. A mistaken/forged human-source review can be wrong. Separate clinical review remains required. Multiple studies/cohorts, subgroup applicability, crossover arms, adjusted versus unadjusted estimates, outcome hierarchies, abbreviation expansion and implicit anaphora remain semantic review cases.
5. **Conservative holds:** Uppercase abbreviations, ambiguous referents, page/document warnings and nearby qualifications can hold material that later proves safe. A single missing nearby qualification can conservatively hold shared context and dependent questions. Automatic disambiguation/hold clearance was deliberately not implemented.
6. **Extraction bounds:** The plan accounts for the supplied canonical/native evidence and known concerns, not unknown PDF content never represented by extraction or warning inventory. Table cell/header/footnote, figure-panel and visual-text relationships are not solved. No new OCR or interpretation model was introduced.
7. **Planning versus writing:** A represented concept means retained source under a reviewed question, not that a generated chunk includes every fact. A supporting method passage is intentionally not credited as independently written content. This layer therefore never declares final document completion or clinical approval. Richer claim-by-claim generation reconciliation remains future work.
8. **Identity/cost:** The full evidence fingerprint and reconstruction validation are conservative and can add local CPU/memory cost. The new timer makes that cost observable; no real-world performance claim or proof cache is introduced. Existing preparation implementation fingerprints include the edited adaptive planner, and existing validation-hold fingerprints hash source files; future authorized runs can invalidate older preparation/hold reuse under those existing policies. No real cache/checkpoint was modified here.
9. **Historical compatibility:** Existing saved output/checkpoint formats are not migrated or backfilled. Newly executed plans/reports carry the new fields. Existing historical reports can lack this accounting and should not be treated as having undergone this milestone's assessment.

Pydantic 2.13.5 and Python 3.13.7 were verified locally. The official [Pydantic model documentation](https://docs.pydantic.dev/latest/concepts/models/) was consulted for the existing model/serialization API; round-trip and mutation tests verify the installed behavior. Pydantic's structural validation is not medical semantic validation.

## Protected state and milestone status

Before/after SHA-256 checks cover **2,186 protected files** across real input/processed PDFs, saved outputs/checkpoints, databases/logs, prompts/configuration and `.env`. Final verification at **19 October 2026, 09:48:47 UTC / 15:18:47 IST**, saved in `tmp/milestone3/protected-verification.json`, found **zero changed/missing files and zero new files in real runtime directories**. Read-only immutable SQLite inspection showed **paused=1**, **11 LOCAL_REVIEW_READY** and **19 QUEUED**. No queue resume, real database write, production ingestion, Azure request, prompt rewrite or Git initialization occurred.

The offline engineering acceptance criteria passed: an independent source-backed concept structure, explicit reviewed question grouping, preserved provenance, five-way document coverage accounting, visible exclusions/holds and a functional unchanged draft-writing/validation route. The semantic and review limitations above remain explicit; this is not evidence of independent clinical accuracy.

**PASS — Milestone 3 implemented and verified offline. Work stops after concept planning and accounting; the queue remains paused and clinical generation prompts remain unchanged.**

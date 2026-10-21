# GastroAGI Phase A2 — Automatic Source-Grounded Concept Proposals

Date: 20 October 2026. Scope: **Phase A2 only**. Evidence: deterministic synthetic sources and isolated offline tests. **PASS for the scoped proposal engineering acceptance criteria.** This is not medical verification, complete document understanding, or completion of Milestones 4/5.

## 1. Current-state diagnosis

Reviewed the current audit, Milestone 1–5 reports, Phase A1 report, Milestone 6B design, project engineering guidance, existing concept/planning/evidence/source-package models, adaptive dispatch, editorial generation and claim/validator boundaries. The current implementation remains the authority.

The M3 `DocumentConceptPlan` already inventories complete canonical source regions, literal cues, unresolved questions, source holds and explicit reviewed exclusions. It deliberately leaves clinical relationships unresolved unless a caller supplies an actual `human_source_review` attestation. The editorial route accepts only compatible source-bound reviewed questions. Phase A1 corrected one limited grammatical mismatch; its three original rejected chunks remain rejected overall.

A2 adds an independently invoked proposal layer around that inventory. It suggests question categories, source-span labels, literal role candidates and candidate context without creating a reviewed question or scheduling generation. It keeps proposal coverage separate from the inventory's verified coverage. Existing automatic draft generation and all approval boundaries remain unchanged.

Before implementation, copied **109 protected source/test/tool/prompt/configuration/skill/report files** to `tmp/phase_a2/baseline/source/`, with `sha256-manifest.json`, `versions.json` and `validation-fingerprint.txt`. No Git repository was initialized. User PDFs, historical outputs/checkpoints, runtime databases, logs and `.env` were not part of that snapshot.

Installed versions recorded: Pydantic **2.13.5**, pytest **9.1.1**, pypdf **6.9.2**, PyMuPDF **1.27.2.2**, OpenAI SDK **3.26.1**, Playwright **1.62.0**, python-dotenv **1.2.3**. No dependency changes were made. The official [Pydantic model documentation](https://docs.pydantic.dev/latest/concepts/models/) was checked, and serialization, frozen contracts and current-source reconstruction were tested against the installed version. Structural validation does not establish clinical truth.

## 2. Exact files and functions

**No existing file was modified.** Eight files were introduced:

| New file | Responsibility |
|---|---|
| `clinical_proposals/__init__.py` | Explicit public exports; no automatic pipeline registration. |
| `clinical_proposals/models.py` | Frozen internal models, immutable span adapters, limits, statuses and redacted `ProposalError`. |
| `clinical_proposals/source_adapter.py` | `pin_inputs`, `_check_budget`, `source_concerns`, `digest`: pre-copy resource checks, source/inventory validation on pinned copies, auxiliary identity and exact source-concern fingerprints. |
| `clinical_proposals/planner.py` | `propose_concepts`, `validate_proposal_plan`; helpers `_span`, `_roles`, `_categories`, `_counts`; deterministic question/context selection, duplicate handling and coverage. |
| `tests/proposal_support.py` | Twenty fictional source fixtures; no human-review stamps or user PDF reads. |
| `tests/test_phase_a2_concept_proposals.py` | 47 deterministic cases covering safety, identity, budgets, provenance, immutability and integration exclusion. |
| `tools/evaluate_phase_a2.py` | Synthetic evaluation and isolated local microbenchmarks, with output constrained to `tmp/phase_a2/`. |
| This report | Engineering evidence, compatibility, limitations and future integration boundary. |

All runtime implementation files are in the top-level `clinical_proposals/` package. Nothing was added beneath `src/`.

## 3. Data model and explicitly invoked API

```python
from clinical_proposals import propose_concepts, validate_proposal_plan, ProposalLimits

# source_doc and document_inventory are current existing typed models.
proposals = propose_concepts(
    source_doc,
    document_inventory,
    limits=ProposalLimits(max_proposals=256, max_context_characters=20000),
)
validate_proposal_plan(proposals, source_doc, document_inventory)
```

This example declares the interface; it is not an instruction to run the queue. The function has no provider, file-output, database, checkpoint or publication parameter. Normal pipeline entrypoints do not import this package.

`ConceptProposal` fields:

- `proposal_id`, `category`, `proposed_question`.
- `candidate_topic`, `candidate_subtopic`: nullable **exact source span references**, not final editorial labels. Their text stays in the supplied source document. Multiple competing mentions leave the label unresolved; all literal alternatives remain in `potential_roles`.
- `source_region_ids`, `ownership`, `page_numbers`, `section_ids`.
- `potential_roles`: role name, exact evidence span, `basis=literal_cue_only`, `relationship=unresolved`.
- `supporting_context`: complete region span, page/section identities, candidate link kind, exact link evidence and unresolved relationship.
- `context_overflow`: candidate count, complete required character count and candidate-set hash when context cannot fit.
- `concern_ids`, `concern_overflow_count`, `unresolved_relationships`.
- `status`, `duplicate_of`.
- `generation_eligible=False`, `human_source_reviewed=False`, `clinically_approved=False`.

`ProposalPlan` carries version, deterministic plan/source identities, original evidence fingerprint, inventory and auxiliary hashes, limits, proposals, global source concerns, region accounting, character/count diagnostics and source warning IDs. Its document-complete, clinical-approval and generation-eligibility fields are literal false.

`SpanRef` is a frozen buffer/start/end reference, with `from_evidence`/`to_evidence` adapters to the existing `EvidenceSpan`. No free-text source passages, sensitive filename, reviewer identity or provider error is placed in proposal diagnostics. Question strings are generic category templates; candidate clinical labels and mentions remain source pointers. These are internal planning records and do not add fields to the five-field clinical chunk schema.

External/saved proposal records must be reconstructed against current source using `validate_proposal_plan`. Pydantic shape validation or a saved identifier alone is insufficient.

## 4. Clinical question and grouping rules

The algorithm reuses the validated inventory's whole canonical paragraphs and literal cues. It also recognizes existing literal mention patterns and a small set of procedure-selection, contraindication and diagnostic-selection cues. It never splits source at an arbitrary word/character count to manufacture an independent concept.

Question categories are `study_scope`, `primary_outcome`, `secondary_outcome`, `comparative_outcome`, `safety`, `recommendation`, `applicability`, `procedure_selection`, `contraindication`, `diagnostic_selection` and `source_statement`.

Primary/secondary/safety/recommendation cues create distinct candidate questions. A comparator/outcome question is suggested only when those literal cues coexist and a more specific question category was not selected. This co-occurrence is **not** a verified clinical relationship. A mixed primary/secondary paragraph keeps both questions and complete evidence, with `multiple_endpoint_relationships_unresolved`; numbers are not assigned to either endpoint automatically.

Every proposal owns **one complete existing source region**. Related passages are separate, unbound context candidates:

1. Adjacent qualification, safety, limitation or applicability regions are retained as potential context. A source referent can also identify neighboring candidate context. Adjacency does not prove a clinical link.
2. An explicit source phrase such as `described in Methods` can identify a unique named source section, including a nonadjacent page. Its exact reference span is retained. The target's complete regions become context candidates; their roles/results are not transferred to the owned region.
3. Missing or repeated section titles produce `missing_or_ambiguous_explicit_context_reference`; the algorithm does not pick a section by proximity.
4. Context across pages adds `cross_page_relationship_unresolved`. Matching terms alone never join owners or bind arms, populations or outcomes.

This conservative grouping supplies review material rather than claiming a final semantic concept graph. It may leave related questions separate until independent relationship review.

## 5. Ownership, source concerns and unsupported relationships

Pinned model copies are checked with existing evidence/inventory validators. Original source objects, private derived indexes, reviewed plans and reference authority remain unchanged. Source section text is checked against its named buffer. Missing/corrupt evidence, changed inventory regions, stale fingerprints, inconsistent PDF identity and mismatched sections fail closed.

If attached, existing source-package, clinical and multiview plans are checked using their existing validators. Their holds/blockers and alternate evidence warnings remain visible. No proof check is removed or cached by object identity.

`SourceConcern` preserves a stable opaque concern ID, exact source-record hash, source-region/page/span references where available, kind and blocking decision. Kinds are `evidence_warning`, `region_hold`, `package_hold`, `package_blocker`, `source_map_warning`, `clinical_plan_hold`, `multiview_hold`. Free-text warning messages and hold contents remain in the original source models; the fingerprint binds the entire original record. All evidence warnings, including nonblocking/resolved ones, remain represented. Package heading/label holds remain visible without being reinterpreted as a document-wide clinical failure. Unscoped blocking concerns conservatively apply at document level.

Relevant source concerns also apply through selected context. When a concern-link budget overflows, the candidate is blocked explicitly; global concern records are not removed. Existing held inventory regions remain blocked, including empty pages, unmapped concerns, damaged passages, unproved table/figure relationships and unresolved referents. Legacy text-only evidence does not acquire a PDF-byte identity or reviewed status from this API.

All role candidates remain unresolved. No nearby numerical value becomes an arm/outcome assignment. Non-significance is not converted to benefit/equivalence; association is not converted to causation. Conditional/low-certainty language remains in complete evidence rather than becoming a stronger recommendation. Cited/previous-study wording receives `article_vs_cited_study_attribution_unresolved`. Bare numerical passages can be insufficient evidence. No automatic semantic-truth claims are made.

## 6. Status and coverage semantics

| Proposal state | Meaning |
|---|---|
| `proposed` | A limited descriptive question was detected, such as a stand-alone study-design statement; still unreviewed and ineligible. |
| `needs_relationship_review` | A clinical question has literal evidence, but its relationships/labels/context need independent assessment. |
| `blocked_source_evidence` | Existing source hold, affected context, or explicit resource constraint prevents advancing the candidate. The reason distinguishes source damage from a budget hold. |
| `duplicate_proposal` | A prior local proposal has the same category and source wording under the same page, section, context and uncertainty conditions. Both ownership occurrences remain visible. |
| `insufficient_evidence` | Cues exist, but they do not supply an adequate clinical question anchor; for example orphan numbers. |

Duplicates ignore **only leading/trailing whitespace**, including paragraph separators. Internal wording/numbers, different pages, populations, sections, contexts or uncertainty are not semantically normalized. The duplicate points to the first ordered proposal and retains its own original span. Existing source blocks take precedence over duplicate status.

Every original inventory region has a `RegionAccounting` entry carrying its original coverage, unchanged ownership, proposal/context links and reasons. Separate proposal-accounting states are:

- `proposal_attached`
- `blocked_source_region`
- `supporting_context_only`
- `excluded_or_nonclinical`
- `not_yet_assessed`

Structural reference sections are identified as nonclinical **for this proposal pass**, with an explicit reason; this does not create a reviewed clinical exclusion. Previously reviewed exclusions/context remain untouched. Unclassified material and proposal-count overflow remain visible as unassessed. A region may support several candidate questions, but its canonical characters are counted once in this disjoint region ledger. Warning/empty/unmapped concern regions without canonical ownership add zero canonical characters.

Region counts sum to the original region count; character counts sum to the complete canonical buffer. Neither sum is clinical coverage. `proposal_attached` is not `represented_in_concept`, and it never promotes the existing M3 coverage. Document completeness and generation eligibility remain false even when every region has a proposal.

## 7. Deterministic identity and validation

Algorithm version: **`clinical-proposals-v1`**.

Proposal identities bind the algorithm/version, exact current evidence fingerprint, complete inventory hash, auxiliary source-planning hash, explicit limits, question category, owned region/span and candidate context/overflow identity. The plan ID additionally binds all emitted proposal records and region accounting. A filename or chunk number is never the identity basis. Source evidence/warnings/sections/configuration are included through the existing fingerprint; source-map warnings and optional attached planning models are additionally bound through `auxiliary_source_sha256`.

Ordering is canonical source order, then a defined category order. Exact duplicates reference the first occurrence deterministically. IDs and output JSON are deterministic for the same source, inventory, algorithm and limits. A future algorithm change must version the proposal contract; this is not a license to reuse old proposal identities after changing rule semantics.

`validate_proposal_plan` rebuilds the result against current inputs and compares the complete typed plan. Errors use only redacted codes: `SOURCE_IDENTITY_INVALID`, `INPUT_BUDGET_EXCEEDED`, or `PROPOSAL_IDENTITY_INVALID`. A current-source failure does not become a repaired source or an approved proposal.

## 8. Budgets and measured performance

Explicit configurable limits:

| Limit | Default |
|---|---:|
| proposals | 256 |
| combined owned/context characters per proposal | 20,000 |
| context regions per proposal | 8 |
| characters per source region scanned by proposal rules | 50,000 |
| role candidates per source region | 256 |
| concern links per proposal | 128 |
| input model string characters | 16,000,000 |
| input inventory regions | 5,000 |
| input traversal records | 200,000 |

The input traversal is checked before deep-copy/serialization/reconstruction. Its character count includes strings throughout the supplied models, not PDF file bytes. Excessive inputs fail explicitly. Excessive region/role requirements produce whole-region blocked placeholders rather than truncated medical text. The proposal-count cap leaves explicit omitted-question counts and region reasons. A context overflow returns the complete required size/count/set hash and a blocked candidate, rather than a misleading partial context. All original evidence remains available in the untouched source model.

The proposal pass builds section/region indexes once, materializes each assessed region once, and uses fixed bounded lexical passes over that value. It reuses frozen role/context objects within a region where possible. Existing source validators still scan/reconstruct evidence; those checks are intentionally retained. Counters distinguish proposal-region scans from top-level source/package/clinical/multiview validation invocations and do not claim those validators perform no internal work.

Deterministic diagnostics include `input_regions`, `source_validation_rebuilds`, `package_validation_rebuilds`, `clinical_validation_rebuilds`, `multiview_validation_rebuilds`, `proposal_region_scans`, `proposal_characters_scanned`, `candidate_questions`, `proposals_emitted`, `omitted_questions_by_limit`, `duplicate_proposals`, `context_budget_holds`, `region_budget_holds`, `role_budget_holds`, `concern_link_budget_holds`, `unresolved_candidate_relationships`.

Actual isolated microbenchmarks from `python -B tools/evaluate_phase_a2.py`:

| Clinical regions | Canonical characters | Proposals | Median of 5 runs | Min–max | Traced peak allocation |
|---:|---:|---:|---:|---:|---:|
| 10 | 1,037 | 10 | **10.143 ms** | 9.317–10.825 ms | 439,013 bytes |
| 50 | 5,197 | 50 | **43.829 ms** | 42.800–45.512 ms | 2,013,133 bytes |
| 150 | 15,647 | 150 | **134.121 ms** | 131.992–169.299 ms | 6,047,299 bytes |

Each timing includes proposal API source copying, validation, indexing and identity hashing in the same warmed Python process; fixture extraction/inventory construction occurred before these timing windows. Peak allocation is from a separate `tracemalloc` run, excludes preconstructed input models and is not total process RSS. Output equality was checked for the traced run. Setting `max_proposals=5` emitted five candidates and explicitly reported **5/45/145** omitted questions respectively. These measurements describe small synthetic local workloads; they establish no real-PDF or Azure throughput claim. The proposal stage made **zero mock or live model calls**; tokens were not measured.

## 9. Synthetic evaluation outcomes

The frozen in-code fictional corpus has twenty cases. A2 evaluation produced **36 proposals**: **29 needs-relationship-review, 4 blocked-source-evidence, 1 duplicate, 1 proposed, 1 insufficient-evidence**. All have generation/clinical approval false. Full metadata, safe span examples, coverage and timings are in `tmp/phase_a2/evaluation.json`; no source passages are exported.

| Case | Proposals | Result |
|---|---:|---|
| two_arm_trial | 4 | Primary/secondary questions remain separate; whole passages and numerical/endpoint uncertainty retained. |
| negative_result | 1 | Review required; non-significance retained in owned evidence. |
| conditional_low_certainty | 2 | Recommendation/applicability review; no forced choice among competing topic/population labels. |
| distinct_populations | 2 | Separate ownership; no automatic adult/child merger. |
| adverse_events | 1 | Review with whole adverse-event/ascertainment-limitation passage. |
| procedure | 2 | Procedure-selection and contraindication questions, both unbound. |
| diagnostic | 2 | Diagnostic/applicability questions, both unbound. |
| explicit_cross_page | 4 | Nonadjacent Methods context candidates; no population/result transfer. |
| unproven_cross_page | 3 | Two review candidates and one blocked referent; cross-page context unresolved. |
| adjacent_qualification | 5 | Adjacent adverse-event limitation remains a candidate context dependency. |
| table | 2 | Both blocked; unproved relationships and five blocked inventory regions remain visible. |
| ambiguous_numbers | 1 | Review; no numerical arm/endpoint association inferred. |
| duplicate | 2 | First candidate plus duplicate with distinct provenance retained. |
| cited_study | 1 | Article-versus-cited-study attribution unresolved. |
| references | 1 | Body proposal; two structural nonclinical/excluded region entries remain separately accounted. |
| empty | 0 | Two blocked inventory regions; source concerns remain visible despite no proposal. |
| unclassified | 0 | Unassessed material remains unassessed. |
| single_design | 1 | Descriptive `proposed` question, still ineligible/unreviewed. |
| damaged_passage | 1 | Blocked; three affected inventory regions remain visible. |
| orphan_numbers | 1 | Insufficient evidence, no clinical relationship filled in. |

These are engineering expectations for fictional sources, **not clinician-reviewed gold labels**. They do not prove general clinical recall, specificity or correct concept granularity.

## 10. Tests executed and actual results

Inspected existing tests/fixtures and autouse isolation before executing the safe baseline. The existing guards redirect captured runtime paths and local storage to temporary directories, prohibit live Azure clients without explicit fake factories, and keep browser tests on local test servers. Real user-PDF test entrypoints were excluded before collection.

Baseline and final safe full selection:

```powershell
python -B -m pytest tests/ --ignore=tests/test_native_layout_real_source.py --ignore=tests/test_task11_regression.py -k 'not both_protected_real_anchors' -q -p no:cacheprovider --tb=short -o junit_family=xunit1 --basetemp=tmp/phase_a2/final-tests --junitxml=tmp/phase_a2/final-test-results.xml
```

Baseline used the corresponding `baseline-tests` and `baseline-test-results.xml` paths.

| Run | Actual outcome | Pytest time |
|---|---|---:|
| Protected pre-change safe baseline | **791 passed, 2 deselected, 9 subtests passed** | 53.76 s |
| Initial A2 focused tests | **39 passed, 2 failed**, both new tests | 2.00 s |
| Focused after correction | **41 passed** | 1.79 s |
| Expanded focused checks | **45 passed** | 1.77 s |
| Final focused A2 selection | **47 passed** | 1.83 s |
| Final safe full regression selection | **838 passed, 2 deselected, 9 subtests passed** | 56.72 s |

Focused invocations selected only `tests/test_phase_a2_concept_proposals.py`, with `-B`, `-q -p no:cacheprovider --tb=short`, separate basetemp directories and XML reports under `tmp/phase_a2/`. The initial failures identified an incorrect new assertion forcing labels despite multiple source candidates, and failure to recognize otherwise identical duplicate paragraphs whose separator whitespace differed. The label test now asserts explicit ambiguity, and duplicate keys ignore only outside whitespace while retaining exact ownership. No existing test or clinical validator was weakened.

There were **no existing regression failures and zero runtime skips**. The two real-PDF modules named above were excluded, not passed; two `both_protected_real_anchors` cases were deselected. No claim is made about the unexecuted real-PDF selection. Qualified clinical assessment, live Azure/capability tests and staging/production integration were **not executed**.

New tests cover all twenty requested safety areas: two-arm trials, primary/secondary distinction, non-significance, conditional recommendations, low certainty, distinct populations, adverse events, procedure selection/contraindications, cross-page context, retained qualifications, table/figure uncertainty, damaged/missing evidence, duplicates, ambiguous numbers, missing regions, stale fingerprints, context/resource overflow, deterministic identity, no fabricated human review and preservation of generation/validation boundaries. They additionally check ordering, frozen spans, source/private-index immutability, saved-plan tampering, missing/ambiguous explicit section references, auxiliary hold revisions, exact source-concern retention and no source-text/filename leakage.

## 11. Before/after regression evidence

Executed the unchanged A1 comparison runner before and after implementation:

```powershell
python -B tools/evaluate_phase_a1.py --output tmp/phase_a1/phase_a2-baseline-comparison.json
python -B tools/evaluate_phase_a1.py --output tmp/phase_a1/phase_a2-final-comparison.json
python -B tools/evaluate_phase_a2.py
```

The complete original editorial evaluation and legacy comparison records are structurally identical before/after. The eight intended examples remain **5 REVIEW_REQUIRED / 3 REJECTED**; fifteen adversaries remain **11 REJECTED / 4 REVIEW_REQUIRED**, with **15/15 expected reasons retained**. All **31 complete legacy validation reports** match their pre-A1 frozen hashes, and all 31 editorial variant statuses are unchanged. Existing generated candidate text, ownership, counts, prompts and clinical decisions were not changed by proposal creation.

The final full regression selection also covers M1 diagnostics/redaction, rolling concurrency limits and failure handling, retry behavior, source ownership/integrity, preparation/candidate reuse, default extractive generation, temporary SQLite storage/readback and approval boundaries. Passing test counts are supported by protected file equality and complete report comparison, rather than used alone as proof of compatibility.

## 12. Cache and source fingerprint impact

`package_generation.validation_fingerprint()` hashes every Python file under `src/`. Placing a new module there would invalidate saved validation holds even without a default-path behavior change. That is why A2 is outside `src/`; the fingerprint algorithm is unchanged.

Before and after fingerprint:

```text
79d1298b9b4b56a629f33c3c7581cb0c49719d7715cad135c84be7ffbc8d8b13
```

`tmp/phase_a2/compatibility.json` records **109 protected files, zero changed/missing files**, unchanged fingerprint and identical editorial/legacy comparisons. Existing preparation-cache module hashes, successful candidate identity, validation/source holds, checkpoint formats and prompts remain unchanged. No historical checkpoint/hold was inspected, rewritten or migrated. A2's own identities are separate and create no compatibility claim for altered source evidence.

## 13. Automatic proposals versus authenticated human review

Automatic proposals detect literal evidence candidates and unresolved questions. They never establish that population, treatment, comparator, outcome, numerical value, time point and qualification belong to the same clinical proposition. Span validity, proximity and regex matches are not medical relationship proof.

Existing `SourceReview` represents an explicit caller-supplied human source-reading attestation; the current model alone is not an authentication service. A2 creates **no** `SourceReview`, `ReviewedQuestion`, review ID, approval ID or five-field clinical chunk. Existing legitimate reviewed input records may be validated/preserved, but their authority is not manufactured or transferred to an automatic proposal.

Tests demonstrate the proposal plan cannot satisfy `DocumentConceptPlan`'s schema and cannot pass the existing editorial boundary; the mocked writer is never called. All proposal generation/clinical approval flags are fixed false. A proposal is not an editorial generation unit, source-reviewed concept or clinically approved knowledge.

## 14. Clinical limitations and remaining A1/M4 blockers

This is a conservative lexical proposal system, not full clinical NLP. It can miss terminology, produce generic questions or separate questions that a clinician would combine. Topic/subtopic labels may be null because extraction cues overlap or competing entities/populations are present. Source context is recognized only through limited structural references and conservative neighbors. Cross-paragraph claims, ambiguous abbreviations, quoted-study attribution, table/figure semantics, numerical associations, causal meaning and clinical granularity remain unresolved. Dense/corrupt documents can exceed explicit limits. Attached legacy proof validation can dominate costs; no integrity check was removed to improve timings.

Source holds can be broader than an individual passage when their scope is uncertain. Structural accounting cannot establish that all clinically relevant knowledge was identified. No clinician reviewed the corpus, and no provider-generated editorial quality was evaluated here.

Phase A1 remains **PARTIAL**: the three original examples retain rejection at chunk level, attribution/causal/implementation-strength wording still needs adjudication, and four original adversaries remain review cases rather than deterministic hard rejections. A2 does not repair those validators or candidates. Milestones 4 and 5 remain **PARTIAL**; no status in their reports was changed.

## 15. Possible A3 boundary — design note only

A separately authorized A3 could present source-span proposals and unresolved context to a review experience, then define an independently tested proposal-aware editorial contract. It must decide which uncertain relationships permit only review-held drafts and which require source review before any request. It must revalidate source/proposal identity, preserve exact evidence and review boundaries, and avoid casting an automatic proposal to `ReviewedQuestion` or generating fake review identifiers.

No such adapter, queue route, generation invocation, storage ingestion or review attestation is implemented here. Milestone 6B remains an isolated design; A2 does not implement 6C or any production approval/publishing service.

## 16. Backward Compatibility and Regression Report

- **Existing files modified:** none. **New files:** the eight files listed in section 2. Evidence artifacts are isolated under `tmp/phase_a2/`, plus the two newly generated A1 comparison files above.
- **Baseline/final results:** 791 → 838 passing safe regression tests; 9 subtests passed both times; 2 real-PDF cases deselected both times. Two initial new-test failures and their corrections are reported. No existing regression failed.
- **Invariants demonstrated:** complete legacy report equality; unchanged prior editorial outcomes and adversarial reasons; exact source ownership and character accounting; immutable caller evidence; deterministic ordering/IDs; retained holds/uncertainty; source-revision rejection; bounded output/explicit overflow; no fabricated review; generation/approval exclusion. Existing concurrency, retry, cache reuse, storage/readback and gate tests also passed.
- **Performance:** local proposal timings and traced allocations are reported with scope limits. No old-pipeline, paid-model, fresh-PDF or Azure speedup is claimed. Existing code/default paths are byte-identical.
- **Intentional behavior change:** none to existing pipeline behavior. New behavior is available only through explicit calls to the isolated API.
- **Protected state:** no existing queue worker/service was started or resumed; no user PDF, live SQLite database, historical output/cache/checkpoint was opened for this work. No `.env`, credential, production setting, prompt, source review, approval gate or runtime record was modified. Tests used temporary runtimes and synthetic evidence. The paused queue was left untouched; no new live-database query is claimed to verify its flag.
- **Security:** exported proposals/metrics use generic questions, opaque IDs, hashes and exact span references; no source passages, sensitive filename or raw exception is logged. Missing/invalid inputs use sanitized errors. There is no new authenticated endpoint or production write surface.
- **Rollback:** remove the new `clinical_proposals/` package, its two new test/support files and new evaluation tool together; preserve or archive this report and temporary evidence as desired. Restore no existing source file and perform no database/cache migration. Do not remove the preceding A1 implementation. Recheck the protected manifest and rerun the safe selection after any rollback.
- **Remaining assumptions:** input source identity rests on the existing evidence builders; synthetic PDF identities are test identifiers, not live PDF certification. Proposal completeness/clinical correctness, reviewer authentication, real-world editorial fidelity, staging and production retrieval remain unverified.

## 17. Acceptance and stop condition

The scoped offline acceptance criteria are demonstrated: deterministic typed evidence-bound proposals; complete source-region/hold accounting; unresolved relationship metadata; explicit resource limits; no fabricated human review; unchanged prior outputs/decisions and no source/cache fingerprint impact. All final safe regressions passed.

No A3/A4/A5, Milestone 6C, paid Azure request, existing user-PDF ingestion, production data write, automatic approval or publishing was performed. Work stops after A2 for independent review.

**PASS — Phase A2 proposal engineering only. Automatic proposals remain unreviewed and ineligible for editorial generation or production; Phase A1 and Milestones 4/5 remain PARTIAL.**

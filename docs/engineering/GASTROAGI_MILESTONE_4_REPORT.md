# GastroAGI Milestone 4 — Opt-in Editorial Clinical Draft Generation

Date: 19 October 2026. Scope: Milestone 4 only.

Implemented an opt-in editorial generation route and an independent deterministic claim/source assessment prototype. It preserves the five-field JSON contract and exact internal evidence, produces paraphrase candidates without rewriting their text after generation, and returns a separate review/rejection report. The existing extractive route remains the default.

**Clinical readiness is not established.** The reproducible evaluation uses engineer-authored fictional sources and mock responses, not live model generation or clinician-reviewed gold labels. Five of eight intended paraphrase examples remain review-required; three are rejected by existing grounding safeguards. This is a usable engineering prototype, not a claim that general clinician-ready paraphrasing has been achieved.

## Prerequisite and editorial authority

Read the Milestone 3 report, source-planning/coverage implementation, generator, package generator, validator and numeric-evidence code. A fresh prerequisite selection verified all Milestone 3 tests plus source-package/generator regressions: **85 passed and 4 subtests passed**. Subsequent full-suite runs also include Milestones 1–3.

The supplied attachment contains the earlier audit request and editorial objective, not a separate verbatim Admin Panel prompt. The user explicitly confirmed: **use the Milestone 4 writing contract**. That contract governs the proposed prompt; no missing external prompt was invented.

## Active versus proposed contracts

| Dimension | Existing article route | Existing default source-package route | New explicit editorial route |
|---|---|---|---|
| Prompt | `gastroagi_chunk_rules.md` | `gastroagi_source_package_rules.md` | New `gastroagi_editorial_rules.md`, opt-in v1 |
| Writing | Clinician-facing language, source-supported interpretation | Exact complete source passages; explicitly forbids paraphrasing | Clinician-facing paraphrase of one source-bound question; no external facts |
| Normal word target | 150–250 | 90–250 when supported | **100–200**, subordinate to source fidelity |
| Length validation | Existing 90/300 hard bounds and 150/250 warning targets | Same existing validator | Out-of-range length is review-required; no padding or truncation; justification recorded separately |
| Quantity | Content driven; typically 4–8 | One to three per source package | One per explicitly bound question; **no document cap of eight**; independent request budget |
| Source | Existing article/unit/package boundaries | Witnessed bounded native/visual evidence | Exact owned passages plus explicitly reviewed supporting context from M3 |
| Citation | Existing source-reference checks | Exact source authority | Same checks plus exact equality to the supplied source authority |
| Approval | Existing validation and human-review gates | Existing draft review gates | Never automatic PASS/approval; REVIEW_REQUIRED or REJECTED |

Both original prompts and the package generator's extractive/correction instructions are unchanged. The new prompt is **363 whitespace-delimited words**. It contains the five-field envelope once and concise instructions covering independent concepts, searchable labels, uncertainty, populations, comparators, endpoints, time points, important statistics, safety, limitations, guideline strength and certainty. It explicitly distinguishes noninferiority from superiority, non-significance from equivalence, and unmeasured harm from absence of harm.

The prompt permits abbreviation expansion only when supplied by the evidence, treats embedded source instructions as data, and preserves DOI-only references without invented bibliography. An empty response uses the existing source-abstention shape and produces a typed hold rather than invented content.

## Exact changes and entrypoints

| File | Change |
|---|---|
| `prompts/gastroagi_editorial_rules.md` — new | Opt-in editorial contract above |
| `src/editorial_generation.py` — new | `EditorialDraft`, `generate_editorial_draft`: source/plan/reference preflight, pinned source/review snapshots, bounded serial concept requests, existing JSON/reference validation, independent claim assessment, sanitized validation and internal ownership report |
| `src/claim_assessment.py` — new | `ClaimAssessment`, `ChunkAssessment`, `assess_editorial_chunk`, `claim_totals`; internal normalization, candidate segmentation, explicit quantity-association, local negation and scope-consistency checks |
| `src/chunk_generator.py` | Add optional `editorial_plan=None` to `generate_chunks`; dispatch only when explicitly supplied, with diagnostics required and incompatible legacy options rejected |
| `src/chunk_validator.py` | Add `editorial=False`; default logic unchanged. Explicit editorial calls use the 100–200 warning target and always add mandatory editorial review. All other deterministic checks remain enabled |
| `tests/fixtures/editorial_evaluation.json` — new | Eight fictional source/example pairs and fifteen adversarial mutations; explicitly not clinician-reviewed |
| `tests/editorial_support.py` — new | Synthetic evidence/review stubs and injected provider, with no PDF ingestion or network |
| `tests/test_milestone4_editorial.py` — new | 15 test functions expanding to 51 cases for contract, safety, fidelity, compatibility and failure handling |
| `tools/evaluate_milestone4.py` — new | Offline evaluation using only the frozen synthetic corpus and mock provider; emits metadata/reason codes under `tmp/milestone4` |
| This report — new | Contracts, examples, actual results, limitations and rollout boundaries |

No new dependencies, model settings, provider retry policies, production approval gates, queue entrypoints, database schemas, existing chunks or historical checkpoints were changed. `src/package_generation.py`, `src/numeric_evidence.py`, M3 source planning/provenance, pipeline orchestration and local storage remain unchanged.

### Explicit opt-in

```python
# reviewed_plan is an independently source-reviewed M3 DocumentConceptPlan.
# provider must be explicitly supplied. This example does not execute a call.
audit = {}
generated = generate_chunks(
    extraction,
    source_doc=source_map,
    editorial_plan=reviewed_plan,
    provider=provider,
    diagnostics=audit,
)
report = audit["editorial"]["validation"]
```

The direct API `generate_editorial_draft(...)` returns `EditorialDraft.generated` and `.validation` separately. The generated document retains the existing `article`/`chunks` envelope and lowercase JSON fields `topic`, `subtopic`, `section`, `reference`, `original_text`; `chunk_number` remains metadata. Claim/source/provenance fields are never inserted into those five content fields.

The opt-in requires a valid M3 plan with at least one `source_bound` question and `relationship_basis=human_source_review`, an explicit source map and injected provider. Unresolved/held questions are listed as unscheduled, never counted as covered. The source graph/plan is validated and copied before dispatch. Missing evidence, stale reviews, corrupt buffers, invalid citation identity or oversized/budget-exceeding selections stop before a provider call.

The prototype allows at most 80 questions and 20,000 source characters per request by default; direct callers can set explicit budgets. All selections are checked before dispatch. The model receives only the selected complete owned/context passages and source metadata. A ten-question test produces ten numbered draft candidates; a nine-request budget rejects the same selection before dispatch. This is a source-count/budget test, not proof that ten similar procedure examples are clinically independent or good writing; duplicate checks remain active.

The new route is serial and has no queue/CLI/environment activation. It does not reuse legacy `generation_plan`, custom prompt, progress callback, concurrent scheduler, correction feedback or attempt-directory semantics. Passing those incompatible options through `generate_chunks` fails before calling a provider. There is no new checkpoint writer, automatic regeneration, retry loop, database ingestion or publication adapter. Existing provider-level transport retries remain owned by the supplied provider.

## Evidence versus editorial writing

Exact source passages remain in the pinned evidence document and bounded request. Internal ownership records reference canonical source spans and separate supporting-context spans, together with the M3 plan/question identity and complete evidence fingerprint. No evidence is replaced with its paraphrase. The writer never supplies trusted claim mappings or self-certification fields; extra fields fail the existing schema checks.

The output text is retained as supplied after existing normalization. The implementation never pads, truncates, inserts clinical qualifications, repairs a fact from memory or silently removes a rejected candidate. Rejected text is visible in the returned review package, with `REJECTED` validation status, rather than presented as a safe stored chunk.

Topic must retain the explicitly source-bound clinical entity; an unrelated Topic produces `EDITORIAL_TOPIC_IDENTITY`. Existing generic-classification, precise Section, citation, independence, abbreviation, extraction-artifact, consistency and duplicate checks remain active. Matching a label alone is not proof of correct medical classification or of every claim in a Section title.

For length exceptions, internal assessment records `none`, `short_source_evidence` or `source_specific_justification_required`. A short-source reason is available only when the selected source itself is below the target. Longer drafts and unsupported shortness require source-specific editorial review. No exception constitutes approval. Tests prove that short and over-300-word drafts are not modified, while the default legacy validator still applies its original hard bounds.

## Independent claim/source assessment prototype

Sentence-like spans of Original Text become **claim candidates**, identified by output offsets and opaque hashes. This is not asserted to be complete semantic claim decomposition. Source spans are labeled `source_span_candidates`, not proven claim correspondences. All exported records contain offsets, IDs, controlled statuses/reasons and counts rather than source passages, clinical output or provider error bodies.

Four claim statuses are available:

- `supported`: narrow literal source consistency only. It does **not** mean verified clinical truth, adequate context, complete evidence coverage or approval.
- `contradicted`: a recognized explicit local consistency conflict, such as an unambiguous arm/value association or strength change.
- `requires_review`: a paraphrase, unresolved scope/association or insufficiently attributable relationship. Lexical overlap and semantic similarity cannot clear it.
- `missing_evidence`: unavailable selected evidence or a numerical value absent from that evidence.

The checks retain existing `numeric_evidence` parsing for decimal values, units, signs/operators and glyph uncertainty. Closed textual forms (`quantity with/in/for/on entity` and `entity: quantity`) can expose explicit arm/value changes. Multiple quantities for the same entity remain ambiguous; the checker does not choose an endpoint by proximity. Explicit unit/sign conflicts on an unambiguous association are flagged. Quantities outside selected evidence cannot be rescued from unrelated document passages.

Statistical polarity checks distinguish an explicitly named endpoint from an unscoped “difference.” Negative evidence about another endpoint cannot prove a contradiction. Unscoped significance reversals receive a source-review reason; clearly aligned explicit reversals can be contradicted. Noninferiority/superiority and negated noninferiority patterns, conditional-to-strong recommendations, low-to-high certainty, adverse-event negation and non-significance-to-equivalence risks have additional narrow checks.

Population, treatment, comparator, endpoint, time-point, qualification, limitation, recommendation-strength and certainty anchors are also checked for preservation. Missing literal anchors remain review cases, including faithful synonyms. This deliberate conservatism avoids claiming semantic identity from matching numbers or similar vocabulary.

The existing deterministic validator then independently checks each candidate against its bounded source. Its errors are retained, including errors against intended paraphrases. Editorial mode changes **only length handling** and adds mandatory review; it does not switch off source grounding or numerical validation. Conflicted/missing claims add an error; unresolved scope adds a warning. Every candidate receives mandatory editorial review, so no candidate has automatic PASS or clinical approval—even a copied source paragraph with all literal claims supported.

`ClaimAssessment` fields are `claim_id`, `chunk_number`, `char_start`, `char_end`, `status`, `reasons`, `source_span_candidates`, `support_basis`. `ChunkAssessment` adds candidate lists, chunk disposition/reasons, `word_count`, `length_exception`, and false `clinically_approved`.

The new diagnostic section `editorial` records `version=editorial-v1`, prompt SHA-256, plan/fingerprint, unscheduled concept IDs/statuses and sanitized validation. Validation contains `claim_assessments`, four-way `claim_totals`, `source_ownership`, false completion/approval/qualified-clinician-review flags and `assessment_method=deterministic_consistency_prototype_not_semantic_proof`. Existing observability still distinguishes logical model calls from unknown HTTP attempts; mocked provider calls are not billed HTTP measurements. The new elapsed stage is `editorial_generation`.

## Representative synthetic before/after

The following is **fictional evaluation data**, not medical guidance or a real trial. No real clinical source or stored output is reproduced.

For both examples the clinical fields are:

| Field | Value |
|---|---|
| Topic | Treatment alpha |
| Subtopic | Adults with condition delta |
| Section | Primary remission with treatment alpha versus placebo at 8 weeks |
| Reference | doi:10.1234/synthetic123 |

**Before: extractive Original Text, 129 words**

> In a randomized trial, adults with condition delta received treatment alpha or placebo for 8 weeks. The primary outcome was remission at 8 weeks. Remission occurred in 40% with treatment alpha and 38% with placebo. The difference in the primary outcome was not statistically significant. The trial did not establish equivalence between treatment alpha and placebo. Follow-up was limited to 8 weeks, and longer-term remission was not assessed. The trial excluded children and adults receiving another investigational treatment. Remission was assessed using the same protocol in both treatment groups. Outcome assessors recorded remission at the planned follow-up visit. Missing observations were retained in the study records for review, and the report did not provide a separate estimate for participants with missing observations. Applicability outside the trial population remains uncertain.

**After: intended editorial Original Text, 111 words**

> For adults with condition delta, the randomized trial compared treatment alpha with placebo over 8 weeks. The primary outcome, remission at 8 weeks, occurred in 40% with treatment alpha and 38% with placebo. The difference in the primary outcome was not statistically significant; equivalence between treatment alpha and placebo was not established. Follow-up was limited to 8 weeks, with longer-term remission unassessed. Children and adults receiving another investigational treatment were excluded. Both groups underwent remission assessment using the same protocol at the planned follow-up visit. Missing observations remained available in the study records, but no separate estimate was reported for participants with missing observations. Applicability beyond the trial population remains uncertain.

The mock returns this prewritten paraphrase unchanged. It passes the retained deterministic checks into **REVIEW_REQUIRED**, not clinical PASS. Eight sentence-like claim candidates remain semantic review cases. The copied comparison has eleven narrowly supported literal candidates and also remains REVIEW_REQUIRED. Neither establishes full-document coverage.

A second intended example preserves **conditional** guidance and **low certainty**, using 53 words because its entire synthetic source has only 51 words. It is not padded to 100 words. It remains **REJECTED** by existing `SOURCE_GROUNDING` checks on paraphrased recommendation wording. The report does not relabel that draft as clinician-ready to improve an evaluation score.

## Reproducible evaluation findings

Corpus: `tests/fixtures/editorial_evaluation.json`. Categories: randomized trial, narrative review, conditional guideline, procedure, negative result, noninferiority, adverse events and uncertain descriptive dose evidence. All sources/examples are engineer-authored, all `clinician_reviewed` labels are false. Test-only synthetic source-review stubs exercise the M3 API; they are not claims of real clinical review.

Commands executed:

```powershell
python -B tools/evaluate_milestone4.py --output tmp/milestone4/evaluation.json
python -B tools/evaluate_milestone4.py --output tmp/milestone4/evaluation-final.json
python -B tools/evaluate_milestone4.py --output tmp/milestone4/evaluation-release.json
```

The three executions reflect successive safety refinements and produced the same aggregate findings. `evaluation-release.json` corresponds to the final Topic check and source assessment implementation. The runner only writes sanitized metadata below `tmp/milestone4`; source/example text remains in the synthetic fixture.

| Measurement | Actual result |
|---|---:|
| Source cases | 8 |
| Variants per execution: editorial + extractive + adversarial | 31 |
| Mock generation calls per execution | 31 |
| Actual/live model or Azure requests | **0** |
| Intended editorial examples: review-required / rejected | **5 / 3** |
| Adversarial variants | 15 |
| Expected dedicated reason identified | **15 / 15** |
| Adversarial variants: rejected / review-required | **11 / 4** |
| Automatic PASS / clinical approvals | **0 / 0** |
| Qualified clinician reviews | **0** |

The 111-word trial example meets the normal target. The seven other examples are 46–53 words, drawn from deliberately short source fixtures. They exercise preservation without padding, not a claim that most real articles warrant short chunks. Eight extractive comparisons remain review-required.

Detected mutations include trial/safety arm swaps, invented statistics/sample/rate, changed time point, altered dose unit, changed population/endpoint, strengthened recommendation, inflated certainty, significance reversal, noninferiority-to-superiority and denial of reported adverse events. Endpoint/population changes and some unscoped polarity/event statements remain held rather than claimed to be semantically disproven.

The three rejected intended paraphrases concern a review, guideline and uncertain dose description; all retained errors are `SOURCE_GROUNDING`. Their semantic truth is not established by engineer intent. They demonstrate that the inherited lexical safeguards can block intended source-faithful editorial wording, and that this transition has not solved general paraphrase validation. No safeguard was downgraded to make the examples pass.

This evaluation measures deterministic behavior on prewritten mock responses. It does **not** measure live prompt compliance, model hallucination rate, clinical accuracy, professional editorial quality, cost, latency or real-world sensitivity/specificity.

## Tests and actual outcomes

Existing tests/fixtures and live-capable entrypoints were inspected. Only named offline modules or `tests/` were executed. The existing autouse fixtures redirect runtime files/databases/defaults into temporary directories and require injected fake Azure clients. Root live diagnostics were excluded; mocked connectivity tests are not real connectivity checks.

All pytest runs used `python -B -m pytest`, `-q -p no:cacheprovider --tb=short`, a separate `--basetemp=tmp/milestone4/<run>` and matching JUnit file. Exact selections/artifacts:

| Selection / JUnit artifact under `tmp/milestone4` | Actual outcome |
|---|---|
| M3 + source-package + generator; `prerequisite-results.xml` | **85 passed, 4 subtests passed, 3.76 s** |
| Initial M4 module; `focused-results.xml` | **3 failed, 40 passed, 2.86 s** |
| M4 + prerequisite modules; `boundary-results.xml` | **128 passed, 4 subtests passed, 4.06 s** |
| Expanded M4; `focused2-results.xml` | **1 failed, 47 passed, 2.52 s** |
| M4 + M1 + M2 + M3 + source-package + generator; `boundary2-results.xml` | **177 passed, 4 subtests passed, 10.38 s** |
| Endpoint-specific significance checks, M4 module; `scope-final-results.xml` | **50 passed, 2.17 s** |
| Full `tests/` before final Topic guard; `offline-results.xml` | **671 passed, 7 skipped, 9 subtests passed, 50.71 s** |
| Final full `tests/`, including source-bound Topic regression; `final-offline-results.xml` | **672 passed, 7 skipped, 9 subtests passed, 47.14 s** |

The first three failures exposed ambiguous significance attribution and quantity parsing that stopped before a unit. The parser now consumes the existing recognized unit before checking an explicit association; unscoped significance reversals receive an explicit review reason, while named-endpoint conflicts are checked locally. An additional regression prevents borrowing a negative result from another endpoint.

The later count test initially used repeated page headings that the existing source mapper correctly held. Its fixture now uses distinct source-owned paragraphs on one synthetic page. No source hold, corroboration check or warning was disabled to make the test pass.

Other checks cover missing/stale/corrupted source, request budgets, legacy-option rejection, exact reference/source identity, rejection of model-supplied claim fields, unknown/malformed provider failures without private error text, unchanged default prompts/limits, exact five-field output, preserved evidence, zero speculative requests and logical-versus-unknown-HTTP accounting.

Seven preexisting skips are unavailable real-source fixtures: two multiview anchors, three native-layout regressions and two TASK11 regressions. They are not passed clinical accuracy checks. The final full-suite result covers the final source implementation; only this report was updated afterward. All current Python sources/tests and the new evaluation runner parsed successfully.

## Compatibility, rollback and remaining limits

1. **Rollback:** omit `editorial_plan` to use the unchanged default route and prompts. There is no environment, UI or queue toggle to undo, no migration and no editorial checkpoint to convert. The new prompt has a distinct path/version/hash; it is never swapped into legacy generation automatically.
2. **Persistence and gates:** no storage or publication adapter was added. Existing stored chunks and historical outputs remain untouched. Review/rejection reports must accompany returned drafts; the wrapper requires diagnostics. Existing eligibility/approval gates remain in force. A future production adapter would require separate implementation and authorization.
3. **Coverage:** M3 source ownership and coverage are unchanged. The new report always states incomplete document coverage and false clinical approval. A copied sentence, paraphrase or arbitrary chunk count cannot complete the document. Claim-source candidate spans are not proof of comprehensive medical coverage.
4. **Clinical limits:** rules cannot prove arbitrary paraphrases, causality, subgroup applicability, adjusted estimates, procedural sequence safety, absence of harms or nuanced recommendation scope. Ambiguous abbreviations/structured evidence remain M3 holds. Number words, unit conversions, uncommon syntax, indirect negation and novel endpoint expressions can escape a dedicated pattern; all paraphrases still require clinical review.
5. **False acceptance/rejection:** narrow literal consistency is not clinical truth. Human source-review records may themselves be mistaken or unauthenticated. Existing lexical checks can reject intended faithful wording, as three examples show. No embeddings, similarity score or writer self-validation is used as proof.
6. **Operational limits:** this is a bounded serial prototype without resume, rolling scheduling, corrections or partial-package checkpoint recovery. A failed request aborts the new package with sanitized failure accounting. Legacy scheduling/recovery behavior is unchanged. The new API requires an explicitly injected provider; no live request was invoked in this work.
7. **Cache compatibility:** successful legacy candidate identities/prompts are unchanged. The existing validation-hold fingerprint hashes Python sources, so a later authorized run can invalidate held-validation reuse after code changes. No real candidate, preparation file, validation hold or database row was rewritten here.

Python 3.13.7/Pydantic 2.13.5 were verified locally. The official [Pydantic model documentation](https://docs.pydantic.dev/latest/concepts/models/) was consulted; installed behavior is covered by the tests. Structural model validation is not clinical semantic validation.

## Separate quality conclusions and milestone status

Final protected-state verification at **19 October 2026, 10:13:46 UTC / 15:43:46 IST** found **2,195 protected files unchanged**, **zero missing/changed protected files** and **zero new real runtime files**. The manifest covers real input/processed PDFs, outputs/checkpoints, databases/logs, existing prompts/configuration, `.env` and nine unchanged source/provenance/provider/storage modules. The new opt-in prompt is a separate authorized addition. Evidence is saved in `tmp/milestone4/protected-verification.json`. Read-only immutable SQLite inspection confirmed **paused=1**, **11 LOCAL_REVIEW_READY** and **19 QUEUED**. No production write, live PDF ingestion, Azure connectivity check, paid model request, queue resume or Git initialization occurred.

**Automatic engineering assessment:** opt-in dispatch, envelope/provenance preservation, dedicated adversarial reasons, mandatory review/rejection and legacy compatibility are implemented and tested. The corpus records all fifteen requested adversarial changes without automatic approval.

**Qualified clinician assessment:** **not performed**. No clinician-reviewed reference examples were supplied for this evaluated corpus. The model-facing prompt was not tested against live generation. Professional clinician-ready quality and general paraphrase fidelity therefore remain unverified.

**PARTIAL — Milestone 4's opt-in generation and source-assessment prototype are implemented; general clinician-ready paraphrase validation is not established, with three intended examples still rejected and no qualified clinician review. Work stops after Milestone 4; nothing is published or ingested.**

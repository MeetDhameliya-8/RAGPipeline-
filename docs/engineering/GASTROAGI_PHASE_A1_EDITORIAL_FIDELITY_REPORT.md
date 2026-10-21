# GastroAGI Phase A1 — Editorial Fidelity and Source Grounding

Date: 20 October 2026. Scope: Phase A1 only. **Engineering status: PARTIAL.**

One demonstrated grammatical false rejection is corrected in the opt-in editorial validator. The three original rejected examples still remain rejected at chunk level. Their remaining language is outside the narrowly verified equivalence, and two examples contain additional interpretive concerns. No fixture or prompt was changed to improve the result. Milestones 4 and 5 remain **PARTIAL**; this work does not establish clinical acceptance.

## 1. Initial state and reproducible baseline

Reviewed the current pipeline audit, Milestone 1–5 reports, Milestone 6B integration plan, project RAG engineering guidance, editorial generator, claim assessment, validator, generator dispatch, concept/evidence contracts, numeric evidence, editorial prompt and evaluation fixtures. Current code was used to establish behavior rather than relying on historical line numbers.

The existing editorial route requires an explicitly supplied source-reviewed concept plan and injected provider. The ordinary extractive/package route remains the default. Editorial generation returns unchanged candidate text plus a separate validation report, never clinical approval. The fixture review attestations are engineer-authored test stubs, **not qualified human clinical reviews**.

Before source changes, created `tmp/phase_a1/baseline/source/` containing **83 non-runtime files**, and `tmp/phase_a1/baseline/sha256-manifest.json` containing **90 protected hashes**, including the audit and prior reports. There is no Git repository; none was initialized. No user PDF, runtime database, historical cache/checkpoint, secret or historical generated output was included.

Baseline evidence:

- `tmp/phase_a1/baseline/dependency-versions.json`
- `tmp/phase_a1/baseline/evaluation.json`: 8 source cases, 31 variants, 31 injected mock calls, zero live calls.
- `tmp/phase_a1/baseline/grounding-trace.json`: synthetic case IDs, sentence offsets, lexical-match measurements and rule findings.
- `tmp/phase_a1/baseline-test-results.xml`: **703 passed, 2 deselected, 9 subtests passed**, 52.66 seconds reported by pytest.
- `tmp/phase_a1/baseline/legacy-validation.json`: full reports for 31 synthetic variants from the protected pre-change validator. The reproducible capture script loads that preserved module explicitly. The portable regression fixture stores their canonical SHA-256 values, not source text.

Installed versions recorded before implementation: pypdf **6.9.2**, PyMuPDF **1.27.2.2**, OpenAI SDK **3.26.1**, Pydantic **2.13.5**, python-dotenv **1.2.3**, Playwright **1.62.0**, pytest **9.1.1**. No dependencies were added, removed or updated.

Initial intended examples: **5 REVIEW_REQUIRED / 3 REJECTED**. Adversaries: **11 REJECTED / 4 REVIEW_REQUIRED**, all **15 expected reasons identified**. No automatic PASS or clinical approval.

## 2. Exact rejection mechanism

`validate_chunks` previously passed both editorial and extractive text to `_validate_source_grounding`. Its hard recommendation traceability condition was:

```python
_contains_recommendation_language(sentence_normalized) and not _has_source_support(sentence, source_text)
```

`_contains_recommendation_language` uses substring matching, so `recommend` also matches the noun `recommendation`. `_has_source_support` accepts literal inclusion, very short sentences, or content-token overlap of at least **0.72 against one source sentence**. `_split_sentences` splits on sentence punctuation followed by whitespace; a semicolon does not split a sentence. Combining two supported propositions can therefore reduce their match against either individual source sentence. These are lexical measurements, not clinical entailment checks.

All four baseline hard findings across the three examples were `SOURCE_GROUNDING`, category `SOURCE_EVIDENCE_MISSING`, with the recommendation traceability message. None arose from `_contradicts_source` or `_recommendation_negation_conflict`. Numeric or citation failure was not the rejection cause. This does **not** establish that the entire examples are clinically faithful.

All three selected packets contain the complete synthetic case source, with the Methods heading. Ownership is present, supporting-context lists are empty, and no relevant fixture sentence was omitted or truncated. Source selection, provider transport, JSON parsing and reference identity did not cause these failures.

### Case 1: `review_uncertainty`

The rejected final sentence occupies generated character offsets **[299, 409)**, sentence index **3** (zero-based). It combines a passive non-recommendation statement with treatment decisions being outside the review's scope.

The source explicitly says the review did not recommend treatment alpha as standard care, then separately states that treatment decisions were outside scope. The candidate combines these statements, changes active to passive voice, and uses `remained` and a possessive scope formulation. The best one-sentence overlap is **0.50**, below 0.72.

**Classification:** demonstrated lexical segmentation/wording mismatch (B), with unresolved attribution/generalization scope (C). “The review did not recommend” is narrower than an unqualified statement that a treatment was not recommended. The opening candidate context identifies the narrative review, but this validator cannot independently prove that the passive sentence preserves that attribution. No direct clinical contradiction is established by the legacy error itself.

**Action:** retain rejection. No passive-voice, actor deletion or cross-sentence equivalence was introduced. Qualified review should adjudicate attribution and independence before changing this case's acceptance expectation. The source and candidate are retained unchanged.

### Case 2: `conditional_guideline`

Two baseline hard findings:

| Generated range / sentence | Selected source evidence | Cause and action |
|---|---|---|
| **[97, 178)** / index 1 | Complete source statement: recommendation is conditional, with low certainty of evidence | Candidate changes only the grammatical connectors to `remains` and `is based on`; both explicit attributes retain their roles. Best overlap **0.666667**. This is the narrowly demonstrated grammatical false rejection (B); now emits a review warning. |
| **[316, 417)** / index 4 | Source separately states applicability to children was not established and the panel did not provide a strong recommendation | Candidate combines these clauses and changes the attributed subject to guidance. Best overlap **0.375**. This remains outside the verified grammar and retains its hard finding (B/C). |

There is also an independent concern not responsible for the baseline hard errors: the candidate says implementation **requires** consideration, whereas the source identifies patient preferences and feasibility as implementation considerations. A mandatory implementation requirement cannot be established from that wording alone (A/C). General semantic-review findings remain; the code does not adjudicate this distinction or repair the candidate.

**Action:** reduce this example's grounding error count from **2 to 1**, retaining chunk status **REJECTED**. Do not treat retained conditional/low-certainty words as proof of the rest of its recommendation relationships.

### Case 3: `uncertain_dose`

The rejected final sentence occupies **[282, 374)**, sentence index **4**. The source describes the recorded dose as descriptive and not a prescribing recommendation. The candidate restates that characterization using different nominal/predicate wording. The best overlap is **0.50**; substring `recommend` triggers the recommendation rule despite the negative characterization.

**Classification:** lexical mismatch in the final sentence (B), plus a separate unsupported explanatory relationship (A/C). Earlier, the candidate says incomplete outcome data **prevented** a quantitative benefit estimate. The source states that outcome data were incomplete **and** that no estimate was provided; it does not explicitly establish that causal explanation. The independent assessment retains `semantic_relationship_unverified`; it does not certify that causal sentence.

**Action:** retain rejection. No equivalence between descriptive statements, prescribing advice or causal explanations was invented. A reviewer must decide whether the causal wording should be rewritten. No automatic text editing was performed.

These distinctions are engineering findings about fictional source/candidate pairs, not clinician adjudication. The remaining rejections are conservatively retained implementation boundaries, not assertions that every disputed sentence is clinically false.

## 3. Exact implementation changes

| File | Change |
|---|---|
| `src/chunk_validator.py` — existing file modified | `validate_chunks` dispatches to a separate private editorial grounding wrapper only for the existing `editorial=True` opt-in. `_validate_source_grounding` retains its signature and delegates to the shared original logic with the new option disabled. New `_validate_grounding` retains the existing checks and ordering; the narrow exception is considered only after positive/negative and recommendation-negation hard checks. |
| `src/editorial_grounding.py` — new | `recommendation_metadata_equivalent` recognizes two complete grammatical forms retaining exactly the same recommendation strength and evidence certainty from one complete selected source sentence. |
| `tests/test_phase_a1_editorial_fidelity.py` — new | 88 deterministic cases, including frozen legacy report comparisons, unchanged corpus outcomes, narrow-equivalence adversaries, review boundaries and checkpoint reuse across validator revisions. |
| `tests/fixtures/phase_a1_regression_contract.json` — new | Metadata-only pre-change corpus/prompt hashes, 31 full legacy-report hashes, original editorial statuses and adversarial reason expectations. These are regression labels, not clinical gold labels. |
| `tools/evaluate_phase_a1.py` — new | Safe synthetic comparison runner; writes only below `tmp/phase_a1`, with rule/status metrics and no source passages. |
| This report — new | Investigation, outcomes, compatibility impact and remaining work. |

The recognized sentence has the same nominal subject (`the recommendation`), strength (`conditional`, `weak` or `strong`) and certainty (`very low`, `low`, `moderate` or `high`) in both forms. Only `is`/`remains` and `with`/`and is based on` connectors vary. Full matching rejects added or dropped populations, indications, outcomes, exceptions, numbers, negation and other advice. Attributes cannot be assembled from separate source sentences. No synonym graph, proximity inference, model-generated claim mapping or lexical score proves a clinical relationship.

The new standard issue uses **severity `WARNING`, rule `EDITORIAL_GROUNDING_REVIEW`, category `REVIEW_REQUIRED`**. It adds no payload fields or approval state. The editorial generator continues sanitizing exported issue messages. This rule is a limited grammatical consistency check; clinical attribution, applicability and the surrounding claim still require independent review.

No existing public function signature, default, prompt, source hash algorithm, schema, validation threshold, request setting, retry policy or approval gate changed. All existing validator function parameter lists were also compared through AST and remain identical.

## 4. Preserved validation and review boundaries

Numeric validation still runs before grounding, including visual/native numeric safeguards. Positive-versus-negative and recommendation-negation checks precede the new exception. Existing citation authority, technical schema, five fields, independence checks, duplicate checks, source ownership and independent claim assessment remain active.

The equivalence does not recognize a directive or treatment recommendation. It cannot authorize a changed strength/certainty, infer a population, join evidence across sentences, or approve a chunk. Missing owned evidence cannot be supplied by matching context. Citation mismatches still raise `REFERENCE_OR_CONTENT_INVALID`. Candidate text is not rewritten. Every editorial result remains REVIEW_REQUIRED or REJECTED, with clinical approval, qualified clinical review and document completeness false.

## 5. Original eight examples: before and after

| Case | Before | After | Grounding hard errors before → after |
|---|---|---|---|
| randomized_primary | REVIEW_REQUIRED | REVIEW_REQUIRED | 0 → 0 |
| review_uncertainty | REJECTED | REJECTED | 1 → 1 |
| conditional_guideline | REJECTED | REJECTED | 2 → 1 |
| procedure_sequence | REVIEW_REQUIRED | REVIEW_REQUIRED | 0 → 0 |
| negative_result | REVIEW_REQUIRED | REVIEW_REQUIRED | 0 → 0 |
| noninferiority | REVIEW_REQUIRED | REVIEW_REQUIRED | 0 → 0 |
| adverse_events | REVIEW_REQUIRED | REVIEW_REQUIRED | 0 → 0 |
| uncertain_dose | REJECTED | REJECTED | 1 → 1 |

The existing evaluation JSON is identical before/after: **5 review-required / 3 rejected**, 31 mock calls, identical claim counts/reasons and no automatic PASS. That evaluator records unique error rule names, so it does not show a reduction from two errors to one under the same rule; the new comparison runner measures those counts separately.

A **separate controlled test**, outside the frozen eight examples, substitutes only the metadata grammar into the otherwise literal guideline source. Legacy validation still reports its recommendation traceability hard error; editorial validation returns **REVIEW_REQUIRED** with one `EDITORIAL_GROUNDING_REVIEW` warning. This demonstrates a supported wording change can reach review, without claiming the original guideline example now passes or changing its labels.

## 6. Fifteen adversaries: before and after

R = REJECTED; H = REVIEW_REQUIRED. Every row retains its original status and expected reason. No previously rejected adversary becomes review-required.

| Source case / mutation | Before → after | Expected reason retained |
|---|---|---|
| randomized_primary / swap_arms | R → R | explicit_arm_quantity_conflict |
| randomized_primary / invent_statistic | R → R | numeric_value_unavailable |
| randomized_primary / significance_reversal | R → R | significance_polarity_requires_source_review |
| randomized_primary / changed_timepoint | R → R | numeric_value_unavailable |
| randomized_primary / swap_endpoint | H → H | outcome_preservation_unverified |
| randomized_primary / swap_population | H → H | population_preservation_unverified |
| review_uncertainty / invent_sample | R → R | numeric_value_unavailable |
| conditional_guideline / strong_recommendation | R → R | recommendation_strength_increased |
| conditional_guideline / certainty_inflation | R → R | evidence_certainty_increased |
| procedure_sequence / invent_procedural_rate | R → R | numeric_value_unavailable |
| negative_result / negative_to_positive | H → H | significance_polarity_requires_source_review |
| noninferiority / noninferiority_to_superiority | R → R | noninferiority_or_unassessed_superiority_strengthened |
| adverse_events / swap_safety_arms | R → R | explicit_arm_quantity_conflict |
| adverse_events / deny_events | H → H | adverse_event_negation_requires_review |
| uncertain_dose / alter_dose_unit | R → R | explicit_quantity_unit_or_sign_conflict |

Totals remain **11 rejected, 4 held for review, 15/15 expected reasons**. In particular, population/endpoint substitutions, one significance reversal and adverse-event denial are not all deterministic hard rejections in the current prototype. Their review findings are preserved; this is a clinical reliability limitation, not a claim of complete adversarial detection. A lexical match never makes them clinically approved.

## 7. Tests actually executed

Inspected tests/fixtures and the autouse guards before execution. `tests/conftest.py` redirects local runtime paths and captured defaults to temporary directories/databases and rejects live Azure client creation without an explicit fake client factory. Browser integration tests use ephemeral local test servers. Synthetic PDF fixtures are permitted; user PDFs are not.

Full safe selection, used unchanged for baseline and final regression runs:

```powershell
python -B -m pytest tests/ --ignore=tests/test_native_layout_real_source.py --ignore=tests/test_task11_regression.py -k 'not both_protected_real_anchors' -q -p no:cacheprovider --tb=short -o junit_family=xunit1 --basetemp=tmp/phase_a1/final-tests --junitxml=tmp/phase_a1/final-test-results.xml
```

For the baseline, the last two paths were respectively `tmp/phase_a1/baseline-tests` and `tmp/phase_a1/baseline-test-results.xml`.

| Execution | Actual result | Time reported by pytest |
|---|---|---|
| Pre-change safe full selection | **703 passed; 2 deselected; 9 subtests passed** | 52.66 s |
| First focused run: new Phase A1 tests plus existing M4 tests | **136 passed; 2 failed**, both in new tests; all existing M4 tests passed | 4.01 s |
| Corrected focused run, same two test files | **138 passed** | 4.17 s |
| Post-change safe full selection | **790 passed; 2 deselected; 9 subtests passed** | 57.03 s |
| Additional isolated cross-validator-version checkpoint test, added after full run | **1 passed** | 0.89 s |

There were **zero runtime skips** in these runs. Two user-PDF modules were excluded before collection as shown above, and two `both_protected_real_anchors` cases were deselected. No collection-wide test total is claimed for the excluded modules. Paid benchmarks, Azure connectivity/capability tests, clinician assessment and production integration were **not executed**.

The first focused failures were inaccurate new test expectations: negation reversal is already a hard error, but the legacy message `conflicts` maps to `SOURCE_EVIDENCE_MISSING`, not the assumed contradiction category; and `as described above` is not in the existing independence phrase list. Tests were corrected to assert the exact negation hard finding and a supported independence marker (`as mentioned above`). Existing tests, category logic and phrase detection were not weakened or changed. Both legacy diagnostic/detection limitations remain documented.

Focused command used the two files `tests/test_phase_a1_editorial_fidelity.py tests/test_milestone4_editorial.py` with the same safe pytest flags, isolated directories `focused-tests`/`focused-final-tests`, and separate XML results. The additional test was executed explicitly:

```powershell
python -B -m pytest tests/test_phase_a1_editorial_fidelity.py::test_successful_legacy_candidate_reuses_checkpoint_across_validation_version_change -q -p no:cacheprovider --tb=short --basetemp=tmp/phase_a1/checkpoint-tests --junitxml=tmp/phase_a1/checkpoint-test-results.xml
python -B tools/evaluate_phase_a1.py
```

The comparison runner measured **0.6233 s**, **0.6088 s** and **0.6035 s** across its initial, repeated and final verified runs, for the synthetic comparison and separate controlled example: **31/31 complete legacy reports equal**, **31/31 editorial statuses equal**, corpus and prompt hashes unchanged. Its original evaluation uses 31 mock calls; its separate control uses one additional mock call. Tokens are not available from these fake responses. This is not an Azure performance measurement. The full-suite wall times are not a controlled throughput comparison because 87 new tests were included in the final full run. Final comparison evidence is `tmp/phase_a1/verified-final-comparison.json`.

New coverage includes faithful metadata paraphrasing, swapped arms, numeric/time-point changes, population/subgroup and endpoint preservation, literal recommendation negation reversal, recommendation strengthening, certainty changes, unsupported equivalence, omitted qualifications, missing owned evidence, citation mismatch, context-dependent writing, sanitized diagnostics, unchanged candidate text and mandatory review. The 31 report-hash tests check full legacy reports, including ordered messages, source-evidence offsets, counts and decisions. All 31 editorial comparisons also assert input evidence/plan immutability. The final additional test uses a newly created synthetic PDF and temporary checkpoint directory, changes only the simulated validator revision, then proves byte-identical checkpoint files and output with **zero provider calls on resume**.

## 8. Source provenance and checkpoint impact

Canonical evidence/source buffer algorithms, PDF/source identities, exact spans, reviewed plan requirements and source ownership are unchanged. The new helper reads only the already selected packet. It does not persist or amend source evidence. No LLM assertion becomes a review attestation.

One existing protected file changed: `src/chunk_validator.py`. The other **89/90 baseline files are byte-identical**, including all three prompts, original evaluation corpus, package scheduling, provider/retry configuration, extraction, numeric assessment, source planning, queue, storage and prior reports. `tmp/phase_a1/compatibility.json` and `validator.diff` record this comparison.

| Hash | Before | After |
|---|---|---|
| `chunk_validator.py` SHA-256 | `990eec759fa0d65bf107b5a77454df03c4325c42494d06a761cfd4199b67fae3` | `7279dfbc2c59e73666d2e6e75b0cc83c0d9942c9d7ee0479230865825ee83e6a` |
| Broad package validation fingerprint | `26ba50cad3f7d2fc811da7b0114d6d626d6df24f6b370f81516a9c0a71d6993d` | `79d1298b9b4b56a629f33c3c7581cb0c49719d7715cad135c84be7ffbc8d8b13` |

**Operational consequence:** `package_generation.validation_fingerprint()` hashes every Python file below `src/`. Both the authorized validator edit and new helper therefore change it, including for the default extractive path. An old saved validation hold no longer satisfies the exact current-revision equality check. On a future explicitly authorized run it will be reconsidered; without a compatible candidate this can lead to regeneration. This effect is disclosed, not avoided by changing the fingerprint algorithm. No actual historical hold or checkpoint was read, migrated or rewritten.

Successful candidate identities remain bound to the unchanged source/prompt/provider configuration, independently of that validation-hold revision. Compatible successful candidates can still resume and undergo validation without a provider call, as the additional isolated test demonstrates. Source-abstention hold identity and preparation-cache key inputs/formats are unchanged. Preparation cache module hashes do not include this validator/helper. No runtime default, checkpoint format or approval semantics changed. A separate cache/fingerprint policy redesign would require authorization; none is included here.

## 9. Backward Compatibility and Regression Report

- **Existing files modified:** only `src/chunk_validator.py`; its existing signatures/defaults are unchanged. No report from M1–5 or 6B was edited.
- **New files:** helper, 88-case test module, metadata-only regression fixture, offline comparison tool and this report, as listed above. Additional evidence/scripts are isolated below `tmp/phase_a1/`.
- **Baseline/final evidence:** 703 prior tests plus 9 subtests before changes; final full run 790 tests plus 9 subtests; one additional new checkpoint test passed separately. No existing regression test failed. The two initial failures were new test expectations and are reported explicitly.
- **Invariants:** full legacy validation-report equality for all 31 variants; corpus statuses/expected adversarial reasons retained; exact five-field content/envelope; evidence and plans unchanged; source/citation checks, scheduling limits, retry handling, checkpoint reuse, isolated storage/readback and approval boundaries covered by the final safe suite. The immutable successful-checkpoint test also checks output equality and zero-network resume across a validation revision change.
- **Intentional behavior change:** only the complete recommendation metadata grammatical equivalence reaches mandatory editorial review rather than the lexical traceability error. It neither resolves the remaining sentence-level cases nor approves candidates. Package validation-hold fingerprint invalidation is the existing mechanism's consequential behavior, disclosed above.
- **Performance:** no production throughput or Azure speedup claim. Scheduling, retries and cache implementation are unchanged; the safe regression suite verifies their existing behaviors.
- **Protected runtime state:** queue not started, resumed or modified; no production persistence, database/schema, user PDF, historical output/checkpoint, `.env`, model setting or publishing gate was changed. No user PDF, live clinical database or historical source cache/output/checkpoint was inspected for this task. Temporary offline test runtimes only. The paused queue remains untouched; it was not inspected through state-mutating status helpers.
- **Rollback:** preserve this report/evidence, restore only `src/chunk_validator.py` from `tmp/phase_a1/baseline/source/src/chunk_validator.py`, and remove the new `src/editorial_grounding.py` import target together with that restoration. Remove or retain the new test/tool/fixture/report as a matched unit. Do not bulk-restore snapshots over subsequent user changes. Verify restored hashes and rerun the baseline safe selection. No database migration or checkpoint rollback is needed. Removing both source changes restores the original broad validation fingerprint only if no other `src` files have changed since baseline.
- **Remaining risks:** narrow grammar coverage, attribution and causal ambiguity, non-exhaustive contradiction detection, legacy category/independence limitations, broad fingerprint invalidation, and no qualified clinical or live-provider acceptance evidence.

## 10. Remaining clinical acceptance and Phase A2 recommendation

Milestone 4 still needs qualified source/candidate adjudication, representative clinician-reviewed examples, broader relationship-aware fidelity assessment, editorial-quality evaluation and an authorized provider evaluation if desired. Synthetic regression safety is not clinical approval. The original three rejected examples remain unresolved at chunk level; four existing adversaries remain review cases rather than deterministic hard rejections. Milestone 5's fresh-PDF and clinical acceptance limitations also remain unchanged.

Recommended **Phase A2 scope for separate authorization/review only**: introduce isolated automatic concept *proposals* bound to exact canonical spans, source regions and evidence identities. Mark them as proposals awaiting independent source review; do not create `human_source_review` attestations, automatically bind ambiguous arms/populations/endpoints, mark evidence covered from model output alone, or promote proposals to the queue's default generation route. Evaluate missing/ambiguous evidence, contextual dependencies, source changes, tables/figures and explicit exclusions on synthetic fixtures first. Keep A2 outside the production knowledge architecture and approval boundary. No A2 implementation has started.

No Phase 6C, live Azure generation, paid evaluation, user-PDF processing, automatic publishing or production ingestion was performed. Work stops for independent review.

**PARTIAL — Phase A1 engineering: one verified grammatical false rejection corrected with legacy behavior preserved; three original chunks remain rejected and broader editorial/clinical relationships still require adjudication.**

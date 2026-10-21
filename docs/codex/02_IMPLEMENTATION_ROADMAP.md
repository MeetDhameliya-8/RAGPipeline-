# GastroAGI TASK11 — Incremental Repair Roadmap

**Purpose:** Guide Codex through small, verifiable repairs to the existing clinical PDF ingestion pipeline.

**Implementation policy:** Fix the known failure boundaries first. Prefer existing modules and interfaces. Preserve the fail-closed architecture.

## Milestone 0 — Baseline Verification

**Mode:** Read-only investigation, followed by isolated test additions as agreed.

### Tasks

1. Inspect relevant source files, test fixtures, prompts, and dependency versions.
2. Record the baseline output of the existing unit test suite.
3. Reproduce a blank-reference generation failure using a mocked provider.
4. Reproduce the numerical validator's behavior using the ABC-02 evidence.
5. Run extraction-only checks against the five PDF fixtures when available locally.
6. Trace the reported zero chunk counts to their data source.
7. Check checkpoint behavior when a rejected chunk has a source-evidence match.

### Deliverable

A reproducible baseline report containing exact file/function findings, failing test cases, and proposed minimal changes.

**Exit:** The root causes have reproducible evidence, or the unresolved diagnostic questions are clearly identified.

---

## Milestone 1 — Repair Source Citation Binding

**Priority:** P1  
**Files:** `src/pdf_extractor.py`, `src/chunk_generator.py`, relevant generator/extractor tests.

### Required behavior

1. Establish the article-level citation before generating clinical chunks.
2. Distinguish a citation candidate extracted by regex from a verified source identity.
3. Cross-check bibliographic details against the actual document, including title, journal, year, volume, pages, and DOI where available.
4. Reject conflicting source identity rather than selecting the most convenient citation.
5. Attach the verified article citation deterministically to generated chunks that lack it.
6. If the model returns a conflicting citation, flag it rather than silently overwriting it.
7. Preserve strict final Pydantic validation of the five-field chunk contract.

### Implementation guidance

Inspect `generate_chunks()`, `validate_generation_schema()`, `normalize_generation_result()`, and `validate_generation_content()`.

The present order validates the schema before normalized reference enrichment is possible.

Introduce a narrow pre-final-validation stage that validates the basic response shape, canonicalizes supported aliases, and binds an authoritative citation.

Do not weaken validation of non-reference clinical fields. Do not fabricate an article reference when verification fails.

Do not add a sixth user-facing chunk field. Carry provenance and metadata separately.

### Exit criteria

- An otherwise valid chunk with a missing/blank reference receives the correct verified citation.
- Conflicting DOI or bibliographic identity is rejected.
- Missing unverified reference remains blocked.
- Existing schema and fabricated-reference tests continue to protect the output.
- No unsupported clinical text is accepted.

---

## Milestone 2 — Repair Numeric Evidence Verification

**Priority:** P1  
**Files:** `src/pdf_extractor.py`, `src/chunk_generator.py`, `src/chunk_validator.py`, associated tests.

### Required behavior

1. Preserve raw page text before cleaning and maintain page identifiers.
2. Compare the extracted source representations for critical numerical evidence.
3. Identify where a value disappears or changes.
4. Recognize meaning-preserving formatting variations where supported.
5. Match numeric findings within their local clinical context.
6. Preserve endpoint, comparator, intervention arm, population, unit, and timeframe.
7. Classify unmatched evidence as `UNSUPPORTED`, `EXTRACTION_INCOMPLETE`, or `REQUIRES_REVIEW` using the repository's actual status conventions, or introduce documented equivalents if needed.
8. Block clinical approval whenever a critical numeric association remains unsupported.

### Specific regression target

In `NEJMoa0908721.pdf`, check PDF page 4 / journal page 1276.

The source reports 66.5% for three cycles of gemcitabine alone and 73.5% for four cycles of cisplatin plus gemcitabine during the first 12 weeks.

If raw extraction contains these values but cleaned extraction does not, inspect cleaning.

If the extractor cannot capture them reliably, evaluate documented layout-aware extraction techniques before replacing the existing extractor.

If the extracted source contains the values, inspect the exact validator input and its normalization behavior.

### Safety rule

A numeric value appearing anywhere in a document is not enough. The same value can belong to a different endpoint or treatment group.

Likewise, failure to find a number in cleaned text is not proof that the model hallucinated it.

### Exit criteria

- Both real ABC-02 values are traceable in the correct context.
- Intentionally invented numbers remain blocked.
- Reversed treatment-group assignments are not accepted.
- Missing table/figure evidence causes review or quarantine.
- Existing numerical tests continue to pass.

---

## Milestone 3 — Repair Source Checkpoint Integrity

**Priority:** P0 for checkpoint correctness  
**Files:** `src/source_map.py`, `src/pipeline.py`, `tests/test_source_map.py`, `tests/test_pipeline_helpers.py`.

### Required behavior

1. Inspect `mark_processed_sections_from_validation()`.
2. Relate each source-evidence record to its actual chunk validation status.
3. Mark sections `processed` only when appropriate validated evidence supports completion.
4. Never mark a section processed solely because rejected generated text had a source match.
5. Preserve deferred table/figure sections and their review reasons.
6. Invalidate checkpoints when relevant source, configuration, or validation semantics change.
7. Ensure repeated processing does not incorrectly skip clinically material source sections.

### Critical test

Create a report where a rejected chunk references a real source section.

**Expected:** The section is not marked successfully processed.

Also test a passing chunk, a missing chunk status, multiple chunks linked to one section, and resumed processing after a corrected source.

### Exit criteria

Checkpoint states accurately reflect verified processing, not merely candidate generation.

---

## Milestone 4 — Preserve Candidate Results and Improve Recovery

**Priority:** P1/P2  
**Files:** `src/chunk_generator.py`, `src/pipeline.py`, `src/local_rag_chat.py`, relevant tests.

### Required behavior

1. Separate raw provider response, parsed candidate, normalized candidate, validated candidate, and approved content.
2. Track candidate counts before clinical validation decides the job outcome.
3. Persist safe diagnostic artifacts without leaking credentials or confidential content.
4. Preserve the existing validated/quarantined subset behavior where compatible.
5. Prevent one rejected chunk from erasing the existence and status of otherwise valid candidates.
6. Avoid marking a partial document complete when source coverage remains unresolved.
7. Retry only the relevant failed work when reliable checkpoints exist.
8. Use bounded repair attempts with clear exhaustion behavior.

### Suggested logical status distinctions

- Provider completed / failed.
- JSON parsed / malformed.
- Schema valid / invalid.
- Source supported / unsupported / uncertain.
- Human review pending / approved / rejected.
- Indexed candidate / active / failed.

Use existing status names when they already carry the correct semantics. Do not rename public response contracts without migration analysis.

### Admin Panel counters

Report separately:

- Candidate chunks generated.
- Structurally valid chunks.
- Source-supported chunks.
- Rejected or review-pending chunks.
- Locally stored chunks.
- Production-published chunks, if and when real production integration exists.

Do not count stored draft chunks as clinically approved.

### Exit criteria

A failure in Chunk 5 is visible as a chunk-specific failure, with the correct candidate count and no unsafe downstream publishing.

---

## Milestone 5 — Activate Section-Based Generation

**Priority:** P2, after immediate failures are resolved.

**Files:** `src/source_map.py`, `src/pipeline.py`, `src/chunk_generator.py`.

### Tasks

1. Reuse the existing section-mapping and `section_batches()` infrastructure.
2. Plan bounded generation units from verified clinical sections.
3. Preserve sufficient neighboring context to avoid fragmenting clinical claims.
4. Keep stable source span/page identifiers.
5. Detect missing, deferred, and duplicated section coverage.
6. Track every section as processed, pending, deferred, excluded, or failed with an explicit reason.
7. Persist checkpoints for safe replay without duplicate chunks.
8. Reconcile coverage before declaring an article reviewable or complete.

Do not assume a fixed number of chunks for every paper.

Do not treat deferred clinically material tables as completed coverage.

### Exit criteria

All important source sections have evidence-linked coverage or explicitly documented, reviewable exclusions.

---

## Milestone 6 — Retrieval and Backend Discovery

**Priority:** Later milestone, not part of the immediate failure fix.

### Tasks

1. Inspect the real GastroAGI backend repository.
2. Verify existing ingestion API/authentication and payload schemas.
3. Verify embedding configuration, dimensions, vector schema, and retrieval filtering.
4. Determine whether candidate-version indexing and activation are already supported.
5. Implement an adapter only after the real contract is established.
6. Test staging ingestion, readback, idempotency, retrieval quality, and rollback.

**Prohibited:** Inventing an API route or writing directly to production ChromaDB because a local test passes.

### Exit criteria

Only explicitly approved records can become searchable, and retrieved evidence matches the exact ingested source.

---

## Architecture Decision Gate: Additional Agents

Do not introduce LangGraph or additional autonomous LLM agents during Milestones 0–4 unless existing code demonstrates a clear, independently testable need.

An additional model-assisted verifier may be considered later if deterministic matching cannot adjudicate complex clinical paraphrases, provided it:

- Uses exact source evidence.
- Does not invent missing content.
- Cannot independently approve its own unsupported claims.
- Produces auditable evidence references.
- Demonstrates measurable improvement on held-out benchmarks.

Prefer simple Python orchestration with durable checkpoints until additional complexity is justified.

## Completion Rules

A milestone is complete only when:

1. Its exact regression tests have been executed.
2. Existing relevant tests still pass.
3. Clinical evidence is independently checked against the original source.
4. Failure modes remain fail-closed.
5. The changed files and test results are documented.
6. No unauthorized Azure or production action occurred.
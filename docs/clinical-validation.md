# Clinical Validation

## Verified Current Behavior

Every generated GastroAGI chunk must preserve exactly five clinical content fields:

- Topic
- Subtopic
- Section
- Reference
- Original Text

`chunk_number` is pipeline metadata only.

Validation has three layers:

1. Deterministic technical validation with Pydantic and local validation helpers.
2. Source-grounding heuristics against extracted source text and available page evidence.
3. Human review through `chunks.md`, `chunks.json`, `validation_report.json`, and `review_package.md`.

Technical validation checks required fields, invalid types, empty values, extra fields, invalid references, word-length expectations, output completeness, and duplicate chunks.

Raw generation types, allowed keys and declared counts are checked before normalization. Null/list/object/numeric clinical fields are rejected, not converted to strings. Field-name aliases and source-supported reference binding are controlled exceptions; duplicate JSON keys, alias collisions and invalid operational chunk numbers reject.

Numeric validation now examines local source passages. Suspected glyph/superscript ambiguity is `EVIDENCE_QUALITY` review; an absent value fails. An explicit sign/unit/operator conflict in an aligned proposition fails; quantity/arm/endpoint/timeframe associations that cannot be established remain review-required. Non-exact lexical support is not relational verification. Contradiction checks distinguish supported endpoint/scope/named-arm differences, and recommendation negation cannot be reversed. Validation continues after early warnings so later failures are visible.

Citation candidates carry raw-page spans and source-supported/unresolved/conflict identity. Conflicting identity blocks reference binding; DOI presence cannot validate unrelated bibliographic details. This is source attribution, not external bibliographic verification.

Source-grounding checks flag numbers not found in extracted text, weakly supported clinical claims, recommendation language without close source support, possible contradictions, missing qualifications, population terms not found in source text, and source evidence availability. When a source map is available, source evidence can also include internal document ID, source file, section ID, section title, and page range metadata.

## Implemented But Not Proof Of Accuracy

The source-grounding layer is deterministic and conservative. It can identify risk, but it is not proof that a clinical statement is correct. A clinician or designated reviewer must approve generated chunks before Admin Panel upload, vector indexing, or production ingestion.

`source_supported` accounting means deterministic chunk PASS only. REVIEW_REQUIRED, structural validity and local storage never count as clinical approval. Full-text evidence spans identify their source buffer hash; a canonical raw/clean/page transformation map remains P1 work. See the [P0 implementation review](engineering/task11-p0-implementation-review-2026-10-16.md) for remaining clinical limits and protected-corpus results.

## Planned Future Improvements

- Use the source map for controlled section-by-section Azure generation.
- Add stronger source-grounding validation over each substantive clinical statement.
- Optional model-assisted review that cites source evidence and never overwrites deterministic failures.

## Blocking Conditions

Production upload, indexing, and ingestion must remain blocked when generation fails, validation status is `FAILED`, source extraction is materially degraded, references are placeholders, source evidence is missing for important claims, or human approval has not been recorded.





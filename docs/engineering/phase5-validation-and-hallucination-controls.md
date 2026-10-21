# Phase 5 Validation And Hallucination Controls

Phase 5 separates validation into three distinct layers. Each layer has a different purpose and failure mode. JSON validity is only a technical gate; it is not evidence that the clinical content is accurate.

## Layer 1: Deterministic Technical Validation

Layer 1 validates the generated package as data. It should run before any clinical interpretation, review packaging, indexing, or Admin Panel automation.

Implementation boundary:

- Use Pydantic models for the top-level article object, chunk objects, field types, and extra-field rejection.
- Use existing deterministic utilities in `src.chunk_validator` for empty values, word count, duplicate detection, reference checks, and completeness checks.
- Keep `chunk_number` as optional pipeline metadata. The five content fields remain Topic, Subtopic, Section, Reference, and Original Text.

Layer 1 checks:

- valid JSON before validation reaches `validate_chunks`
- top-level object shape
- exact required article fields
- chunk content fields plus optional `chunk_number`
- valid field types
- empty values
- word-length expectations
- duplicate chunks and duplicate sections
- invalid or placeholder references
- output completeness, including non-empty chunks and matching `number_of_chunks`

Layer 1 failure should produce `status: FAILED` and block upload, indexing, and production ingestion. It does not prove source fidelity; it only proves the generated package is structurally reviewable.

## Layer 2: Source-Grounded Clinical Claim Validation

Layer 2 validates clinical statements against extracted source passages. It treats the source PDF and extracted text as the only source of truth, and it reports uncertainty rather than repairing clinical content.

Implemented deterministic checks include:

- numerical source matching for statistics, doses, time points, and percentages
- warning-level detection of clinical claim sentences that are not closely supported by extracted source text
- error-level detection of recommendation language that is not traceable to a closely matching source passage
- error-level detection of positive or significant findings that contradict source language such as `not statistically significant`, `no difference`, or `did not differ`
- warning-level detection of population descriptors that are not present in the source text
- warning-level detection of strong claims that may omit source qualifications, limitations, or uncertainty
- extraction artifact detection, reference-list leakage, author-affiliation leakage, generic classification detection, abbreviation checks, chunk independence checks, and duplicate/reference consistency checks

Layer 2 now also adds `source_evidence` metadata to the validation report when `source_text` is supplied. This metadata is not part of the Admin Panel content fields. It may include exact-match page numbers, source section identifiers, and source text spans when they are reliably available. If exact provenance cannot be found, the validator reports `match_type: not_found` instead of inventing a span.

Future source-grounding improvements should add a richer Stage B source map. Each chunk should identify supporting source section IDs or spans from that map. The validator should then check:

- every dose, statistic, time point, outcome, recommendation, population, comparator, and bibliographic detail is traceable to the cited source passage
- unsupported numbers or claims are flagged rather than repaired
- limitations and uncertainty in the source are preserved
- cautious recommendations are not strengthened
- unrelated source sections are not merged into one chunk
- missed major clinical domains are flagged for reviewer attention when section-level provenance shows uncovered source material

An optional second-model review may assist Layer 2, but it must not be treated as independent proof of correctness. Model-assisted validation should return evidence-linked findings for human review, not overwrite clinical content or silence deterministic failures.

## Layer 3: Human Review And Release Gate

Layer 3 is the publication gate. Generated chunks are drafts until reviewed by a clinician or designated medical reviewer.

Layer 3 checks:

- review of `chunks.md` for clinical usefulness and independent readability
- review of `chunks.json` for the exact Admin Panel fields
- review of `validation_report.json` for all Layer 1 and Layer 2 issues
- review of `review_package.md`, which shows the original source reference, generated chunk content, source evidence, validation status, warnings, and rejection reasons
- confirmation that source extraction quality is acceptable
- confirmation that no failed validation output is uploaded, indexed, or treated as production-ready
- explicit human approval before Admin Panel upload, vector indexing, or production ingestion
- explicit human action for final Admin Panel Upload

The validation report includes a `human_review` gate with `approval_required: true`, `approved: false`, and `ingestion_allowed: false` by default. The Admin Panel helper may populate fields after review, but it must not click final Upload automatically. RAG indexing must remain downstream of passing validation and explicit human approval.

## Failure Policy

- Invalid JSON or malformed schema: fail Layer 1.
- Empty chunks, missing fields, invalid types, placeholder references, or incomplete output: fail Layer 1.
- Unsupported clinical numbers or claims: fail Layer 2 or require review when uncertainty remains.
- Any `FAILED` validation status: block Admin Panel upload, vector indexing, and production ingestion.
- Any warning status: require human review before publication.



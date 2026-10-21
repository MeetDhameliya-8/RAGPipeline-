# Phase 4 Clinical Knowledge Generation Strategy

This design extends the existing PDF-to-chunk pipeline without changing the GastroAGI Admin Panel content contract.

## Required Content Contract

Each reviewable GastroAGI chunk must contain exactly five content fields:

- Topic
- Subtopic
- Section
- Reference
- Original Text

`chunk_number` remains pipeline metadata for ordering and review only. It is not an Admin Panel content field and should not be treated as a RAG payload field unless a later schema explicitly adds it.

## Clinical Chunk Target

Each chunk should represent one clinically coherent concept and normally contain 150-250 words in `Original Text`. This is a target range, not permission to pad or truncate. A shorter chunk may be acceptable when the source contains a concise point, and a longer chunk may be needed when population, intervention, comparator, outcome, uncertainty, and limitations must stay together.

Each chunk should be independently understandable, preserve the source meaning, include relevant population/intervention/comparison/outcome/limitations when appropriate, avoid invented findings or recommendations, avoid combining unrelated sections, avoid unnecessary duplication, and preserve qualifications or scientific uncertainty.

## Stage A: Extract Text And Preserve Source Provenance

Use `src.pdf_extractor` as the extraction boundary. The extraction output should retain:

- source file name
- page count
- raw page text
- cleaned per-page text
- cleaned full article text
- detected citation metadata
- extraction warnings

The source PDF and extracted text remain the source of truth. Do not silently truncate source material before generation. If extraction is empty, low density, degraded, or missing pages, the pipeline should fail or produce a validation report that blocks production use.

## Stage B: Identify Section Structure And Candidate Boundaries

Before chunk generation, identify candidate semantic boundaries from extracted text rather than falling back to arbitrary fixed-size splitting. Candidate boundaries may include article headings, abstract subsections, methods/results/discussion blocks, guideline recommendation blocks, tables when extraction is reliable, and clinically coherent paragraphs.

For long PDFs, prefer controlled section-level processing:

- Build a source map of candidate sections with stable IDs, heading text, page span when available, character span, and text.
- Send complete candidate sections or bounded groups of adjacent sections to the model.
- Keep enough neighboring context to preserve study design, population, comparator, outcomes, and limitations.
- Reject a section batch if it would require silent truncation.
- Record which candidate section IDs were used for each generated chunk.

If section detection is uncertain, flag uncertainty for review. Do not use arbitrary text-splitting as a clinical fallback when LLM generation fails.

## Stage C: Generate Clinically Independent Chunks

Use the prompt in `prompts/gastroagi_chunk_rules.md` as the generation contract. Generation should produce chunks that:

- use specific clinical Topic and Subtopic values
- use Section as the searchable title of the chunk
- preserve the most complete verified Reference available
- keep `Original Text` faithful to supplied source passages
- include relevant population, intervention, comparator, outcome, adverse events, applicability, and limitations when the source supports them
- distinguish primary from secondary outcomes, statistical significance from numerical trends, association from causation, and cautious recommendations from strong recommendations

For long PDFs processed section by section, generation should be followed by a reconciliation pass that removes duplicates, checks for missed major clinical domains, and confirms no unrelated sections were merged.

## Stage D: Validate Output Structure

Run deterministic schema validation before clinical validation. JSON validity alone is not clinical accuracy.

The current generator validates:

- top-level `article` object
- top-level `chunks` list
- required chunk fields
- forbidden extra fields through the Pydantic generation model
- normalized field casing

Validation should fail closed when required fields are absent, when the model returns malformed output, or when the output shape cannot be normalized without hiding contract violations.

## Stage E: Check Clinical Claims Against Source Passages

Run deterministic validation in `src.chunk_validator` and treat the result as a clinical review gate. The validator should continue to check:

- required fields
- specific Topic/Subtopic values
- Section quality
- target word count
- source-cleaning artifacts
- reference-list leakage
- chunk independence
- unexplained abbreviations
- duplicate chunks
- numerical claims traceable to extracted source text

Future source-grounding improvements should add chunk-to-source support evidence rather than silently repairing claims. If a dose, statistic, time point, outcome, recommendation, or bibliographic detail cannot be traced to the extracted source, validation should flag it for review.

## Stage F: Produce A Reviewable, Auditable Chunk Package

The review package should remain local and human-auditable:

- `chunks.json` for structured review and downstream automation
- `chunks.md` for clinician-readable review
- `validation_report.json` for deterministic status, issues, diagnostics, and extraction/generation metadata

For section-level processing, the package should also include a provenance map that links each chunk to source section IDs and source spans. Until that map is implemented, the pipeline should avoid implying span-level traceability that it does not yet provide.

Failed generation or failed validation must block production ingestion, vector indexing, and Admin Panel upload. Human medical review remains mandatory before publication.

# Clinical Safety Reference

The pipeline handles clinical source material. Reliability and explicit uncertainty matter more than convenience.

## Source Fidelity

Only use the supplied PDF/extracted article text for clinical content. Do not fill missing facts from model memory, web search, or general medical knowledge. If extraction cannot verify a fact, preserve the gap and let validation report it.

## Chunk Contract

Each GastroAGI chunk has exactly five content fields:

- Topic
- Subtopic
- Section
- Reference
- Original Text

`chunk_number` is operational metadata, not clinical content.

## Blocking Conditions

Block production ingestion, vector indexing, or Admin Panel upload when:

- generation fails
- validation status is `FAILED`
- source PDF extraction is empty or materially degraded
- output uses generic document labels as clinical classification
- reference lists, boilerplate, affiliations, or broken extraction artifacts leak into chunk text
- numerical findings or recommendations cannot be traced to extracted source text

## Human Review

All generated chunks are drafts. A clinician must review the generated Markdown, JSON, and validation report before publication or upload.

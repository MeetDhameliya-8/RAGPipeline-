# Phase 8: Testing and Quality Benchmarks

## Purpose

This plan defines how the GastroAGI PDF-to-RAG clinical knowledge pipeline should be tested before it is trusted for clinician review or future ingestion. The core safety rule is unchanged: routine automated tests must use mocked model responses and local fixtures only. They must not make paid API requests, upload clinical chunks, or modify a production database.

Live provider checks and full clinical PDF generation are separate opt-in activities. Full clinical generation that may incur substantial cost requires explicit approval from the operator before it is run.

## Test Boundaries

| Boundary | Routine test mode | Live/opt-in mode | Cost and safety guard |
| --- | --- | --- | --- |
| Provider configuration | Unit tests with patched environment | `python test_azure_connection.py --dry-run` | No inference request in dry run |
| Azure connectivity | Mocked HTTP/SDK responses | `python test_azure_connection.py --max-output-tokens 16` or `python -m src.pipeline --check-llm` | Non-clinical prompt only; token cap on direct diagnostic probe |
| Clinical chunk generation | Mocked model completions | `python -m src.pipeline --file <pdf> --test-generation --force` | Requires explicit approval for substantial cost |
| PDF extraction | Local fixture PDFs | Manual comparison against source PDFs | Never silently truncate source text |
| Validation and review package | Local generated JSON fixtures | Clinician review of `review_package.md` | Human approval required before ingestion |
| Database ingestion | Contract tests with fake backend/client | Backend staging verification only after real contract inspection | No production writes in this task |

## Required Coverage

1. Azure provider configuration
   - Verify `GASTROAGI_LLM_PROVIDER=azure` is required.
   - Verify missing `AZURE_OPENAI_API_KEY`, `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, and `AZURE_OPENAI_API_VERSION` fail closed.
   - Verify endpoints are absolute HTTPS Azure OpenAI resource URLs, not Azure AI Foundry Models endpoints unless a later approved provider contract adds support.
   - Verify configuration reports mask secrets.

2. Authentication failures
   - Mock 401 and 403 responses from Azure SDK and direct diagnostic HTTP calls.
   - Assert errors are sanitized, non-retryable, and include safe request IDs when available.
   - Assert API keys and prompt/source text are never printed.

3. Invalid deployment names
   - Mock 404 deployment-not-found responses.
   - Assert the pipeline reports deployment-name guidance and does not retry.
   - Keep the diagnostic warning that common model names may not be Azure deployment names.

4. Quota and rate-limit errors
   - Mock 429 responses with and without `Retry-After`.
   - Assert generation retry behavior respects bounded retry-after values.
   - Assert connectivity diagnostics classify quota/rate-limit failures distinctly from authentication and schema failures.

5. Network timeouts
   - Mock socket timeouts and SDK timeout exceptions.
   - Assert timeout status, retryability, and sanitized messages.
   - Add coverage for DNS and TLS failures in the direct diagnostic if those paths change.

6. Malformed model output
   - Mock non-JSON, fenced malformed JSON, empty choices, missing message content, and non-object payloads.
   - Assert failures are explicit `ChunkGenerationError` or validation failures.
   - Do not fall back to arbitrary text splitting after an LLM output failure.

7. Schema validation failures
   - Use Pydantic Layer 1 validation and existing validation utilities to reject missing required fields, invalid types, empty values, invalid JSON, bad references, duplicate chunks, and incomplete output.
   - Assert `chunk_number` remains metadata and is not treated as an Admin Panel content field.
   - Assert exactly five content fields are required for each reviewable chunk: `Topic`, `Subtopic`, `Section`, `Reference`, and `Original Text`.

8. Multi-column PDF extraction
   - Maintain fixture PDFs or synthetic PDFs with two-column layout, headings, footnotes, page numbers, tables, and references.
   - Assert extracted text preserves clinically meaningful reading order well enough for source-grounding review.
   - Compare extracted text against a golden text fixture where possible; where exact layout differs, assert required source passages are present and not duplicated excessively.

9. Missing or corrupted pages
   - Use corrupted or partially unreadable PDF fixtures.
   - Assert extraction records page-level warnings and never silently truncates source material.
   - Assert downstream generation is blocked or marked failed when completeness cannot be established.

10. Duplicate chunks
   - Test exact duplicates and near duplicates across generated chunks.
   - Assert duplicate warnings or failures include chunk numbers and repeated section/reference information.
   - Verify duplicate protection remains separate from future database duplicate-document handling.

11. Unsupported clinical claims
   - Use source passages that intentionally omit a statistic, population, outcome, dosing detail, or recommendation claimed by the generated chunk.
   - Assert Layer 2 source-grounding validation flags unsupported findings, fabricated statistics, incorrect populations, misstated recommendations, missing qualifications, and contradictions.
   - Treat optional second-model review as assistance only, never independent proof of correctness.

12. Long-document processing
   - Test controlled section-level processing plans for long PDFs rather than a single blind whole-document request.
   - Assert source coverage accounting reports processed, deferred, and failed sections.
   - Assert no source section is silently dropped when token limits or provider errors occur.

13. Provider compatibility
   - Preserve regression tests that Azure is the active provider and OpenAI/Swan legacy paths do not silently run.
   - Use provider-interface contract tests for any future provider.
   - Require provider-specific request-shape tests before enabling provider-specific parameters in generation.

14. Existing OpenAI and Swan regression tests
   - Keep regression coverage showing `GASTROAGI_LLM_PROVIDER=openai`, Swan, or unknown legacy provider values fail closed unless reintroduced by an approved design.
   - If either provider is intentionally restored later, add isolated mocked tests for configuration, request shape, error mapping, and JSON normalization before any live use.

15. Future database ingestion and retrieval
   - Until the real GastroAGI backend is inspected, use only fake clients and contract placeholders.
   - Future tests must cover stable chunk IDs, idempotency keys, duplicate protection, retry-safe submission, audit logging, partial failures, approval gating, deletion/update propagation, and retrieval verification.
   - Production collections, schemas, routes, authentication, embedding models, and chunk ID rules must be discovered from the real backend before implementation.

## Quality Benchmarks

Technical quality gates:

- `python -m pytest` passes without network access or paid API calls.
- Validation rejects invalid JSON, invalid schema, empty content fields, duplicate chunks, invalid references, and incomplete outputs.
- Generated review packages include original source reference, generated chunk, source evidence, validation status, warnings, and rejection reasons.
- Failure reports are written when generation or validation fails, without moving invalid chunks into an ingestion path.

Clinical quality gates:

- Each accepted draft chunk is clinically coherent, independently understandable, and typically 150-250 words.
- Each chunk preserves the source meaning, qualifications, uncertainty, population, intervention, comparison, outcome, and limitations when applicable.
- Unsupported clinical claims are flagged for human review rather than repaired silently.
- Human approval remains mandatory before Admin Panel upload or future database ingestion.

Long-document quality gates:

- Section-level processing coverage is auditable.
- No page, section, or source passage is silently truncated.
- Token-limit handling produces explicit failures, deferrals, or review warnings.
- Regeneration is retry-safe and does not create duplicate accepted chunks.

## Opt-In Azure Connectivity Check

The direct Azure diagnostic is intentionally separate from ordinary tests and pipeline generation:

```powershell
python test_azure_connection.py --dry-run
python test_azure_connection.py --max-output-tokens 16 --timeout 30
python test_azure_connection.py --max-output-tokens 16 --token-limit-parameter max_tokens --timeout 30
```

The live probe sends a non-clinical prompt and includes a token cap for Azure OpenAI chat completions. It defaults to `max_completion_tokens` for current chat-completions compatibility and supports `--token-limit-parameter max_tokens` for deployments or API versions that require the legacy field. If the configured API version or deployment rejects that request shape, the diagnostic reports `invalid_request_or_api_compatibility`; it must not trigger fallback clinical generation.

The pipeline-level connectivity check remains opt-in:

```powershell
python -m src.pipeline --check-llm
```

Routine test execution must mock provider responses and must not call either live check.

## Cost Approval Rule

Any command that performs full clinical PDF generation against a paid model, especially for long PDFs or batches, requires explicit operator approval before execution. Approval should identify the file or batch, expected mode, and whether outputs may be written. Dry runs, validation-only runs, and mocked tests do not satisfy approval for paid generation.


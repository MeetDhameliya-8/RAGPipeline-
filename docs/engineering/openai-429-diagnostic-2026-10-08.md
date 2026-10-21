# OpenAI HTTP 429 Diagnostic

Date: 2026-10-08.

This diagnostic inspected the OpenAI Python SDK error surface without modifying production generator code, changing models, moving PDFs, uploading data, indexing data, or substituting raw-text chunks.

## Configuration

- Provider: OpenAI
- Configured model: `gpt-4.1`
- API key: configured, not printed
- Full pipeline input size from `input_pdfs/Testing.pdf`: approximately 48453 characters sent to the model, roughly 12113 input tokens by a simple characters divided by four estimate

## SDK Surface

- Installed OpenAI Python SDK: `3.13.0`
- `RateLimitError` inherits from `APIStatusError`
- Local `responses.create` supports `model`, `input`, `temperature`, `text`, and `max_output_tokens`
- The Responses API rejected `max_output_tokens=1` with HTTP 400 because the minimum accepted value is 16 for this request path

## Minimal Non-Clinical Probe

Probe:

- Endpoint path used through SDK: `client.responses.create`
- Input text: non-clinical, 9 characters
- `max_output_tokens`: 16
- SDK retries disabled for the probe with `max_retries=0`

Sanitized outcome:

- Exception class: `RateLimitError`
- HTTP status: `429`
- `error.type`: `billing_not_active`
- `error.code`: `billing_not_active`
- Sanitized `error.message`: `Your account is not active, please check your billing details on our website.`
- Request ID: `req_4ef0f3bbf05d4e9493198c48704778fc`
- `Retry-After`: not present
- Rate-limit headers for request/token limits: not present in the captured response

## Interpretation

This is a billing/account activation problem, not a temporary requests-per-minute or tokens-per-minute rate limit. Exponential backoff is not the appropriate next action for this specific error.

Required account action: activate or fix billing for the OpenAI account/project associated with the configured API key, then rerun a minimal non-clinical probe before retrying the full PDF generation.

The pipeline correctly remained fail-closed: no valid clinical chunks were generated, no raw-text fallback was substituted, no database/vector upload occurred, and the source PDF was not moved.

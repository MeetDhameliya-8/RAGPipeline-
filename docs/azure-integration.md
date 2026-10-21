# Azure Integration

## Verified Current Behavior

Azure OpenAI is the only active runtime inference provider selected by `GASTROAGI_LLM_PROVIDER=azure`.

Required environment variables:

- `AZURE_OPENAI_API_KEY`
- `AZURE_OPENAI_ENDPOINT`
- `AZURE_OPENAI_DEPLOYMENT`
- `AZURE_OPENAI_API_VERSION`

The endpoint must be an HTTPS Azure OpenAI resource endpoint ending in `.openai.azure.com`. Azure AI Foundry Models endpoints such as `.services.ai.azure.com` and `.inference.ai.azure.com` are rejected by current validation.

The provider adapter in `src.providers.azure` owns:

- environment configuration loading
- endpoint validation
- `AzureOpenAI` SDK client construction
- chat-completions request submission
- bounded transient retries
- sanitized error mapping
- token usage logging without source text or credentials
- opt-in non-clinical connectivity checks

The runtime registry in `src.providers.registry` fails closed for any provider other than Azure.

Nonstreaming ingestion generation now retains a sanitized response envelope outside the clinical payload: configuration/input/prompt fingerprint, response ID, finish reason, usage and transport-attempt count. Missing finish reasons/content, truncated completions, refusals and content-filtered completions reject even if the returned text happens to be JSON. Errors retain typed outcome codes without raw provider bodies or credentials.

Generation diagnostics distinguish planned logical requests, attempted provider calls and per-unit transport attempts (including retries). User-input characters and system-prompt characters are counted separately at the provider-call boundary; they are not proof of remote receipt or token counts. `generated_candidates` counts observed parseable candidates; `candidate_count_complete=false` marks unknown/uncompleted counts. Earlier observed counts survive a later failure, but unit results are not yet durably resumable.

Native structured outputs and clinical output token caps remain unconfigured pending exact deployment/API compatibility verification. Installed SDK parameter availability alone is insufficient. No live request was used to implement P0.

## Connectivity Checks

`python -m src.pipeline --check-llm` performs a live, opt-in, non-clinical check only after sanitized configuration validation passes. It sends `Reply OK.` and caps output with `max_completion_tokens` by default.

For deployments or API versions that require the legacy token cap field, set:

```powershell
$env:AZURE_OPENAI_CONNECTIVITY_TOKEN_LIMIT_PARAMETER = "max_tokens"
```

The direct diagnostic script remains available:

```powershell
python test_azure_connection.py --dry-run
python test_azure_connection.py --max-output-tokens 16 --timeout 30
```

Routine tests mock Azure responses and must not call the live service.

## Error Handling

Fail-closed behavior is preserved for:

- missing configuration
- invalid endpoint shape
- authentication and authorization failures
- invalid deployment names
- quota exhaustion
- rate limits
- request timeouts
- malformed model responses
- schema-invalid model output

Retries are bounded and reserved for transient failures such as timeout, connection, 408, 409, 429, and 5xx responses, except quota and invalid-request classes remain non-retryable.

## Security And Cost

Do not commit Azure keys, real endpoints with secrets, logs containing source material, generated clinical outputs, or browser state. Full clinical PDF generation against Azure requires explicit approval. Connectivity checks are not clinical generation and must stay low-token.

## Planned Or Unknown

The project intentionally preserves the current Azure endpoint style, deployment configuration, and API version. A future migration to a newer endpoint style requires separate compatibility verification and approval.

# Azure Provider Migration Status

Last updated: 2026-10-09.

## Active Runtime Provider

Azure OpenAI is the only active inference provider for clinical chunk generation. Runtime provider selection is centralized in `src.chunk_generator._provider_from_environment`, which accepts `GASTROAGI_LLM_PROVIDER=azure` and rejects any other configured provider. Missing Azure credentials or deployment settings must fail closed; the pipeline must not fall back to OpenAI, Swan, raw text, or another provider.

`generate_chunks` remains provider-independent by accepting any object that satisfies the `ChunkGeneratorProvider` protocol. That boundary is for testability and future approved migrations, not for automatic runtime fallback.

## Preserved Legacy Code

The legacy `OpenAIChunkGenerator` class in `src.chunk_generator` is retained for now but is not environment-selectable. It should not be removed or reactivated without an approved cleanup plan and focused tests.

No Swan provider module or dependency was found in the current repository inspection.

## Dependencies

`openai>=1.0` remains required because the active Azure provider uses the OpenAI Python SDK's `AzureOpenAI` client. It is not currently an unused dependency.

## Protected Boundaries

This migration status does not change PDF extraction, clinical prompt requirements, validation safeguards, human-review gates, Admin Panel upload behavior, production database contents, ChromaDB collections, or PDF movement/archive safeguards.

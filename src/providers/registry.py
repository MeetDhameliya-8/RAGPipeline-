"""Runtime provider selection for GastroAGI chunk generation."""

from __future__ import annotations

import os

from .azure import AzureChunkGenerator
from .base import ChunkGenerationError, ChunkGeneratorProvider


def provider_from_environment() -> ChunkGeneratorProvider:
    """Select the configured runtime provider, failing closed for unsupported values."""
    provider_name = os.getenv("GASTROAGI_LLM_PROVIDER", "").strip().lower()
    if provider_name == "azure":
        return AzureChunkGenerator.from_environment()
    if provider_name:
        raise ChunkGenerationError(
            f"GENERATION_FAILED: Unsupported LLM provider `{provider_name}`."
        )
    raise ChunkGenerationError("GENERATION_FAILED: LLM generation unavailable.")

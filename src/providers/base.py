"""Provider interfaces and shared generation errors."""

from __future__ import annotations

from typing import Any, Protocol


class ChunkGenerationError(RuntimeError):
    """Raised when chunk generation fails or returns malformed content."""

    def __init__(self, message: str, *, code: str = "GENERATION_FAILED", diagnostics: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.diagnostics = dict(diagnostics or {})


class GenerationResponse(str):
    """String-compatible response with a separate sanitized transport envelope."""

    metadata: dict[str, Any]

    def __new__(cls, content: str, metadata: dict[str, Any]) -> GenerationResponse:
        response = super().__new__(cls, content)
        response.metadata = dict(metadata)
        return response


class ChunkGeneratorProvider(Protocol):
    """Provider interface for LLM-backed or deterministic chunk generation."""

    def generate(self, article_text: str, source_file: str, prompt: str) -> str:
        """Return a JSON string following the GastroAGI schema."""

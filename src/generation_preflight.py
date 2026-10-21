"""Fail before paid source recovery when deterministic identity cannot pass."""
from __future__ import annotations

from typing import Any

from .providers.base import ChunkGenerationError


def require_source_reference(extraction: dict[str, Any]) -> str:
    from .chunk_generator import _verified_reference_from_extraction

    bounded = dict(extraction)
    bounded['reference_source_text'] = extraction.get('reference_source_text') or (
        '\n\n'.join(str(p.get('raw_text') or '') for p in extraction.get('pages', [])[:2])
        or str(extraction.get('full_text') or ''))
    reference = _verified_reference_from_extraction(bounded)
    if not reference:
        raise ChunkGenerationError(
            'Source citation identity cannot pass deterministic checks. No paid page reading or chunk generation was started; source reference review is required.',
            code='REFERENCE_IDENTITY_UNRESOLVED')
    return reference

"""Explicit, bounded front-matter reading; OCR agreement never approves identity.

This is an operator-facing library boundary. A caller must supply a provider and
independently review the rendered focal article before applying its citation.
It never changes clinical buffers, clinical source checks or publication gates.
"""
from __future__ import annotations

from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from . import diagnostics as telemetry
from .evidence import add_buffer, get_evidence, text_hash, warn
from .pdf_extractor import extract_document_reference_metadata
from .providers.base import ChunkGenerationError

PROMPT = (
    "Read the article front matter on this page verbatim, including its title and publisher citation/DOI. "
    "Stop at any References or Bibliography heading. Omit the clinical body, tables and figures. "
    "Do not supply cited articles' identifiers as this article's DOI. Never guess, repair, complete or "
    "look up missing characters. Return strict JSON: "
    '{"lines":["verbatim front-matter line"],"uncertainties":["unreadable or ambiguous item"]}.'
)
VERSION = "reviewed-front-matter-v1"


class PrivateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class FrontMatter(PrivateRecord):
    lines: list[str] = Field(max_length=200, repr=False)
    uncertainties: list[str] = Field(max_length=100, repr=False)


class CitationPage(PrivateRecord):
    page_number: int = Field(ge=1, le=2)
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    passes: list[FrontMatter] = Field(min_length=2, max_length=2, repr=False)


class CitationReadings(PrivateRecord):
    version: Literal["reviewed-front-matter-v1"] = VERSION
    pdf_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    provider_fingerprint: str = Field(pattern=r"^[a-f0-9]{64}$")
    prompt_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    pages: list[CitationPage] = Field(min_length=1, max_length=2, repr=False)

    @property
    def reading_id(self) -> str:
        return text_hash(self.model_dump_json())

    def candidate_reference(self) -> str:
        """A candidate remains held until independent source review, even if equal."""
        if [p.page_number for p in self.pages] != list(range(1, len(self.pages)+1)):
            return ""
        references = []
        for attempt in range(2):
            if any(p.passes[attempt].uncertainties for p in self.pages):
                return ""
            pages = []
            for page in self.pages:
                text = "\n".join(page.passes[attempt].lines)
                heading = re.search(r"(?im)^\s*(?:references|bibliography)\s*$", text)
                pages.append({"page_number": page.page_number,
                              "raw_text": text[:heading.start()] if heading else text})
                if heading:
                    break
            meta = extract_document_reference_metadata(pages)
            # DOI-only: do not fill missing journal/pages/title from an OCR guess.
            if meta.get("identity_status") != "source_supported" or not meta.get("doi"):
                return ""
            references.append("doi:"+meta["doi"])
        return references[0] if references[0] == references[1] else ""


class ReviewedCitation(PrivateRecord):
    readings: CitationReadings = Field(repr=False)
    reading_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    review_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    focal_article_confirmed: Literal[True]
    identifier_matches_rendered_source: Literal[True]


def _render(page) -> bytes:
    import fitz
    if page.rect.width*page.rect.height*2.25 > 12_000_000:
        raise ChunkGenerationError("Front-matter render budget exceeded; source review required.",
                                   code="SOURCE_PASSAGE_HELD")
    return page.get_pixmap(matrix=fitz.Matrix(1.5, 1.5)).tobytes("png")


@telemetry.observed_run
@telemetry.timed("citation_recovery")
def recover_citation_readings(pdf_path: Path, *, provider, cache_directory: Path,
                              max_pages: int = 2, allow_network: bool = True) -> CitationReadings:
    """At most four logical reads, explicitly invoked; completed passes survive failure.

    No provider is constructed implicitly. Cache identity includes exact PDF bytes,
    rendering, page, prompt and provider configuration. No citation is auto-applied.
    """
    import fitz
    from .chunk_generator import parse_model_json
    from .preparation_cache import _atomic_write
    if isinstance(max_pages, bool) or max_pages not in (1, 2):
        raise ValueError("Citation reading is restricted to one or two front pages.")
    digest = sha256(pdf_path.read_bytes()).hexdigest()
    configuration = {key: getattr(provider, key, None) for key in ("deployment", "endpoint", "api_version")}
    configuration = {k: v if isinstance(v, str) else type(provider).__name__ for k, v in configuration.items()}
    fingerprint = text_hash(json.dumps(configuration, sort_keys=True))
    pages = []
    cache_directory.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as pdf:
        for position in range(min(max_pages, len(pdf))):
            png = _render(pdf[position]); image_hash = sha256(png).hexdigest()
            key = text_hash(f"{VERSION}:{digest}:{position+1}:{image_hash}:{fingerprint}:{text_hash(PROMPT)}")
            transcripts = []
            for attempt in (1, 2):
                path = cache_directory/f"{key}-pass-{attempt}.json"
                parsed = None
                if path.exists():
                    try:
                        parsed = FrontMatter.model_validate_json(path.read_bytes(), strict=True)
                        telemetry.count("cached_citation_passes")
                    except (OSError, ValueError):
                        telemetry.count("invalid_citation_checkpoints")
                if parsed is None:
                    if not allow_network:
                        raise ChunkGenerationError("Citation reading checkpoint unavailable; source review required.",
                                                   code="SOURCE_PASSAGE_HELD")
                    with telemetry.stage("citation_transcription_pass"):
                        raw = telemetry.model_call("citation_transcription", provider.transcribe_image, png, PROMPT)
                    try:
                        parsed = FrontMatter.model_validate(parse_model_json(raw), strict=True)
                    except (ValueError, ChunkGenerationError):
                        raise ChunkGenerationError("Citation reading schema invalid; source review required.",
                                                   code="MALFORMED_RESPONSE") from None
                    _atomic_write(path, parsed.model_dump_json().encode())
                transcripts.append(parsed)
            pages.append(CitationPage(page_number=position+1, image_sha256=image_hash, passes=transcripts))
    if sha256(pdf_path.read_bytes()).hexdigest() != digest:
        raise ValueError("Source identity changed during citation recovery.")
    return CitationReadings(pdf_sha256=digest, provider_fingerprint=fingerprint,
                            prompt_sha256=text_hash(PROMPT), pages=pages)


@telemetry.timed("citation_review_verification")
def apply_reviewed_citation(pdf_path: Path, extraction: dict, review: ReviewedCitation) -> dict:
    """Verify a supplied source-review attestation, never infer clinical approval.

    Attestations are operator inputs, not proof of reviewer identity. Integrations
    must obtain the confirmations from a human viewing this exact focal article.
    Native citation conflicts cannot be overridden through this boundary.
    """
    import fitz
    review = ReviewedCitation.model_validate(review.model_dump(), strict=True)
    readings = review.readings
    doc = get_evidence(extraction)
    digest = sha256(pdf_path.read_bytes()).hexdigest()
    reference = readings.candidate_reference()
    native = extraction.get("reference_metadata") or {}
    if (digest != readings.pdf_sha256 or digest != doc.pdf_sha256 or doc.identity_quality != "pdf_bytes"
            or review.reading_id != readings.reading_id or readings.prompt_sha256 != text_hash(PROMPT)
            or not reference or native.get("identity_status") == "conflict"
            or (native.get("reference") and native["reference"] != reference)):
        raise ValueError("Citation review identity is incompatible; source review required.")
    with fitz.open(pdf_path) as pdf:
        for page in readings.pages:
            if page.page_number > len(pdf) or sha256(_render(pdf[page.page_number-1])).hexdigest() != page.image_sha256:
                raise ValueError("Citation review rendering changed; source review required.")
    spans = []; source_text = []
    for page in readings.pages:
        for attempt, transcript in enumerate(page.passes, 1):
            text = "\n".join(transcript.lines)
            identity = add_buffer(doc, "raw", f"citation:{review.reading_id}:{page.page_number}:{attempt}",
                                  text, page.page_number, "reviewed_front_matter")
            spans.append({"buffer_id": identity, "page_number": page.page_number,
                          "char_start": 0, "char_end": len(text), "extractor": "reviewed_front_matter"})
            source_text.append(text)
    if sha256(pdf_path.read_bytes()).hexdigest() != digest:
        raise ValueError("Source identity changed during citation verification.")
    warn(doc, "citation_recovered_requires_clinical_review",
         "Source-reviewed front matter does not establish clinical source coverage or approval.", 1, True)
    return dict(extraction, evidence=doc.model_dump(mode="json"), reference_source_text="\n\n".join(source_text),
                reference_metadata={"identity_status": "source_supported", "reference": reference,
                                    "doi": reference[4:], "bibliography_status": "review_required", "source_spans": spans},
                reviewed_citation=review.model_dump(mode="json"))

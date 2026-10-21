"""Deterministic validation for GastroAGI chunk drafts."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
from functools import lru_cache
import json
from pathlib import Path
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, StrictInt, StrictStr, ValidationError

from .numeric_evidence import local_numeric_findings
from .editorial_grounding import recommendation_metadata_equivalent


REQUIRED_FIELDS = ("topic", "subtopic", "section", "reference", "original_text")
ALLOWED_CHUNK_FIELDS = set(REQUIRED_FIELDS) | {"chunk_number"}
TARGET_MIN_WORDS = 150
TARGET_MAX_WORDS = 250
HARD_MIN_WORDS = 90
HARD_MAX_WORDS = 300
REFERENCE_PLACEHOLDERS = {
    "",
    "n/a",
    "na",
    "none",
    "not available",
    "unknown",
    "tbd",
    "to be determined",
    "no reference",
}
VAGUE_SECTIONS = {
    "results",
    "discussion",
    "treatment",
    "conclusion",
    "conclusions",
    "background",
    "other",
    "other findings",
    "general information",
}
GENERIC_CLASSIFICATION_LABELS = {
    "original article",
    "original research",
    "article",
    "research",
    "review",
    "abstract",
    "medicine",
    "gastroenterology",
    "inflammatory bowel disease",
    "results",
    "discussion",
    "background",
}
SECTION_AUTHOR_START_RE = re.compile(
    r"^(dr|prof|professor|mr|mrs|ms|miss)\s+[A-Z][A-Za-z-]+|"
    r"\bdepartment of\b|"
    r"\bdivision of\s+(?:gastroenterology|hepatology|medicine|surgery|digestive|internal medicine|clinical|biostatistics|epidemiology)\b",
    re.IGNORECASE,
)
SECTION_CITATION_RE = re.compile(r"\b\d{1,3}\s+[A-Z][A-Za-z-]+\s+[A-Z]")
BROKEN_EXTRACTION_RE = re.compile(r"\b(?:[A-Za-z]\s+){8,}[A-Za-z]\b")
REFERENCE_FRAGMENT_RE = re.compile(
    r"\b(?:et al\.|eds\.|assessment of quality of life|health-related quality of life)\b",
    re.IGNORECASE,
)
AFFILIATION_RE = re.compile(
    r"\b(correspondence to|author affiliations|department of|email:)\b|"
    r"\bdivision of\s+(?:gastroenterology|hepatology|medicine|surgery|digestive|internal medicine|clinical|biostatistics|epidemiology)\b",
    re.IGNORECASE,
)
INDEPENDENCE_PHRASES = (
    "as mentioned above",
    "in the previous section",
    "as described earlier",
    "the above",
    "these findings from the previous section",
)
ALLOWED_ABBREVIATIONS = {
    "CI",
    "SD",
    "IQR",
    "OR",
    "HR",
    "RR",
    "DNA",
    "RNA",
    "CT",
    "MRI",
    "US",
    "USA",
    "UK",
    "FDA",
    "NHS",
    "DOI",
}
ABBREVIATION_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,6}s?\b")
NUMBER_RE = re.compile(r"\b\d+(?:,\d{3})*(?:\.\d+)?%?\b")
RECOMMENDATION_TERMS = (
    "recommend",
    "recommended",
    "should",
    "must",
    "may be considered",
    "not recommended",
)
SUGGEST_RECOMMENDATION_RE = re.compile(
    r"\b(?:we|authors?|guidelines?|clinicians?|providers?|physicians?)\s+suggest(?:s|ed)?\b|"
    r"\bsuggest(?:s|ed)?\s+(?:treatment|therapy|use|using|administration|dose|dosing|screening|monitoring)\b",
    re.IGNORECASE,
)
CLAIM_TERMS = (
    "reported",
    "found",
    "showed",
    "demonstrated",
    "associated",
    "increased",
    "reduced",
    "improved",
    "higher",
    "lower",
    "significant",
    "benefit",
    "effective",
    "cure",
    "cures",
    "cured",
    "guaranteed",
)
STRONG_CLAIM_TERMS = (
    "demonstrated",
    "proved",
    "proven",
    "established",
    "effective",
    "should",
    "must",
    "recommended",
)
QUALIFICATION_TERMS = (
    "limitation",
    "limitations",
    "uncertain",
    "insufficient",
    "not statistically significant",
    "not significant",
    "no significant",
    "may",
    "might",
    "suggests",
    "exploratory",
    "observational",
)
NEGATED_RESULT_TERMS = (
    "not statistically significant",
    "not significant",
    "no significant",
    "no difference",
    "did not differ",
    "was not different",
)
POPULATION_TERMS = (
    "adult",
    "adults",
    "children",
    "pediatric",
    "paediatric",
    "elderly",
    "pregnant",
    "patients with",
    "participants with",
)


class TechnicalArticleModel(BaseModel):
    """Layer 1 technical contract for generated article metadata."""

    model_config = ConfigDict(extra="forbid")

    source_file: StrictStr
    topic: StrictStr
    subtopic: StrictStr
    reference: StrictStr
    number_of_chunks: StrictInt


class TechnicalChunkModel(BaseModel):
    """Layer 1 technical contract for generated chunk records."""

    model_config = ConfigDict(extra="forbid")

    chunk_number: StrictInt | None = None
    topic: StrictStr
    subtopic: StrictStr
    section: StrictStr
    reference: StrictStr
    original_text: StrictStr


class TechnicalGeneratedDocumentModel(BaseModel):
    """Layer 1 technical contract for a generated chunk package."""

    model_config = ConfigDict(extra="forbid")

    article: TechnicalArticleModel
    chunks: list[TechnicalChunkModel]


def eligible_chunk_numbers(report: dict[str, Any], allowed_statuses: set[str]) -> set[int]:
    """Fail closed for document errors, missing states, or conflicting chunk decisions."""
    if any(
        isinstance(issue, dict)
        and issue.get("severity") == "ERROR"
        and not isinstance(issue.get("chunk"), int)
        for issue in report.get("issues", [])
    ):
        return set()
    states: dict[int, set[str]] = defaultdict(set)
    for item in report.get("chunk_statuses", []):
        if isinstance(item, dict) and type(item.get("chunk")) is int:
            states[item["chunk"]].add(str(item.get("status", "")).strip().upper())
    return {number for number, statuses in states.items() if statuses <= allowed_statuses}


def validate_chunks(
    generated: dict[str, Any],
    source_text: str = "",
    previous_output_root: str | Path | None = None,
    source_pages: list[dict[str, Any]] | None = None,
    source_sections: list[dict[str, Any]] | None = None,
    *, chunk_sources: dict[int, list[dict[str, Any]]] | None = None,
    editorial: bool = False,
) -> dict[str, Any]:
    """Validate generated chunks and return a review-oriented report."""
    issues: list[dict[str, Any]] = []
    if not isinstance(generated, dict):
        issues.append(_issue(None, "ERROR", "SCHEMA", "Generated output must be an object."))
        return _validation_report(issues, 0)

    _validate_technical_contract(generated, issues)
    chunks = generated.get("chunks")

    if not isinstance(chunks, list):
        issues.append(_issue(None, "ERROR", "SCHEMA", "`chunks` must be a list."))
        chunks = []

    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            issues.append(_issue(index, "ERROR", "SCHEMA", "Chunk must be an object."))
            continue
        _validate_required_fields(index, chunk, issues)
        _validate_extra_fields(index, chunk, issues)
        _validate_classification(index, "topic", str(chunk.get("topic", "")), issues)
        _validate_classification(index, "subtopic", str(chunk.get("subtopic", "")), issues)
        _validate_reference_quality(index, str(chunk.get("reference", "")), issues)
        if editorial:
            count = _word_count(str(chunk.get("original_text", "")))
            if count < 100 or count > 200:
                issues.append(_issue(index, "WARNING", "WORD_COUNT",
                    "Outside the editorial 100-200 word target; source-specific length justification requires review."))
        else:
            _validate_word_count(index, str(chunk.get("original_text", "")), issues)
        _validate_section(index, str(chunk.get("section", "")), issues)
        _validate_original_text_quality(index, str(chunk.get("original_text", "")), issues)
        _validate_independence(index, str(chunk.get("original_text", "")), issues)
        _validate_abbreviations(index, str(chunk.get("original_text", "")), issues)
        packets = chunk_sources.get(index, []) if chunk_sources is not None else [{"text": source_text}] if source_text else []
        if chunk_sources is not None and (not packets or any(not p.get("text", "").strip() for p in packets)):
            issues.append(_issue(index, "ERROR", "SOURCE_FIDELITY", "Selected source ownership is unavailable."))
        for packet in packets:
            original_text = str(chunk.get("original_text", ""))
            _validate_numbers_against_source(index, original_text, packet["text"], issues)
            if editorial:
                _validate_editorial_source_grounding(index, original_text, packet["text"], issues)
            else:
                _validate_source_grounding(index, original_text, packet["text"], issues)
            if packet.get("visual_transcription"):
                _validate_numbers_against_source(index,original_text,packet.get("native_numeric_source",""),issues)
                issues.append(_issue(index,"WARNING","VISUAL_SOURCE_REVIEW",
                    "Two visual transcriptions agree; independent clinical/source verification is still required. Native numerical uncertainty is retained."))
        if any(packet.get("review_required") for packet in packets):
            issues.append(_issue(index,"WARNING","SOURCE_CONTEXT",
                "Source evidence supports draft text only; clinical relationships, qualifications and incomplete source coverage require review."))
        if editorial:
            issues.append(_issue(index, "WARNING", "EDITORIAL_REVIEW",
                "Paraphrased clinical claims require independent source and clinician review; automated checks cannot approve them."))

    _validate_output_completeness(generated, chunks, issues)
    _validate_article_required_fields(generated.get("article", {}), issues)
    _validate_article_reference(generated.get("article", {}), issues)
    _validate_article_consistency(chunks, "topic", issues)
    _validate_article_consistency(chunks, "subtopic", issues)
    _validate_article_consistency(chunks, "reference", issues)
    _validate_article_classification(generated.get("article", {}), issues)
    _validate_duplicates(chunks, issues)

    reference = str(generated.get("article", {}).get("reference", "")).strip()
    if previous_output_root and reference:
        _validate_previous_reference(reference, Path(previous_output_root), issues)

    report = _validation_report(issues, len(chunks))
    if chunk_sources is not None:
        report["source_evidence"] = []
        for index, chunk in enumerate(chunks, 1):
            for packet in chunk_sources.get(index, []):
                records = _build_source_evidence([chunk], packet["text"], [], [])
                for record in records:
                    # This span indexes a bounded request evidence string, not
                    # a persisted canonical document. Canonical/native ownership
                    # is carried explicitly in selected_source.
                    record["request_evidence_span"] = record["text_span"]
                    record["text_span"] = None
                    record.update(chunk=index, selected_source={k: v for k, v in packet.items() if k not in {"text","native_numeric_source"}})
                report["source_evidence"].extend(records)
    elif source_text:
        report["source_evidence"] = _build_source_evidence(
            chunks,
            source_text=source_text,
            source_pages=source_pages or [],
            source_sections=source_sections or [],
        )
    return report



def _validate_technical_contract(generated: dict[str, Any], issues: list[dict[str, Any]]) -> None:
    try:
        TechnicalGeneratedDocumentModel.model_validate(generated)
    except ValidationError as exc:
        for error in exc.errors()[:12]:
            location = ".".join(str(part) for part in error.get("loc", ())) or "<root>"
            error_type = str(error.get("type", "invalid"))
            issues.append(
                _issue(
                    None,
                    "ERROR",
                    "SCHEMA",
                    f"Technical schema error at `{location}`: {error_type}.",
                )
            )


def _validate_output_completeness(
    generated: dict[str, Any], chunks: Any, issues: list[dict[str, Any]]
) -> None:
    if not isinstance(chunks, list):
        return
    if not chunks:
        issues.append(
            _issue(None, "ERROR", "OUTPUT_COMPLETENESS", "No chunks were generated.")
        )

    article = generated.get("article", {})
    if not isinstance(article, dict):
        return
    expected_count = article.get("number_of_chunks")
    if isinstance(expected_count, int) and expected_count != len(chunks):
        issues.append(
            _issue(
                None,
                "ERROR",
                "OUTPUT_COMPLETENESS",
                f"Article number_of_chunks is {expected_count}, but {len(chunks)} chunk(s) were returned.",
            )
        )


def _validate_article_required_fields(article: Any, issues: list[dict[str, Any]]) -> None:
    if not isinstance(article, dict):
        return
    for field in ("source_file", "topic", "subtopic", "reference"):
        if not str(article.get(field, "")).strip():
            issues.append(
                _issue(
                    None,
                    "ERROR",
                    "REQUIRED_FIELD",
                    f"Article is missing required field `{field}`.",
                )
            )


def _validate_article_reference(article: Any, issues: list[dict[str, Any]]) -> None:
    if not isinstance(article, dict):
        return
    _validate_reference_quality(None, str(article.get("reference", "")), issues)


def _validate_required_fields(
    index: int, chunk: dict[str, Any], issues: list[dict[str, Any]]
) -> None:
    for field in REQUIRED_FIELDS:
        if not str(chunk.get(field, "")).strip():
            issues.append(
                _issue(
                    index,
                    "ERROR",
                    "REQUIRED_FIELD",
                    f"Chunk is missing required field `{field}`.",
                )
            )


def _validate_extra_fields(
    index: int, chunk: dict[str, Any], issues: list[dict[str, Any]]
) -> None:
    extra_fields = sorted(set(chunk) - ALLOWED_CHUNK_FIELDS)
    if extra_fields:
        issues.append(
            _issue(
                index,
                "WARNING",
                "SCHEMA",
                "Chunk contains non-GastroAGI fields: "
                + ", ".join(f"`{field}`" for field in extra_fields)
                + ".",
            )
        )



def _validate_reference_quality(
    index: int | None, reference: str, issues: list[dict[str, Any]]
) -> None:
    normalized = _normalize_label(reference)
    if normalized in REFERENCE_PLACEHOLDERS:
        issues.append(
            _issue(index, "ERROR", "INVALID_REFERENCE", "Reference is empty or a placeholder.")
        )
        return
    if normalized.endswith(".pdf") and "doi" not in normalized:
        issues.append(
            _issue(
                index,
                "ERROR",
                "INVALID_REFERENCE",
                "Reference appears to be a source filename rather than verified citation metadata.",
            )
        )


def _validate_word_count(index: int, original_text: str, issues: list[dict[str, Any]]) -> None:
    count = _word_count(original_text)
    if count < HARD_MIN_WORDS:
        issues.append(
            _issue(index, "ERROR", "WORD_COUNT", f"Chunk contains only {count} words.")
        )
    elif count > HARD_MAX_WORDS:
        issues.append(
            _issue(index, "ERROR", "WORD_COUNT", f"Chunk contains {count} words.")
        )
    elif count < TARGET_MIN_WORDS:
        issues.append(
            _issue(
                index,
                "WARNING",
                "WORD_COUNT",
                f"Chunk contains {count} words; typical target is {TARGET_MIN_WORDS}-{TARGET_MAX_WORDS}.",
            )
        )
    elif count > TARGET_MAX_WORDS:
        issues.append(
            _issue(
                index,
                "WARNING",
                "WORD_COUNT",
                f"Chunk contains {count} words; typical target is {TARGET_MIN_WORDS}-{TARGET_MAX_WORDS}.",
            )
        )


def _validate_section(index: int, section: str, issues: list[dict[str, Any]]) -> None:
    normalized = re.sub(r"\s+", " ", section).strip().lower()
    if len(section) > 140:
        issues.append(
            _issue(
                index,
                "ERROR",
                "SECTION_QUALITY",
                "Section exceeds 140 characters.",
            )
        )
    if normalized in VAGUE_SECTIONS:
        issues.append(
            _issue(index, "ERROR", "SECTION_QUALITY", f"Vague section: `{section}`.")
        )
    if section and section[0].islower():
        issues.append(
            _issue(
                index,
                "ERROR",
                "SECTION_QUALITY",
                "Section appears to begin with a sentence fragment.",
            )
        )
    if SECTION_AUTHOR_START_RE.search(section):
        issues.append(
            _issue(
                index,
                "ERROR",
                "SECTION_QUALITY",
                "Section appears to begin with author or affiliation text.",
            )
        )
    if SECTION_CITATION_RE.search(section):
        issues.append(
            _issue(
                index,
                "ERROR",
                "SECTION_QUALITY",
                "Section appears to contain citation-list numbering.",
            )
        )


def _validate_classification(
    index: int, field: str, value: str, issues: list[dict[str, Any]]
) -> None:
    normalized = _normalize_label(value)
    if _is_generic_classification(normalized):
        issues.append(
            _issue(
                index,
                "ERROR",
                "CLASSIFICATION",
                f"Chunk {field} is a generic document label: `{value}`.",
            )
        )


def _validate_article_classification(article: Any, issues: list[dict[str, Any]]) -> None:
    if not isinstance(article, dict):
        issues.append(_issue(None, "ERROR", "SCHEMA", "`article` must be an object."))
        return
    for field in ("topic", "subtopic"):
        value = str(article.get(field, ""))
        normalized = _normalize_label(value)
        if _is_generic_classification(normalized):
            issues.append(
                _issue(
                    None,
                    "ERROR",
                    "CLASSIFICATION",
                    f"Article {field} is a generic document label: `{value}`.",
                )
            )


def _validate_original_text_quality(
    index: int, original_text: str, issues: list[dict[str, Any]]
) -> None:
    if BROKEN_EXTRACTION_RE.search(original_text):
        issues.append(
            _issue(
                index,
                "ERROR",
                "EXTRACTION_ARTIFACT",
                "Original Text contains broken spaced-letter extraction artifacts.",
            )
        )
    if AFFILIATION_RE.search(original_text):
        issues.append(
            _issue(
                index,
                "ERROR",
                "SOURCE_CLEANING",
                "Original Text contains author affiliation or correspondence text.",
            )
        )
    if len(REFERENCE_FRAGMENT_RE.findall(original_text)) >= 3:
        issues.append(
            _issue(
                index,
                "ERROR",
                "REFERENCE_LEAKAGE",
                "Original Text appears to contain reference-list fragments.",
            )
        )


def _validate_independence(
    index: int, original_text: str, issues: list[dict[str, Any]]
) -> None:
    lower = original_text.lower()
    for phrase in INDEPENDENCE_PHRASES:
        if phrase in lower:
            issues.append(
                _issue(
                    index,
                    "WARNING",
                    "INDEPENDENCE",
                    f"Chunk uses context-dependent phrase: `{phrase}`.",
                )
            )


def _validate_abbreviations(
    index: int, original_text: str, issues: list[dict[str, Any]]
) -> None:
    unexplained = []
    for token in sorted(set(ABBREVIATION_RE.findall(original_text))):
        normalized = token.rstrip("s")
        if normalized in ALLOWED_ABBREVIATIONS:
            continue
        if _is_abbreviation_explained(normalized, original_text):
            continue
        unexplained.append(normalized)

    if unexplained:
        preview = ", ".join(unexplained[:5])
        issues.append(
            _issue(
                index,
                "WARNING",
                "ABBREVIATION",
                f"Potential unexplained abbreviation(s): {preview}.",
            )
        )


def _validate_numbers_against_source(
    index: int, original_text: str, source_text: str, issues: list[dict[str, Any]]
) -> None:
    findings = []
    for sentence in _split_sentences(original_text):
        passages = _source_support_sentences(sentence, source_text, min_overlap=0.45)
        if _normalize_text_for_matching(sentence) in _normalize_text_for_matching(source_text):
            passages = [sentence]
        findings.extend(local_numeric_findings(sentence, passages, source_text))
    for severity, rule, message in dict.fromkeys(findings):
        issues.append(_issue(index, severity, rule, message))


def source_claim_issues(text: str, source_text: str) -> list[dict[str, Any]]:
    """Apply the existing evidence checks to clinical answer text without chunk schema rules."""
    issues: list[dict[str, Any]] = []
    if text.strip() and not source_text.strip():
        return [_issue(1, "WARNING", "EVIDENCE_QUALITY", "Source evidence is unavailable; clinical review is required.")]
    _validate_numbers_against_source(1, text, source_text, issues)
    _validate_source_grounding(1, text, source_text, issues)
    return issues



def _validate_source_grounding(
    index: int, original_text: str, source_text: str, issues: list[dict[str, Any]]
) -> None:
    _validate_grounding(index, original_text, source_text, issues)


def _validate_editorial_source_grounding(
    index: int, original_text: str, source_text: str, issues: list[dict[str, Any]]
) -> None:
    _validate_grounding(index, original_text, source_text, issues, editorial_metadata=True)


def _validate_grounding(
    index: int, original_text: str, source_text: str, issues: list[dict[str, Any]],
    *, editorial_metadata: bool = False,
) -> None:
    source_normalized = _normalize_text_for_matching(source_text)
    if not source_normalized:
        return

    for sentence in _split_sentences(original_text):
        if not sentence.strip():
            continue
        sentence_normalized = _normalize_text_for_matching(sentence)
        if _contradicts_source(sentence_normalized, source_text):
            issues.append(
                _issue(
                    index,
                    "ERROR",
                    "SOURCE_GROUNDING",
                    "Chunk appears to state a positive or significant finding that contradicts a negative or non-significant source statement.",
                )
            )
            continue
        if (_contains_recommendation_language(sentence_normalized)
                and _recommendation_negation_conflict(sentence, source_text)):
            issues.append(_issue(index, "ERROR", "SOURCE_GROUNDING", "Recommendation negation conflicts with the same local source proposition."))
            continue
        if _contains_recommendation_language(sentence_normalized) and not _has_source_support(sentence, source_text):
            if editorial_metadata and recommendation_metadata_equivalent(sentence, source_text):
                issues.append(_issue(index, "WARNING", "EDITORIAL_GROUNDING_REVIEW",
                    "Recommendation strength and evidence certainty retain explicit source wording through a limited grammatical equivalence; clinical review remains required."))
                continue
            issues.append(
                _issue(
                    index,
                    "ERROR",
                    "SOURCE_GROUNDING",
                    "Recommendation language is not traceable to a closely matching source passage.",
                )
            )
            continue
        if _is_claim_sentence(sentence_normalized) and not _has_source_support(sentence, source_text):
            issues.append(
                _issue(
                    index,
                    "WARNING",
                    "SOURCE_GROUNDING",
                    "Clinical claim sentence is not closely supported by extracted source text.",
                )
            )
        elif (_is_claim_sentence(sentence_normalized)
              and sentence_normalized not in source_normalized
              and _has_source_support(sentence, source_text)):
            issues.append(_issue(index, "WARNING", "CLAIM_ASSOCIATION",
                "Lexical overlap does not establish endpoint, negation, treatment-arm or comparator identity; review is required."))

    missing_population_terms = [
        term for term in POPULATION_TERMS if term in _normalize_text_for_matching(original_text) and term not in source_normalized
    ]
    if missing_population_terms:
        issues.append(
            _issue(
                index,
                "WARNING",
                "SOURCE_GROUNDING",
                "Population descriptor(s) not found in extracted source text: "
                + ", ".join(missing_population_terms[:4])
                + ".",
            )
        )

    original_normalized = _normalize_text_for_matching(original_text)
    if (
        _contains_any(source_normalized, QUALIFICATION_TERMS)
        and _contains_any(original_normalized, STRONG_CLAIM_TERMS)
        and not _contains_any(original_normalized, QUALIFICATION_TERMS)
    ):
        issues.append(
            _issue(
                index,
                "WARNING",
                "SOURCE_GROUNDING",
                "Source text contains uncertainty or limitations that may be missing from a strong chunk claim.",
            )
        )


def _build_source_evidence(
    chunks: Any,
    source_text: str,
    source_pages: list[dict[str, Any]],
    source_sections: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(chunks, list):
        return []

    evidence: list[dict[str, Any]] = []
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        original_text = str(chunk.get("original_text", "")).strip()
        exact_span = _find_exact_span(source_text, original_text)
        snippet = original_text if exact_span else _first_exact_sentence(original_text, source_text)
        match_type = "exact" if exact_span else "not_found"
        if not snippet:
            supported = _best_supported_source_sentence(original_text, source_text)
            if supported:
                snippet = supported
                match_type = "sentence_overlap"
        span = exact_span or (_find_exact_span(source_text, snippet) if snippet else None)
        page_numbers = _matching_page_numbers(snippet, source_pages) if snippet else []
        matched_sections = _matching_source_sections(snippet, source_sections) if snippet else []
        section_ids = [section["section_id"] for section in matched_sections]

        evidence.append(
            {
                "chunk": int(chunk.get("chunk_number") or index),
                "match_type": "exact" if exact_span else match_type,
                "page_numbers": page_numbers,
                "source_section_ids": section_ids,
                "source_sections": matched_sections,
                "source_document_id": matched_sections[0].get("document_id") if matched_sections else None,
                "source_file": matched_sections[0].get("source_file") if matched_sections else None,
                "page_range": _evidence_page_range(page_numbers, matched_sections),
                "text_span": {"start": span[0], "end": span[1],
                              "buffer_id": "full_text:sha256:" + sha256(source_text.encode("utf-8")).hexdigest()} if span else None,
            }
        )
    return evidence


def _find_exact_span(source_text: str, needle: str) -> tuple[int, int] | None:
    needle = needle.strip()
    if len(needle) < 24:
        return None
    start = source_text.find(needle)
    if start == -1:
        return None
    return start, start + len(needle)


def _first_exact_sentence(original_text: str, source_text: str) -> str:
    for sentence in _split_sentences(original_text):
        sentence = re.sub(r"\s+", " ", sentence).strip()
        if _find_exact_span(source_text, sentence):
            return sentence
    return ""


def _matching_page_numbers(snippet: str, source_pages: list[dict[str, Any]]) -> list[int]:
    page_numbers: list[int] = []
    normalized_snippet = _normalize_text_for_matching(snippet)
    if not normalized_snippet:
        return page_numbers
    for page in source_pages:
        page_text = str(page.get("text") or page.get("raw_text") or "")
        if normalized_snippet in _normalize_text_for_matching(page_text):
            page_number = page.get("page_number")
            if isinstance(page_number, int):
                page_numbers.append(page_number)
    return page_numbers



def _matching_source_sections(snippet: str, source_sections: list[dict[str, Any]]) -> list[dict[str, Any]]:
    matched: list[dict[str, Any]] = []
    normalized_snippet = _normalize_text_for_matching(snippet)
    if not normalized_snippet:
        return matched
    for section in source_sections:
        section_text = str(section.get("text") or "")
        if normalized_snippet not in _normalize_text_for_matching(section_text):
            continue
        section_id = section.get("section_id") or section.get("id")
        if section_id is None:
            continue
        page_start = section.get("page_start")
        page_end = section.get("page_end")
        matched.append(
            {
                "section_id": str(section_id),
                "title": str(section.get("title") or ""),
                "kind": str(section.get("kind") or ""),
                "document_id": str(section.get("document_id") or ""),
                "source_file": str(section.get("source_file") or ""),
                "page_range": [page_start, page_end]
                if isinstance(page_start, int) and isinstance(page_end, int)
                else [],
            }
        )
    return matched


def _evidence_page_range(page_numbers: list[int], matched_sections: list[dict[str, Any]]) -> list[int]:
    if page_numbers:
        return [min(page_numbers), max(page_numbers)]
    starts: list[int] = []
    ends: list[int] = []
    for section in matched_sections:
        page_range = section.get("page_range")
        if (
            isinstance(page_range, list)
            and len(page_range) == 2
            and isinstance(page_range[0], int)
            and isinstance(page_range[1], int)
        ):
            starts.append(page_range[0])
            ends.append(page_range[1])
    return [min(starts), max(ends)] if starts and ends else []

def _matching_section_ids(snippet: str, source_sections: list[dict[str, Any]]) -> list[str]:
    section_ids: list[str] = []
    normalized_snippet = _normalize_text_for_matching(snippet)
    if not normalized_snippet:
        return section_ids
    for section in source_sections:
        section_text = str(section.get("text") or "")
        if normalized_snippet in _normalize_text_for_matching(section_text):
            section_id = section.get("section_id") or section.get("id")
            if section_id is not None:
                section_ids.append(str(section_id))
    return section_ids


def _split_sentences(text: str) -> list[str]:
    return [sentence.strip() for sentence in re.split(r"(?<=[.!?])\s+", text) if sentence.strip()]


def _is_claim_sentence(sentence_normalized: str) -> bool:
    return (_contains_any(sentence_normalized, CLAIM_TERMS) or bool(NUMBER_RE.search(sentence_normalized))
            or _contains_recommendation_language(sentence_normalized))


def _has_source_support(sentence: str, source_text: str) -> bool:
    sentence_normalized = _normalize_text_for_matching(sentence)
    if len(sentence_normalized) < 24:
        return True
    source_normalized = _normalize_text_for_matching(source_text)
    if sentence_normalized in source_normalized:
        return True
    sentence_tokens = _content_tokens(sentence_normalized)
    if len(sentence_tokens) < 4:
        return True
    return bool(_source_support_sentences(sentence, source_text, min_overlap=0.72))


def _contradicts_source(sentence_normalized: str, source_text: str) -> bool:
    if _negative_result(sentence_normalized):
        return False
    if not any(
        term in sentence_normalized
        for term in ("significant", "improved", "reduced", "increased", "higher", "lower", "benefit")
    ):
        return False
    endpoints = _endpoint_terms(sentence_normalized)
    for source_sentence in _source_support_sentences(sentence_normalized, source_text, min_overlap=0.25):
        normalized = _normalize_text_for_matching(source_sentence)
        source_endpoints = _endpoint_terms(normalized)
        if not endpoints or not (endpoints & source_endpoints):
            continue
        # A flat sentence with several endpoints cannot prove which endpoint
        # a negative predicate qualifies. Retain review instead of guessing.
        if source_endpoints - endpoints or endpoints - source_endpoints:
            continue
        candidate_scopes = set(re.findall(r"\b(primary|secondary)\b", sentence_normalized))
        source_scopes = set(re.findall(r"\b(primary|secondary)\b", normalized))
        if candidate_scopes != source_scopes:
            continue
        candidate_arms = set(re.findall(r"\b(?:treatment|arm|group)\s+([a-z0-9]+)\b", sentence_normalized))
        source_arms = set(re.findall(r"\b(?:treatment|arm|group)\s+([a-z0-9]+)\b", normalized))
        if candidate_arms and source_arms and candidate_arms != source_arms:
            continue
        if _negative_result(normalized):
            return True
    return False


def _negative_result(text: str) -> bool:
    return bool(re.search(
        r"\b(?:not\s+(?:statistically\s+)?significant\w*|no\s+(?:statistically\s+)?significant\w*|"
        r"nonsignificant\w*|non-significant\w*|without\s+(?:a\s+)?significant\w*|"
        r"no\s+difference|did\s+not\s+(?:significantly\s+)?(?:differ|improve)|"
        r"(?:endpoint|outcome)\s+was\s+not\s+met|was\s+not\s+different)\b", text, re.I))


def _endpoint_terms(text: str) -> set[str]:
    # Generic research vocabulary guides matching only; it supplies no facts.
    return set(re.findall(r"\b(?:remission|recurrence|recurrences|survival|mortality|bleeding|"
                          r"hospitalization|hospitalisation|symptoms|exposure|pain|safety|"
                          r"primary|secondary)\b", text, re.I))


def _contains_recommendation_language(sentence_normalized: str) -> bool:
    return _contains_any(sentence_normalized, RECOMMENDATION_TERMS) or bool(
        SUGGEST_RECOMMENDATION_RE.search(sentence_normalized)
    )


def _recommendation_negation_conflict(sentence: str, source_text: str) -> bool:
    def proposition(text: str) -> list[str]:
        return [word for word in re.findall(r"[a-z]+", text.casefold()) if word not in {"not", "never"}]
    negative = bool(re.search(r"\b(?:not|never)\b", sentence, re.I))
    return any(proposition(sentence) == proposition(passage)
               and negative != bool(re.search(r"\b(?:not|never)\b", passage, re.I))
               for passage in _source_support_sentences(sentence, source_text, 0.72))


def _source_support_sentences(sentence: str, source_text: str, min_overlap: float) -> list[str]:
    sentence_tokens = _content_tokens(sentence)
    if not sentence_tokens:
        return []
    matches = []
    for source_sentence, source_tokens in _source_sentence_records(source_text):
        if not source_tokens:
            continue
        overlap = len(sentence_tokens & source_tokens) / len(sentence_tokens)
        if overlap >= min_overlap:
            matches.append((overlap, source_sentence))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [sentence for _, sentence in matches[:5]]


@lru_cache(maxsize=8)
def _source_sentence_records(source_text: str) -> tuple[tuple[str, frozenset[str]], ...]:
    """Bounded in-memory index only; never persisted or shared across sources."""
    return tuple((sentence, frozenset(_content_tokens(sentence))) for sentence in _split_sentences(source_text))


def _best_supported_source_sentence(original_text: str, source_text: str) -> str:
    best: tuple[float, str] = (0.0, "")
    for sentence in _split_sentences(original_text):
        sentence_tokens = _content_tokens(sentence)
        if len(sentence_tokens) < 4:
            continue
        for source_sentence, source_tokens in _source_sentence_records(source_text):
            if not source_tokens:
                continue
            overlap = len(sentence_tokens & source_tokens) / len(sentence_tokens)
            if overlap > best[0]:
                best = (overlap, source_sentence)
    return best[1] if best[0] >= 0.55 else ""


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    return any(term in text for term in terms)


def _normalize_text_for_matching(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()

def _validate_article_consistency(
    chunks: Any, field: str, issues: list[dict[str, Any]]
) -> None:
    if not isinstance(chunks, list) or len(chunks) < 2:
        return
    values = [
        str(chunk.get(field, "")).strip()
        for chunk in chunks
        if isinstance(chunk, dict) and str(chunk.get(field, "")).strip()
    ]
    if len(set(values)) <= 1:
        return
    counts = Counter(values)
    common_value, _ = counts.most_common(1)[0]
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        value = str(chunk.get(field, "")).strip()
        if value and value != common_value:
            issues.append(
                _issue(
                    index,
                    "WARNING",
                    "CONSISTENCY",
                    f"Chunk {field} differs from the article majority value.",
                )
            )


def _validate_duplicates(chunks: Any, issues: list[dict[str, Any]]) -> None:
    if not isinstance(chunks, list):
        return
    seen_text: dict[str, int] = {}
    seen_section: dict[str, int] = {}
    token_sets: list[tuple[int, set[str]]] = []

    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("original_text", "")).strip()
        section = str(chunk.get("section", "")).strip()
        text_key = _dedupe_key(text)
        section_key = _dedupe_key(section)

        if text_key and text_key in seen_text:
            issues.append(
                _issue(
                    index,
                    "ERROR",
                    "DUPLICATE",
                    f"Original Text duplicates chunk {seen_text[text_key]}.",
                )
            )
        elif text_key:
            seen_text[text_key] = index

        if section_key and section_key in seen_section:
            issues.append(
                _issue(
                    index,
                    "WARNING",
                    "DUPLICATE",
                    f"Section duplicates chunk {seen_section[section_key]}.",
                )
            )
        elif section_key:
            seen_section[section_key] = index

        tokens = _content_tokens(text)
        if len(tokens) >= 20:
            for previous_index, previous_tokens in token_sets:
                similarity = _jaccard(tokens, previous_tokens)
                if similarity >= 0.88:
                    issues.append(
                        _issue(
                            index,
                            "WARNING",
                            "DUPLICATE",
                            f"Original Text is highly similar to chunk {previous_index} ({similarity:.2f}).",
                        )
                    )
                    break
            token_sets.append((index, tokens))


def _validate_previous_reference(
    reference: str, output_root: Path, issues: list[dict[str, Any]]
) -> None:
    if not output_root.exists():
        return
    for chunks_path in output_root.glob("*/chunks.json"):
        try:
            data = json.loads(chunks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        previous_ref = str(data.get("article", {}).get("reference", "")).strip()
        if previous_ref and previous_ref == reference:
            issues.append(
                _issue(
                    None,
                    "WARNING",
                    "DUPLICATE_REFERENCE",
                    f"Reference already appears in previous output: {chunks_path.parent.name}.",
                )
            )


def _is_abbreviation_explained(abbreviation: str, text: str) -> bool:
    escaped = re.escape(abbreviation)
    if re.search(rf"\([ ]*{escaped}[ ]*\)", text):
        return True
    if re.search(rf"\b{escaped}\s*\([A-Za-z][^)]+\)", text):
        return True
    return False


def _normalize_number(number: str) -> str:
    return number.replace(",", "").strip()


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip(" .:-").lower()


def _is_generic_classification(normalized: str) -> bool:
    if normalized in GENERIC_CLASSIFICATION_LABELS:
        return True
    return any(
        re.search(rf"\b{re.escape(label)}\b", normalized)
        for label in GENERIC_CLASSIFICATION_LABELS
        if len(label.split()) > 1
    )


def _dedupe_key(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().lower()


def _content_tokens(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-zA-Z]{4,}", text.lower())
        if token not in {"this", "that", "with", "from", "were", "been", "have"}
    }


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def _word_count(text: str) -> int:
    return len(re.findall(r"\b[\w'-]+\b", text))



def _validation_report(issues: list[dict[str, Any]], chunk_count: int) -> dict[str, Any]:
    errors = sum(1 for issue in issues if issue["severity"] == "ERROR")
    warnings = sum(1 for issue in issues if issue["severity"] == "WARNING")
    chunk_issue_map: dict[int, list[str]] = defaultdict(list)
    for issue in issues:
        if isinstance(issue.get("chunk"), int):
            chunk_issue_map[issue["chunk"]].append(issue["severity"])

    passed_chunks = sum(
        1
        for index in range(1, chunk_count + 1)
        if index not in chunk_issue_map
    )
    rejected_chunks = sum(
        1
        for severities in chunk_issue_map.values()
        if "ERROR" in severities
    )
    review_required_chunks = sum(
        1
        for severities in chunk_issue_map.values()
        if "ERROR" not in severities and "WARNING" in severities
    )
    status = "FAILED" if errors else "REVIEW_REQUIRED" if warnings else "PASS"

    return {
        "status": status,
        "summary": {
            "chunks": chunk_count,
            "passed": passed_chunks,
            "validated_chunks": passed_chunks,
            "review_required_chunks": review_required_chunks,
            "rejected_chunks": rejected_chunks,
            "warnings": warnings,
            "errors": errors,
        },
        "chunk_statuses": [
            {
                "chunk": index,
                "status": "REJECTED"
                if "ERROR" in chunk_issue_map.get(index, [])
                else "REVIEW_REQUIRED"
                if "WARNING" in chunk_issue_map.get(index, [])
                else "PASS",
            }
            for index in range(1, chunk_count + 1)
        ],
        "issues": issues,
    }


def _issue(
    chunk: int | None, severity: str, rule: str, message: str
) -> dict[str, Any]:
    issue: dict[str, Any] = {
        "severity": severity,
        "rule": rule,
        "category": _issue_category(rule, message),
        "message": message,
    }
    if chunk is not None:
        issue["chunk"] = chunk
    return issue


def _issue_category(rule: str, message: str) -> str:
    normalized_rule = rule.strip().upper()
    normalized_message = message.lower()
    if normalized_rule == "SCHEMA":
        return "INVALID_SCHEMA"
    if normalized_rule in {"INVALID_REFERENCE", "DUPLICATE_REFERENCE"}:
        return "REFERENCE_UNVERIFIED"
    if normalized_rule == "SOURCE_FIDELITY":
        return "SOURCE_EVIDENCE_MISSING"
    if normalized_rule == "SOURCE_GROUNDING":
        if "contradict" in normalized_message:
            return "CLINICAL_CONTRADICTION"
        return "SOURCE_EVIDENCE_MISSING"
    if normalized_rule in {"EXTRACTION_ARTIFACT", "SOURCE_CLEANING", "REFERENCE_LEAKAGE"}:
        return "EXTRACTION_INCOMPLETE"
    if normalized_rule in {"WORD_COUNT", "ABBREVIATION", "CONSISTENCY"}:
        return "REVIEW_REQUIRED"
    if normalized_rule == "GENERATION_FAILED":
        return "LLM_GENERATION_FAILED"
    return "REVIEW_REQUIRED" if "review" in normalized_message or normalized_rule else "REVIEW_REQUIRED"











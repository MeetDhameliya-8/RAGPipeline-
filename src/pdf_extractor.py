"""PDF text extraction for GastroAGI source articles."""

from __future__ import annotations

from .diagnostics import timed

from collections import Counter
from hashlib import sha256
from importlib import metadata as importlib_metadata
from pathlib import Path
import math
import re
from typing import Any

from .adaptive_ingestion import assess_page_text, build_ingestion_plan
from .evidence import EvidenceDocument, build_evidence_document


PAGE_NUMBER_RE = re.compile(r"^(?:page\s*)?\d+\s*(?:of\s+\d+)?$", re.IGNORECASE)
WHITESPACE_RE = re.compile(r"[ \t]+")
URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+\b", re.IGNORECASE)
CITATION_RE = re.compile(
    r"\b[A-Z][A-Za-z][A-Za-z &.-]{1,40}\s+\d{4};\d+(?:\(\d+\))?:[A-Za-z]?\d+[-–]\d+\.?",
)
NEJM_CITATION_RE = re.compile(
    r"\b(?:N\s+)?Engl\s+J\s+Med\s+"
    r"(?P<year>\d{4})\s*;\s*"
    r"(?P<volume>\d(?:\s*\d){1,4})"
    r"(?:\s*\(\s*(?P<issue>\d+)\s*\))?"
    r"\s*:\s*(?P<pages>[A-Za-z]?\d+\s*[-–]\s*\d+)\.?",
    re.IGNORECASE,
)
VOLUME_ISSUE_RE = re.compile(
    r"\bvol\.\s*(?P<volume>\d(?:\s*\d){1,4})\s+no\.\s*(?P<issue>\d+)\b",
    re.IGNORECASE,
)
BOILERPLATE_RE = re.compile(
    r"("
    r"downloaded\s+from|"
    r"copyright|"
    r"all\s+rights\s+reserved|"
    r"correspondence\s+to|"
    r"accepted\s+\d|"
    r"published\s+online|"
    r"email:|"
    r"http[s]?://|"
    r"www\.|"
    r"gut\.bmj\.com|"
    r"group\.bmj\.com|"
    r"permissions|"
    r"reprints"
    r")",
    re.IGNORECASE,
)
REFERENCE_HEADING_RE = re.compile(
    r"\n\s*(references|bibliography)\s*\n", re.IGNORECASE
)
SPACED_LETTER_ARTIFACT_RE = re.compile(r"\b(?:[A-Za-z]\s+){8,}[A-Za-z]\b")
FONT_CODE_ARTIFACT_RE = re.compile(r"/H\d{4,}|/C\d{2,}|\(cid:\d+\)", re.IGNORECASE)


class PdfExtractionError(RuntimeError):
    """Raised when a PDF cannot provide source text safe for generation."""
    extraction: dict[str, Any] | None = None


@timed("native_extraction")
def extract_pdf(pdf_path: str | Path) -> dict[str, Any]:
    """Extract text from a PDF while preserving raw page text for debugging."""
    path = Path(pdf_path)
    if not path.exists():
        raise FileNotFoundError(f"PDF not found: {path}")
    if path.suffix.lower() != ".pdf":
        raise ValueError(f"Expected a .pdf file: {path}")

    try:
        from pypdf import PdfReader
    except ImportError as exc:  # pragma: no cover - depends on local environment
        raise RuntimeError(
            "pypdf is required for PDF extraction. Install requirements.txt first."
        ) from exc

    extraction_warnings: list[str] = []
    pdf_digest = sha256(path.read_bytes()).hexdigest()
    try:
        reader = PdfReader(str(path))
    except Exception as exc:  # pragma: no cover - pypdf error types vary
        raise RuntimeError(f"Could not read PDF: {path}") from exc

    raw_pages: list[dict[str, Any]] = []
    for page_index, page in enumerate(reader.pages, start=1):
        provenance: dict[str, Any] = {}
        try:
            raw_text = _extract_page_text(page, provenance)
        except Exception as exc:  # pragma: no cover - depends on PDF internals
            raw_text = ""
            extraction_warnings.append(
                f"Page {page_index}: text extraction failed: {exc}"
            )
        raw_pages.append(
            {
                "page_number": page_index,
                "raw_text": raw_text,
                "extractor": "pypdf",
                "quality_score": _page_text_quality_score(raw_text),
                "method": provenance.get("method", "extraction_failed"),
            }
        )

    pypdf_pages = list(raw_pages)
    pymupdf_pages = _extract_pymupdf_pages(path, extraction_warnings)
    if len(pymupdf_pages) == len(raw_pages):
        raw_pages = _select_best_page_texts(raw_pages, pymupdf_pages, extraction_warnings)
    elif pymupdf_pages:
        extraction_warnings.append(
            "PyMuPDF extraction page count did not match pypdf; retained pypdf extraction for source integrity."
        )

    repetitive_lines = _detect_repetitive_margin_lines(
        [page["raw_text"] for page in raw_pages]
    )

    pages: list[dict[str, Any]] = []
    for page in raw_pages:
        cleaned_text = normalize_page_text(page["raw_text"], repetitive_lines)
        pages.append(
            {
                "page_number": page["page_number"],
                "text": cleaned_text,
                "raw_text": page["raw_text"],
                "extractor": page.get("extractor", "pypdf"),
                "quality_score": page.get("quality_score"),
            }
        )

    joined_text = "\n\n".join(page["text"] for page in pages if page["text"].strip())
    # Article identity must come from front matter, never a cited paper later
    # in the body/reference list. The general hint parser remains reusable.
    reference_metadata = extract_document_reference_metadata(raw_pages, path.name)
    if pymupdf_pages:
        reference_metadata = _select_reference_metadata(
            [reference_metadata, extract_document_reference_metadata(pymupdf_pages, path.name)],
            extraction_warnings,
        )
    full_text = clean_article_text(joined_text)
    extraction_warnings.extend(_quality_warnings(raw_pages, full_text))

    result = {
        "file_name": path.name,
        "page_count": len(raw_pages),
        "extraction_strategy": "hybrid_pypdf_pymupdf" if pymupdf_pages else "pypdf",
        "extractor_versions": _extractor_versions(bool(pymupdf_pages)),
        "page_extractors": [
            {
                "page_number": page["page_number"],
                "extractor": page.get("extractor", "pypdf"),
                "quality_score": page.get("quality_score"),
            }
            for page in raw_pages
        ],
        "raw_full_text": _joined_raw_text(raw_pages),
        "full_text": full_text,
        "reference_metadata": reference_metadata,
        "pages": pages,
        "extraction_warnings": extraction_warnings,
    }
    visual_by_page = {p["page_number"]: p.get("has_visual_content", False) for p in pymupdf_pages}
    adaptive_blockers = []
    for page in raw_pages:
        adaptive_blockers.extend(assess_page_text(
            str(page.get("raw_text") or ""), int(page["page_number"]),
            bool(visual_by_page.get(page["page_number"], False)),
        ))
    result["adaptive_blockers"] = adaptive_blockers
    if sha256(path.read_bytes()).hexdigest() != pdf_digest:
        raise PdfExtractionError("PDF bytes changed during extraction.")
    evidence = build_evidence_document(
        result, pdf_sha256=pdf_digest, candidates=pypdf_pages + pymupdf_pages,
        configuration={"pypdf_method": "layout_with_plain_fallback", "pymupdf_method": "sorted_with_native_fallback",
                       "geometry_method": "native_dict_no_images_v1"},
    )
    _bind_reference_spans(reference_metadata, evidence)
    result["evidence"] = evidence.model_dump(mode="json")
    plan = build_ingestion_plan(result)
    result["adaptive_ingestion"] = plan.model_dump(exclude={"batches"}) | {
        "batch_count": len(plan.batches),
        "batches": [b.model_dump(exclude={"text"}) for b in plan.batches],
    }
    extraction_warnings.extend(adaptive_blockers)
    try:
        validate_extracted_text_for_generation(result)
    except PdfExtractionError as exc:
        # Retain diagnostics for an explicit inspection route, never usable text.
        exc.extraction = result
        raise
    return result



def validate_extracted_text_for_generation(extraction: dict[str, Any]) -> None:
    """Fail before generation when extraction produced no usable source text."""
    page_count = int(extraction.get("page_count") or 0)
    pages = extraction.get("pages") if isinstance(extraction.get("pages"), list) else []
    raw_full_text = str(extraction.get("raw_full_text") or "").strip()
    full_text = str(extraction.get("full_text") or "").strip()

    if page_count <= 0:
        raise PdfExtractionError("EXTRACTION_FAILED: PDF contains no readable pages.")

    raw_pages_with_text = [
        page for page in pages
        if isinstance(page, dict) and str(page.get("raw_text") or "").strip()
    ]
    if not raw_pages_with_text and not raw_full_text:
        raise PdfExtractionError(
            "EXTRACTION_FAILED: No machine-readable text was extracted from the PDF; "
            "the PDF may be scanned or image-only and requires OCR before clinical chunk generation."
        )

    if not full_text:
        raise PdfExtractionError(
            "EXTRACTION_FAILED: Extracted PDF text is empty after conservative cleaning; "
            "chunk generation was not attempted."
        )


def clean_article_text(text: str) -> str:
    """Conservatively clean article text before LLM chunk generation."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = SPACED_LETTER_ARTIFACT_RE.sub(" ", text)

    reference_match = REFERENCE_HEADING_RE.search(text)
    if reference_match:
        text = text[: reference_match.start()]

    paragraphs: list[str] = []
    for paragraph in re.split(r"\n\s*\n+", text):
        paragraph = re.sub(r"\s+", " ", paragraph).strip()
        if not paragraph:
            continue
        paragraph = _strip_inline_noise(paragraph)
        if not paragraph:
            continue
        if _is_boilerplate_paragraph(paragraph):
            continue
        paragraphs.append(paragraph)

    return "\n\n".join(paragraphs).strip()


def extract_reference_metadata(text: str, source_file: str = "") -> dict[str, str]:
    """Extract verified citation hints without inventing missing fields."""
    normalized = _normalize_for_citation_matching(text)
    head = normalized[:12000]
    doi_match = DOI_RE.search(normalized)
    nejm_citation_match = NEJM_CITATION_RE.search(head)
    citation_match = CITATION_RE.search(head)
    volume_issue_match = VOLUME_ISSUE_RE.search(head)

    doi = _clean_doi_match(doi_match.group(0)) if doi_match else ""
    if not doi:
        doi = _recover_elsevier_pii_doi(normalized, source_file)
    if nejm_citation_match:
        citation = _format_nejm_citation(nejm_citation_match)
    elif citation_match:
        citation = citation_match.group(0).rstrip(".")
    else:
        citation = ""
    reference = ""
    if citation and doi:
        reference = f"{citation}. doi:{doi}"
    elif citation:
        reference = citation
    elif doi:
        reference = f"doi:{doi}"

    return {
        "source_file": source_file,
        "citation": citation,
        "doi": doi,
        "reference": reference,
        "article_title": extract_article_title(head),
        "journal": "N Engl J Med" if nejm_citation_match else _journal_from_citation(citation),
        "publication_year": nejm_citation_match.group("year") if nejm_citation_match else _year_from_citation(citation),
        "volume": _compact_digits(
            nejm_citation_match.group("volume") if nejm_citation_match else (
                volume_issue_match.group("volume") if volume_issue_match else ""
            )
        ),
        "issue": (
            nejm_citation_match.group("issue") or ""
            if nejm_citation_match
            else (volume_issue_match.group("issue") if volume_issue_match else "")
        ),
        "pages": _normalize_pages(nejm_citation_match.group("pages")) if nejm_citation_match else _pages_from_citation(citation),
    }


def extract_document_reference_metadata(pages: list[dict[str, Any]], source_file: str = "") -> dict[str, Any]:
    """Restrict identity hints to two front-matter pages, before references.

    If the article has no DOI in front matter, leave it unknown. A missing
    citation is reviewable; a DOI copied from an unrelated cited paper is not.
    """
    front = _joined_raw_text(pages[:2])
    match = REFERENCE_HEADING_RE.search(front)
    if match:
        front = front[:match.start()]
    metadata: dict[str, Any] = extract_reference_metadata(front, source_file)
    candidates: list[dict[str, Any]] = []
    spans: list[dict[str, Any]] = []
    for index, page in enumerate(pages[:2], 1):
        raw = str(page.get("raw_text") or "")
        reference_heading = REFERENCE_HEADING_RE.search(raw)
        limit = reference_heading.start() if reference_heading else len(raw)
        for match in re.finditer(r"10\.\d{4,9}/[ \t]*[-._;()/:A-Z0-9]+", raw[:limit], re.I):
            doi = _clean_doi_match(re.sub(r"/[ \t]+", "/", match.group()))
            if not doi:
                continue
            start_of_line = raw.rfind("\n", 0, match.start()) + 1
            layout_prefix = raw[start_of_line:match.start()]
            prefix = layout_prefix.strip()
            own_label = bool(re.fullmatch(r"(?:(?:https?://)?(?:dx\.)?doi\.org/|doi\s*:?\s*)?", prefix, re.I))
            # A labeled identifier at a separate column's margin is not an
            # inline cited DOI merely because extraction joins both columns.
            own_label = own_label or bool(re.search(
                r"[ \t]{3,}(?:(?:https?://)?(?:dx\.)?doi\.org/|doi\s*:?[ \t]*)$", layout_prefix, re.I))
            # Publisher/manuscript front matter can put the DOI after a
            # complete citation on the same line. This establishes DOI
            # ownership only; an unreadable page-range glyph is not repaired.
            citation_prefix = " ".join(layout_prefix.split())
            own_label = own_label or bool(re.fullmatch(
                r"[A-Z][A-Za-z &.,-]{1,60}\s+(?:19|20)\d{2}"
                r"(?:\s+[A-Za-z]{3,9}\s+\d{1,2})?\s*;\s*\d+"
                r"(?:\s*\(\d+\))?\s*:\s*\d+[-–\ufffd]\d+\s*\.\s*"
                r"(?:https?://(?:dx\.)?doi\.org/|doi\s*:\s*)",
                citation_prefix, re.I))
            span = {"page_number": page.get("page_number", index),
                    "buffer_id": f"page:{page.get('page_number', index)}:raw_text",
                    "extractor": page.get("extractor"),
                    "char_start": match.start(), "char_end": match.end(), "text": match.group()}
            spans.append(span)
            candidates.append(dict(span, doi=doi, focal_label=own_label))
        offset = 0
        for line in raw[:limit].splitlines(keepends=True):
            normalized = _normalize_for_citation_matching(line)
            if CITATION_RE.search(normalized) or NEJM_CITATION_RE.search(normalized):
                spans.append({"page_number": page.get("page_number", index),
                              "buffer_id": f"page:{page.get('page_number', index)}:raw_text",
                              "extractor": page.get("extractor"),
                              "char_start": offset, "char_end": offset + len(line), "text": line})
            offset += len(line)
        if reference_heading:
            break
    distinct = {candidate["doi"].lower() for candidate in candidates}
    status = "source_supported" if metadata.get("citation") or any(c["focal_label"] for c in candidates) else "unresolved"
    if len(distinct) > 1:
        status = "conflict"
    if metadata.get("doi") and not any(c["focal_label"] for c in candidates):
        # A cited identifier next to an article citation is still not evidence
        # that the identifier belongs to that article.
        metadata["doi"] = ""
        metadata["reference"] = metadata.get("citation", "")
    if status != "source_supported":
        metadata.update(reference="", citation="", doi="")
    elif not metadata.get("reference"):
        focal = {c["doi"] for c in candidates if c["focal_label"]}
        if len(focal) == 1:
            doi = next(iter(focal))
            metadata.update(doi=doi, reference=f"doi:{doi}")
    if status == "source_supported" and "\ufffd" in metadata.get("citation", ""):
        focal = {c["doi"] for c in candidates if c["focal_label"]}
        if len(focal) == 1:
            doi = next(iter(focal))
            metadata.update(reference=f"doi:{doi}", citation="", doi=doi,
                            journal="", publication_year="", volume="", issue="", pages="",
                            bibliography_status="review_required")
    metadata.update(identity_status=status, citation_candidates=candidates, source_spans=spans)
    return metadata


def _bind_reference_spans(metadata: dict[str, Any], evidence: EvidenceDocument) -> None:
    """Resolve P0 page-local citation aliases to their actual native candidate."""
    for key in ("source_spans", "citation_candidates"):
        for item in metadata.get(key, []):
            page = next((p for p in evidence.pages if p.page_number == item["page_number"]), None)
            matches = []
            for candidate in page.candidates if page else []:
                if item.get("extractor") and candidate.extractor != item["extractor"]:
                    continue
                text = evidence.buffers[candidate.buffer_id].text
                if 0 <= item["char_start"] <= item["char_end"] <= len(text) and text[item["char_start"]:item["char_end"]] == item["text"]:
                    matches.append(candidate.buffer_id)
            if not matches:
                raise PdfExtractionError("Citation source span does not match any retained native candidate.")
            item.setdefault("legacy_buffer_id", item["buffer_id"])
            item["buffer_id"] = matches[0]
            item["matching_buffer_ids"] = matches
    for candidate in metadata.get("conflicting_candidates", []):
        _bind_reference_spans(candidate, evidence)


def _normalize_for_citation_matching(text: str) -> str:
    """Collapse layout extraction artifacts for citation and DOI matching.

    PDF layout extraction often splits characters with whitespace, producing
    artifacts like ``N Engl J Me d 20 0 9;36 0 :2 277- 8 8``.  This function
    collapses isolated single digits and repairs known journal-name spacing
    so that the standard citation and DOI regexes can match.
    """
    # Protect complete identifiers before repairing citation digits. Neither a
    # following page number nor another line may be appended to a DOI suffix.
    text = re.sub(r"(10\.\d{4,9}/)[ \t]+([A-Z0-9])", r"\1\2", text, flags=re.I)
    identifiers: list[str] = []
    def protect(match: re.Match[str]) -> str:
        identifiers.append(match.group())
        return f"DOIPROTECTED{chr(65 + len(identifiers) - 1)}TOKEN"
    text = DOI_RE.sub(protect, text)
    # Collapse spaced citation digits within a line only.
    # Applied iteratively because adjacent single digits may need multiple passes.
    for _ in range(6):
        collapsed = re.sub(r"(\d)[ \t]+(\d)", r"\1\2", text)
        if collapsed == text:
            break
        text = collapsed
    # Repair "Me d" → "Med" specifically in journal name context.
    text = re.sub(r"\bMe\s+d\b", "Med", text)
    # Repair DOI fragments split by PDF layout extraction:
    # "10.1016/  S2468-1253..." → "10.1016/S2468-1253..."
    text = re.sub(r"(10\.\d{4,9}/)\s+([A-Z0-9])", r"\1\2", text, flags=re.IGNORECASE)
    # Repair spaced single-letter fragments adjacent to short tokens that
    # commonly appear in broken DOIs and citation components:
    # "NEJMoa09 08721" → "NEJMoa0908721" (already handled by digit collapse).
    for index, doi in enumerate(identifiers):
        text = text.replace(f"DOIPROTECTED{chr(65 + index)}TOKEN", doi)
    return text


def _clean_doi_match(value: str) -> str:
    doi = value.strip().rstrip(").,;")
    if "/" not in doi:
        return ""
    suffix = doi.split("/", 1)[1]
    if not any(char.isdigit() for char in suffix):
        return ""
    return doi


def _recover_elsevier_pii_doi(text: str, source_file: str) -> str:
    """Recover Lancet Gastroenterology & Hepatology PII-style DOI fragments.

    Some Elsevier/Lancet PDFs extract as ``doi.org/10.1016/`` separated from
    the ``S2468-1253(...)`` suffix by layout text. Only recover this narrow
    DOI form when both the DOI prefix and a matching PII identity are present.
    """
    if "10.1016/" not in text:
        return ""
    text_pii = _find_lancet_gastro_pii(text)
    file_pii = _find_lancet_gastro_pii(source_file)
    if not text_pii:
        return ""
    if text_pii and file_pii and text_pii != file_pii:
        return ""
    return f"10.1016/{_format_lancet_gastro_pii(text_pii)}"


def _find_lancet_gastro_pii(text: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]+", "", text).upper()
    match = re.search(r"S24681253\d{8}", compact)
    return match.group(0) if match else ""


def _format_lancet_gastro_pii(pii: str) -> str:
    return f"{pii[:5]}-{pii[5:9]}({pii[9:11]}){pii[11:16]}-{pii[16:]}"


def _joined_raw_text(pages: list[dict[str, Any]]) -> str:
    return "\n\n".join(str(page.get("raw_text") or "") for page in pages)


def _extract_pymupdf_pages(
    path: Path, extraction_warnings: list[str]
) -> list[dict[str, Any]]:
    """Extract page text with PyMuPDF when available for quality comparison."""
    try:
        import fitz
    except ImportError:
        return []
    try:
        document = fitz.open(str(path))
    except Exception as exc:  # pragma: no cover - depends on PDF internals
        extraction_warnings.append(f"PyMuPDF extraction unavailable: {exc}")
        return []
    try:
        pages = []
        for index, page in enumerate(document):
            method = "get_text_sorted"
            try:
                page_text = page.get_text(sort=True) or ""
                if len(page_text) > 200000:
                    plain = page.get_text(sort=False)
                    if plain and plain.strip():
                        page_text = plain
                        method = "get_text_native_inflation_fallback"
                        extraction_warnings.append(f"Page {index + 1}: inflated PyMuPDF sorted layout replaced with native text.")
                has_visual_content = False
                if len("".join(page_text.split())) < 200:
                    has_visual_content = bool(page.get_images() or page.get_drawings())
            except Exception as exc:
                extraction_warnings.append(f"Page {index + 1}: PyMuPDF extraction failed: {type(exc).__name__}")
                page_text, has_visual_content = "", False
                method = "extraction_failed"
            pages.append({
                "page_number": index + 1,
                "raw_text": page_text,
                "extractor": "pymupdf",
                "quality_score": _page_text_quality_score(page_text),
                "has_visual_content": has_visual_content,
                "method": method,
                "layout": _native_page_layout(page, page_text, extraction_warnings, index + 1),
            })
        return pages
    finally:
        document.close()


def _native_page_layout(page: Any, text: str, warnings: list[str], number: int) -> dict[str, Any]:
    """Retain native geometry without interpreting visual or table relationships."""
    try:
        import fitz
        data = page.get_text("dict", sort=False, flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
        if not isinstance(data, dict):
            raise ValueError("Native dictionary unavailable")
        blocks = []
        for index, block in enumerate(data.get("blocks", [])):
            lines = block.get("lines", [])
            blocks.append({"text": "\n".join("".join(s.get("text", "") for s in line.get("spans", [])) for line in lines),
                           "bbox": list(block["bbox"]), "native_order": index,
                           "direction": list(lines[0].get("dir", (1, 0))) if lines else None,
                           "lines": [{"text": "".join(s.get("text", "") for s in line.get("spans", [])),
                                      "bbox": list(line["bbox"]) if line.get("bbox") is not None else None,
                                      "direction": list(line["dir"]) if line.get("dir") is not None else None} for line in lines]})
        layout_warnings = []
        for inventory, bbox_key, kind in ((page.get_image_info, "bbox", "image"), (page.get_drawings, "rect", "drawing")):
            try:
                for visual in inventory():
                    blocks.append({"bbox": list(visual[bbox_key]), "native_order": len(blocks), "role": "visual_region", "signal_kind": kind})
            except Exception:
                layout_warnings.append("visual_inventory_unavailable")
        return {"width": float(page.rect.width), "height": float(page.rect.height),
                "rotation": int(page.rotation), "blocks": blocks, "warnings": layout_warnings}
    except Exception as exc:
        warnings.append(f"Page {number}: native geometry unavailable: {type(exc).__name__}")
        return {}


def _select_best_page_texts(
    pypdf_pages: list[dict[str, Any]],
    pymupdf_pages: list[dict[str, Any]],
    extraction_warnings: list[str],
) -> list[dict[str, Any]]:
    """Use the cleaner page extraction when pypdf layout text is visibly degraded."""
    selected: list[dict[str, Any]] = []
    for pypdf_page, pymupdf_page in zip(pypdf_pages, pymupdf_pages):
        pypdf_text = str(pypdf_page.get("raw_text") or "")
        pymupdf_text = str(pymupdf_page.get("raw_text") or "")
        if _should_use_pymupdf_page(pypdf_text, pymupdf_text):
            page_number = int(pypdf_page.get("page_number") or pymupdf_page.get("page_number") or len(selected) + 1)
            extraction_warnings.append(
                f"Page {page_number}: pypdf layout text replaced with PyMuPDF text because pypdf output appeared degraded."
            )
            selected.append(
                {
                    "page_number": page_number,
                    "raw_text": pymupdf_text,
                    "extractor": "pymupdf",
                    "quality_score": _page_text_quality_score(pymupdf_text),
                    "fallback_from": "pypdf",
                }
            )
        else:
            selected.append(pypdf_page)
    return selected


def _extractor_versions(include_pymupdf: bool) -> dict[str, str]:
    versions = {"pypdf": _package_version("pypdf")}
    if include_pymupdf:
        versions["pymupdf"] = _package_version("PyMuPDF")
    return versions


def _package_version(package_name: str) -> str:
    try:
        return importlib_metadata.version(package_name)
    except importlib_metadata.PackageNotFoundError:
        return ""


def _should_use_pymupdf_page(pypdf_text: str, pymupdf_text: str) -> bool:
    if not pymupdf_text.strip():
        return False
    if not pypdf_text.strip():
        return True
    pypdf_score = _page_text_quality_score(pypdf_text)
    pymupdf_score = _page_text_quality_score(pymupdf_text)
    return pymupdf_score > pypdf_score + 5


def _page_text_quality_score(text: str) -> float:
    space_fraction = text.count(" ") / max(1, len(text))
    whitespace_penalty = 50 if len(text) > 10000 and space_fraction > .95 else 0
    # Run regex-based quality checks on compact spacing to avoid quadratic
    # backtracking across synthetic layout padding. Source text is unchanged.
    text = WHITESPACE_RE.sub(" ", text)
    artifact_penalty = len(SPACED_LETTER_ARTIFACT_RE.findall(text)) * 8
    font_code_penalty = len(FONT_CODE_ARTIFACT_RE.findall(text)) * 8
    single_letter_penalty = _single_letter_token_ratio(text) * 30
    replacement_penalty = text.count("\ufffd") * 0.5
    citation_bonus = 12 if NEJM_CITATION_RE.search(_normalize_for_citation_matching(text)) else 0
    doi_bonus = 8 if DOI_RE.search(_normalize_for_citation_matching(text)) else 0
    # Layout extraction can emit megabytes of spaces for unusual PDF fonts.
    # Whitespace inflation must not make that candidate score as richer text.
    length_score = min(len(text.strip()) / 1000, 40)
    return (
        length_score + citation_bonus + doi_bonus
        - artifact_penalty - font_code_penalty - single_letter_penalty - replacement_penalty - whitespace_penalty
    )


def _select_reference_metadata(
    candidates: list[dict[str, Any]], extraction_warnings: list[str]
) -> dict[str, Any]:
    usable = [candidate for candidate in candidates if candidate.get("reference")]
    if not usable:
        return candidates[0] if candidates else {}
    compatible: list[dict[str, str]] = []
    for candidate in usable:
        if all(_reference_metadata_compatible(candidate, other) for other in compatible):
            compatible.append(candidate)
        else:
            # Different rendered journal names or damaged citation glyphs
            # need not invalidate a uniquely labelled focal DOI. Retain only
            # that exact source identifier; disputed bibliography stays held.
            focal = {c["doi"].lower(): c["doi"] for m in candidates
                     for c in m.get("citation_candidates", []) if c.get("focal_label")}
            if len(focal) == 1 and not any(
                    m.get("identity_status") == "conflict" or
                    (m.get("doi") and m["doi"].lower() not in focal) for m in candidates):
                doi = next(iter(focal.values()))
                owner = next(m for m in candidates if any(
                    c.get("focal_label") and c["doi"].lower() == doi.lower()
                    for c in m.get("citation_candidates", [])))
                extraction_warnings.append(
                    "Citation bibliography differs across extractors; using only the uniquely labelled source DOI. Bibliography requires review.")
                return dict(owner, reference=f"doi:{doi}", citation="", doi=doi,
                            journal="", publication_year="", volume="", issue="", pages="",
                            identity_status="source_supported", bibliography_status="review_required",
                            conflicting_candidates=candidates)
            extraction_warnings.append(
                "Conflicting citation metadata detected across extractors; source identity requires review."
            )
            return dict(compatible[0], reference="", doi="", citation="", identity_status="conflict",
                        conflicting_candidates=candidates)
    if not compatible:
        return candidates[0]
    compatible.sort(key=_reference_metadata_score, reverse=True)
    return compatible[0]


def _reference_metadata_compatible(left: dict[str, str], right: dict[str, str]) -> bool:
    for field in ("publication_year", "volume", "pages", "journal", "issue"):
        left_value = " ".join(str(left.get(field, "")).split()).casefold()
        right_value = " ".join(str(right.get(field, "")).split()).casefold()
        if left_value and right_value and left_value != right_value:
            return False
    left_doi = str(left.get("doi", "")).strip().lower()
    right_doi = str(right.get("doi", "")).strip().lower()
    if left_doi and right_doi and left_doi != right_doi:
        return False
    return True


def _reference_metadata_score(metadata: dict[str, str]) -> int:
    score = 0
    for field in ("reference", "citation", "journal", "publication_year", "volume", "pages"):
        if str(metadata.get(field, "")).strip():
            score += 1
    if str(metadata.get("doi", "")).strip():
        score += 2
    return score


def extract_article_title(text: str) -> str:
    """Extract a plausible article title from verified PDF header text."""
    candidates = []
    for paragraph in re.split(r"\n\s*\n+", text[:5000]):
        cleaned = re.sub(r"\s+", " ", paragraph).strip(" .")
        if not cleaned:
            continue
        author_start = re.search(r"\s+[A-Z][A-Za-z�.'-]+(?:\s+[A-Z]\.)?\s+[A-Z][A-Za-z�.'-]+,\s*M\.D\.", cleaned)
        if author_start and author_start.start() >= 12:
            cleaned = cleaned[: author_start.start()].strip(" .")
        normalized = _line_key(cleaned)
        if SPACED_LETTER_ARTIFACT_RE.search(cleaned) or _single_letter_token_ratio(cleaned) > 0.35:
            continue
        if normalized in {
            "original article",
            "or igina l a r t icl e",
            "abstract",
            "a b s t r a c t",
            "the new england journal of medicine",
        }:
            continue
        if normalized.startswith(("from ", "established in", "copyright ")):
            continue
        if re.search(r"\b(M\.D\.|Ph\.D\.|D\.O\.|Pharm\.D\.)\b", cleaned):
            continue
        if len(cleaned.split()) < 3 or len(cleaned) > 180:
            continue
        candidates.append(cleaned)
    if len(candidates) >= 2 and (candidates[0].endswith(("versus", "for", "with")) or candidates[1].lower().startswith(("for ", "with "))):
        return f"{candidates[0]} {candidates[1]}".strip()
    return candidates[0] if candidates else ""


def _format_nejm_citation(match: re.Match[str]) -> str:
    year = match.group("year")
    volume = _compact_digits(match.group("volume"))
    pages = _normalize_pages(match.group("pages"))
    return f"N Engl J Med. {year};{volume}:{pages}"


def _compact_digits(value: str) -> str:
    return re.sub(r"\s+", "", value or "")


def _normalize_pages(value: str) -> str:
    return re.sub(r"\s+", "", (value or "").replace("–", "-")).rstrip(".")


def _journal_from_citation(citation: str) -> str:
    match = re.match(r"\s*([^.;]+(?:\s+[A-Za-z][A-Za-z.-]+)*)\s+\d{4}\s*;", citation)
    return match.group(1).strip() if match else ""


def _year_from_citation(citation: str) -> str:
    match = re.search(r"\b(19|20)\d{2}\b", citation)
    return match.group(0) if match else ""


def _pages_from_citation(citation: str) -> str:
    match = re.search(r":\s*([A-Za-z]?\d+\s*[-–]\s*\d+)", citation)
    return _normalize_pages(match.group(1)) if match else ""


def _single_letter_token_ratio(text: str) -> float:
    tokens = re.findall(r"[A-Za-z]+", text)
    if not tokens:
        return 0.0
    return sum(1 for token in tokens if len(token) == 1) / len(tokens)


def _extract_page_text(page: Any, provenance: dict[str, Any] | None = None) -> str:
    """Extract page text, preferring layout mode when pypdf supports it."""
    method = "extract_text_layout"
    try:
        text = page.extract_text(extraction_mode="layout")
    except Exception:
        text = page.extract_text()
        method = "extract_text_plain_layout_failure"
    if text and len(text) > 200000:
        # Some unusual fonts make layout mode synthesize millions of spaces.
        # Retain the same PDF's plain extraction instead of feeding that
        # synthetic whitespace into cleaning and quality comparison.
        plain = page.extract_text()
        if plain and plain.strip():
            text = plain
            method = "extract_text_plain_inflation_fallback"
    if provenance is not None:
        provenance["method"] = method
    return text or ""


def normalize_page_text(raw_text: str, repetitive_lines: set[str] | None = None) -> str:
    """Normalize one page without aggressive scientific-content changes."""
    repetitive_lines = repetitive_lines or set()
    raw_text = raw_text.replace("\r\n", "\n").replace("\r", "\n")
    raw_text = raw_text.replace("\u00a0", " ")
    lines = raw_text.split("\n")

    normalized_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        comparable = _line_key(stripped)
        if not stripped:
            normalized_lines.append("")
            continue
        if PAGE_NUMBER_RE.match(stripped):
            continue
        if comparable in repetitive_lines:
            continue
        normalized_lines.append(stripped)

    return _join_wrapped_lines(normalized_lines)


def _join_wrapped_lines(lines: list[str]) -> str:
    paragraphs: list[str] = []
    current = ""

    for line in lines:
        if not line:
            _flush(paragraphs, current)
            current = ""
            continue

        if _looks_table_like(line):
            _flush(paragraphs, current)
            current = ""
            paragraphs.append(line.rstrip())
            continue

        clean_line = WHITESPACE_RE.sub(" ", line).strip()
        if not current:
            current = clean_line
            continue

        if current.endswith("-") and clean_line[:1].islower():
            current = f"{current[:-1]}{clean_line}"
        elif _looks_like_heading(clean_line):
            _flush(paragraphs, current)
            current = clean_line
        else:
            current = f"{current} {clean_line}"

    _flush(paragraphs, current)
    return "\n\n".join(paragraphs)


def _flush(paragraphs: list[str], text: str) -> None:
    text = text.strip()
    if text:
        paragraphs.append(text)


def _detect_repetitive_margin_lines(raw_pages: list[str]) -> set[str]:
    if len(raw_pages) < 3:
        return set()

    counter: Counter[str] = Counter()
    for raw_text in raw_pages:
        candidates = _margin_candidates(raw_text)
        counter.update(candidates)

    threshold = max(2, math.ceil(len(raw_pages) * 0.5))
    return {
        line
        for line, count in counter.items()
        if count >= threshold and 4 <= len(line) <= 140
    }


def _margin_candidates(raw_text: str) -> set[str]:
    lines = [_line_key(line.strip()) for line in raw_text.splitlines() if line.strip()]
    lines = [line for line in lines if not PAGE_NUMBER_RE.match(line)]
    if not lines:
        return set()
    return set(lines[:2] + lines[-2:])


def _line_key(line: str) -> str:
    return WHITESPACE_RE.sub(" ", line.strip()).lower()


def _is_boilerplate_paragraph(paragraph: str) -> bool:
    lower = paragraph.lower()
    if BOILERPLATE_RE.search(paragraph):
        return True
    if lower.startswith(("author affiliations", "contributors", "funding")):
        return True
    if len(paragraph.split()) <= 8 and DOI_RE.search(paragraph):
        return True
    return False


def _strip_inline_noise(paragraph: str) -> str:
    """Remove inline web noise without dropping adjacent clinical evidence."""
    paragraph = URL_RE.sub("", paragraph)
    paragraph = re.sub(r"\(\s*\)", "", paragraph)
    paragraph = re.sub(r"\s+", " ", paragraph)
    return paragraph.strip()


def _looks_table_like(line: str) -> bool:
    if "\t" in line or "|" in line:
        return True
    if re.search(r"\S\s{3,}\S", line):
        return True
    numeric_tokens = re.findall(r"\b\d+(?:\.\d+)?%?\b", line)
    return len(numeric_tokens) >= 4 and len(line.split()) <= 18


def _looks_like_heading(line: str) -> bool:
    words = line.split()
    if len(words) > 12 or len(line) > 90:
        return False
    if line.endswith((".", ",", ";", ":")):
        return False
    uppercase_letters = sum(1 for char in line if char.isupper())
    letters = sum(1 for char in line if char.isalpha())
    return bool(letters) and uppercase_letters / max(letters, 1) > 0.45


def _quality_warnings(raw_pages: list[dict[str, Any]], full_text: str) -> list[str]:
    warnings: list[str] = []
    page_count = len(raw_pages)
    if page_count == 0:
        warnings.append("PDF contains no readable pages.")
        return warnings

    empty_pages = [
        str(page["page_number"])
        for page in raw_pages
        if not str(page["raw_text"]).strip()
    ]
    if empty_pages:
        warnings.append(f"No text extracted from pages: {', '.join(empty_pages)}.")

    chars = len(full_text.strip())
    if chars == 0:
        warnings.append("No machine-readable text was extracted from the PDF.")
    elif chars / page_count < 250:
        warnings.append(
            "Low text density detected; the PDF may be scanned or extraction quality may be poor."
        )

    replacement_count = full_text.count("\ufffd")
    if replacement_count:
        warnings.append(
            f"Detected {replacement_count} replacement characters; text encoding may be degraded."
        )
    font_code_count = len(FONT_CODE_ARTIFACT_RE.findall(full_text))
    if font_code_count:
        warnings.append(
            f"Detected {font_code_count} unresolved PDF font codes; affected symbols and numerical context require review."
        )

    return warnings

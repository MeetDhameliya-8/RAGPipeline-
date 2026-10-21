from pathlib import Path
import json
import unittest

from src.chunk_generator import ChunkGenerationError, generate_chunks
from src.pdf_extractor import (
    _select_reference_metadata,
    _should_use_pymupdf_page,
    extract_pdf,
)


class Task11RegressionTests(unittest.TestCase):
    def test_five_nejm_fixture_references_are_detected(self) -> None:
        expected_references = {
            "NEJMoa0808145.pdf": "N Engl J Med. 2009;360:2277-88",
            "NEJMoa0903230.pdf": "N Engl J Med. 2010;362:129-37",
            "NEJMoa0904492.pdf": "N Engl J Med. 2010;362:1383-95",
            "NEJMoa0908721.pdf": "N Engl J Med. 2010;362:1273-81. doi:10.1056/NEJMoa0908721",
            "NEJMoa0908821.pdf": "N Engl J Med. 2010;362:1491-502",
        }
        fixture_root = Path("input_pdfs")
        for filename, expected_reference in expected_references.items():
            pdf_path = fixture_root / filename
            if not pdf_path.exists():
                self.skipTest(f"Missing fixture PDF: {pdf_path}")
            with self.subTest(filename=filename):
                extraction = extract_pdf(pdf_path)

                self.assertEqual(
                    extraction["reference_metadata"]["reference"], expected_reference
                )
                self.assertEqual(extraction["extraction_strategy"], "hybrid_pypdf_pymupdf")
                self.assertEqual(len(extraction["page_extractors"]), extraction["page_count"])

    def test_abc02_page_four_preserves_treatment_compliance_context(self) -> None:
        pdf_path = Path("input_pdfs") / "NEJMoa0908721.pdf"
        if not pdf_path.exists():
            self.skipTest(f"Missing fixture PDF: {pdf_path}")

        extraction = extract_pdf(pdf_path)
        page_four = next(page for page in extraction["pages"] if page["page_number"] == 4)
        evidence_blob = "\n".join(
            [page_four["raw_text"], page_four["text"], extraction["full_text"]]
        )

        self.assertIn("66.5", evidence_blob)
        self.assertIn("73.5", evidence_blob)
        self.assertIn("three cycles of gemcitabine alone", evidence_blob)
        self.assertIn("four cycles of cisplatin plus gem", evidence_blob)
        self.assertEqual(page_four["extractor"], "pymupdf")

    def test_hybrid_selection_prefers_clean_page_over_spaced_layout_artifacts(self) -> None:
        degraded = (
            "T r e a t m e n t   c o m p l i a n c e   w a s   r e p o r t e d. "
            "T h r e e   c y c l e s   6 6 . 5 %   a n d   f o u r   c y c l e s   7 3 . 5 %. "
        ) * 6
        clean = (
            "Treatment compliance was reported. Three cycles were completed by 66.5% "
            "with gemcitabine alone and four cycles by 73.5% with cisplatin plus gemcitabine. "
        ) * 6

        self.assertTrue(_should_use_pymupdf_page(degraded, clean))

    def test_hybrid_selection_keeps_pypdf_when_fallback_has_no_text(self) -> None:
        pypdf_text = "Treatment outcome text with DOI 10.1056/NEJMoa0000000. " * 8

        self.assertFalse(_should_use_pymupdf_page(pypdf_text, ""))

    def test_reference_metadata_selection_warns_and_does_not_merge_conflicts(self) -> None:
        warnings: list[str] = []
        pypdf_metadata = {
            "reference": "N Engl J Med. 2010;362:1273-81",
            "citation": "N Engl J Med. 2010;362:1273-81",
            "doi": "",
            "publication_year": "2010",
            "volume": "362",
            "pages": "1273-81",
        }
        conflicting_metadata = {
            "reference": "N Engl J Med. 2010;363:1273-81. doi:10.1056/NEJMoa9999999",
            "citation": "N Engl J Med. 2010;363:1273-81",
            "doi": "10.1056/NEJMoa9999999",
            "publication_year": "2010",
            "volume": "363",
            "pages": "1273-81",
        }

        selected = _select_reference_metadata(
            [pypdf_metadata, conflicting_metadata], warnings
        )

        self.assertEqual(selected["reference"], "")
        self.assertEqual(selected["identity_status"], "conflict")
        self.assertEqual(warnings, [
            "Conflicting citation metadata detected across extractors; source identity requires review."
        ])

    def test_missing_chunk_reference_is_bound_from_verified_source_identity(self) -> None:
        document = _valid_generated_document()
        document["chunks"][0].pop("reference")

        generated = generate_chunks(
            _verified_extraction(), provider=_StaticProvider(json.dumps(document))
        )

        self.assertEqual(generated["article"]["reference"], "doi:10.1000/example")
        self.assertEqual(generated["chunks"][0]["reference"], "doi:10.1000/example")

    def test_blank_chunk_reference_is_bound_from_verified_source_identity(self) -> None:
        document = _valid_generated_document()
        document["chunks"][0]["reference"] = ""

        generated = generate_chunks(
            _verified_extraction(), provider=_StaticProvider(json.dumps(document))
        )

        self.assertEqual(generated["chunks"][0]["reference"], "doi:10.1000/example")

    def test_reference_binding_requires_verified_source_identity(self) -> None:
        document = _valid_generated_document()
        document["chunks"][0].pop("reference")
        extraction = _verified_extraction()
        extraction["reference_metadata"] = {}

        with self.assertRaisesRegex(ChunkGenerationError, "verified source reference"):
            generate_chunks(extraction, provider=_StaticProvider(json.dumps(document)))

    def test_reference_binding_does_not_fill_other_missing_fields(self) -> None:
        document = _valid_generated_document()
        document["chunks"][0].pop("original_text")

        with self.assertRaisesRegex(ChunkGenerationError, "original_text"):
            generate_chunks(
                _verified_extraction(), provider=_StaticProvider(json.dumps(document))
            )

    def test_conflicting_generated_reference_is_rejected(self) -> None:
        document = _valid_generated_document()
        document["chunks"][0]["reference"] = "doi:10.9999/conflict"

        with self.assertRaisesRegex(ChunkGenerationError, "DOI"):
            generate_chunks(
                _verified_extraction(), provider=_StaticProvider(json.dumps(document))
            )


class _StaticProvider:
    def __init__(self, output: str) -> None:
        self.output = output

    def generate(self, article_text: str, source_file: str, prompt: str) -> str:
        return self.output


def _verified_extraction() -> dict[str, object]:
    source_text = "Ulcerative colitis treatment context. " * 40
    return {
        "file_name": "paper.pdf",
        "full_text": source_text + " doi:10.1000/example",
        "reference_metadata": {
            "doi": "10.1000/example",
            "reference": "doi:10.1000/example",
        },
    }


def _valid_generated_document() -> dict[str, object]:
    return {
        "article": {
            "source_file": "paper.pdf",
            "topic": "Ulcerative Colitis",
            "subtopic": "Adults With Active Disease",
            "reference": "",
            "number_of_chunks": 1,
        },
        "chunks": [
            {
                "chunk_number": 1,
                "topic": "Ulcerative Colitis",
                "subtopic": "Adults With Active Disease",
                "section": "Does Treatment Improve Clinical Remission?",
                "reference": "",
                "original_text": "Ulcerative colitis treatment context. " * 40,
            }
        ],
    }

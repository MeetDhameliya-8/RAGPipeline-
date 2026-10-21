import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch
from pypdf import PdfWriter

from src.pdf_extractor import (
    PdfExtractionError,
    clean_article_text,
    extract_pdf,
    extract_reference_metadata,
    validate_extracted_text_for_generation,
)
class PdfExtractorTests(unittest.TestCase):
    def test_reference_metadata_uses_doi_outside_first_pages(self) -> None:
        text = "Front matter without citation. " * 500
        text += "Gut 2016;65:47-56. doi:10.1136/gutjnl-2014-308363"

        metadata = extract_reference_metadata(text, "paper.pdf")

        self.assertEqual(metadata["doi"], "10.1136/gutjnl-2014-308363")
        self.assertIn("doi:10.1136/gutjnl-2014-308363", metadata["reference"])

    def test_reference_metadata_repairs_split_doi_after_slash(self) -> None:
        text = (
            "Lancet Gastroenterol Hepatol article. "
            "doi.org/10.1016/  S2468-1253(24)00272-3 "
            "Clinical trial source text."
        )

        metadata = extract_reference_metadata(text, "PIIS2468125324002723.pdf")

        self.assertEqual(metadata["doi"], "10.1016/S2468-1253(24)00272-3")
        self.assertEqual(metadata["reference"], "doi:10.1016/S2468-1253(24)00272-3")

    def test_reference_metadata_recovers_lancet_gastro_pii_doi_when_layout_interleaves_text(self) -> None:
        text = (
            "Lancet Gastroenterol Hepatol source. doi.org/10.1016/ "
            "Methods and other layout text before the identifier. "
            "S2468-1253(16)30003-6 admitted with severe ulcerative colitis."
        )

        metadata = extract_reference_metadata(text, "PIIS2468125316300036.pdf")

        self.assertEqual(metadata["doi"], "10.1016/S2468-1253(16)30003-6")
        self.assertEqual(metadata["reference"], "doi:10.1016/S2468-1253(16)30003-6")

    def test_reference_metadata_does_not_recover_conflicting_lancet_gastro_pii(self) -> None:
        text = (
            "Lancet Gastroenterol Hepatol source. doi.org/10.1016/ "
            "Methods and other layout text before the identifier. "
            "S2468-1253(16)30003-6"
        )

        metadata = extract_reference_metadata(text, "PIIS2468125316300784.pdf")

        self.assertEqual(metadata["doi"], "")
        self.assertEqual(metadata["reference"], "")
    def test_reference_metadata_normalizes_nejm_spaced_volume_citation(self) -> None:
        text = (
            "or igina l a r t icl e\n\n"
            "Sunitinib Malate for the Treatment of Pancreatic Neuroendocrine Tumors\n\n"
            "Eric Raymond, M.D., Ph.D.\n\n"
            "N Engl J Med 2011;36 4:501-13.\n\n"
            "Copyright Massachusetts Medical Society."
        )

        metadata = extract_reference_metadata(text, "NEJMoa1003825.pdf")

        self.assertEqual(metadata["article_title"], "Sunitinib Malate for the Treatment of Pancreatic Neuroendocrine Tumors")
        self.assertEqual(metadata["citation"], "N Engl J Med. 2011;364:501-13")
        self.assertEqual(metadata["reference"], "N Engl J Med. 2011;364:501-13")
        self.assertEqual(metadata["journal"], "N Engl J Med")
        self.assertEqual(metadata["volume"], "364")
        self.assertEqual(metadata["pages"], "501-13")

    def test_clean_article_text_removes_reference_section(self) -> None:
        text = (
            "Title\n\n"
            "Clinically important article body that should remain available.\n\n"
            "References\n\n"
            "1 Hjortswang H, Tysk C, Bohr J, et al."
        )

        cleaned = clean_article_text(text)

        self.assertIn("Clinically important article body", cleaned)
        self.assertNotIn("Hjortswang", cleaned)
    def test_validate_extracted_text_rejects_scanned_image_only_pdf(self) -> None:
        extraction = {
            "page_count": 2,
            "raw_full_text": "",
            "full_text": "",
            "pages": [
                {"page_number": 1, "raw_text": "", "text": ""},
                {"page_number": 2, "raw_text": "", "text": ""},
            ],
        }

        with self.assertRaisesRegex(PdfExtractionError, "scanned or image-only"):
            validate_extracted_text_for_generation(extraction)

    def test_validate_extracted_text_rejects_empty_cleaned_text(self) -> None:
        extraction = {
            "page_count": 1,
            "raw_full_text": "References\n\n1 Example reference only.",
            "full_text": "",
            "pages": [
                {
                    "page_number": 1,
                    "raw_text": "References\n\n1 Example reference only.",
                    "text": "References\n\n1 Example reference only.",
                }
            ],
        }

        with self.assertRaisesRegex(PdfExtractionError, "empty after conservative cleaning"):
            validate_extracted_text_for_generation(extraction)

    def test_extract_pdf_preserves_page_order_and_warnings_with_mock_reader(self) -> None:
        class FakePage:
            def __init__(self, text: str) -> None:
                self.text = text

            def extract_text(self, extraction_mode: str = "layout") -> str:
                return self.text

        class FakeReader:
            def __init__(self, path: str) -> None:
                self.pages = [
                    FakePage("Title\n\nPrimary outcome 12.5% on page one."),
                    FakePage("Results\n\nMortality was 8.2% on page two."),
                ]

        fake_pypdf = types.SimpleNamespace(PdfReader=FakeReader)
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "article.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            with patch.dict(sys.modules, {"pypdf": fake_pypdf}):
                extraction = extract_pdf(pdf_path)

        self.assertEqual(extraction["page_count"], 2)
        self.assertEqual([page["page_number"] for page in extraction["pages"]], [1, 2])
        self.assertIn("Primary outcome 12.5%", extraction["full_text"])
        self.assertIn("Mortality was 8.2%", extraction["full_text"])


    def test_extract_pdf_rejects_zero_page_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "empty.pdf"
            writer = PdfWriter()
            with pdf_path.open("wb") as handle:
                writer.write(handle)

            with self.assertRaisesRegex(PdfExtractionError, "no readable pages"):
                extract_pdf(pdf_path)

    def test_extract_pdf_rejects_corrupt_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "corrupt.pdf"
            pdf_path.write_bytes(b"this is not a valid pdf")

            with self.assertRaisesRegex(RuntimeError, "Could not read PDF"):
                extract_pdf(pdf_path)
    def test_extract_pdf_rejects_mock_scanned_pdf_before_generation(self) -> None:
        class EmptyPage:
            def extract_text(self, extraction_mode: str = "layout") -> str:
                return ""

        class FakeReader:
            def __init__(self, path: str) -> None:
                self.pages = [EmptyPage(), EmptyPage()]

        fake_pypdf = types.SimpleNamespace(PdfReader=FakeReader)
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "scanned.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n")
            with patch.dict(sys.modules, {"pypdf": fake_pypdf}):
                with self.assertRaisesRegex(PdfExtractionError, "scanned or image-only"):
                    extract_pdf(pdf_path)





from pathlib import Path
import tempfile
import unittest

from src.local_admin_panel import (
    UploadError,
    UploadedPdf,
    list_pdf_files,
    parse_multipart_upload,
    save_uploaded_pdf,
    secure_pdf_filename,
    validate_pdf_payload,
)


class LocalAdminPanelTests(unittest.TestCase):
    def test_secure_pdf_filename_strips_paths_and_normalizes_extension(self) -> None:
        self.assertEqual(secure_pdf_filename(r"C:\secret\IBD Trial 2026.PDF"), "IBD-Trial-2026.pdf")
        self.assertEqual(secure_pdf_filename("../../paper.pdf"), "paper.pdf")

    def test_validate_pdf_payload_rejects_non_pdf_content(self) -> None:
        with self.assertRaisesRegex(UploadError, "does not look like a PDF"):
            validate_pdf_payload("paper.pdf", b"not a pdf")

    def test_save_uploaded_pdf_avoids_overwrite(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="gastroagi-local-admin-"))
        try:
            first = save_uploaded_pdf(UploadedPdf("paper.pdf", b"%PDF-1.7\nfirst"), temp_root)
            second = save_uploaded_pdf(UploadedPdf("paper.pdf", b"%PDF-1.7\nsecond"), temp_root)

            self.assertEqual(first["filename"], "paper.pdf")
            self.assertEqual(second["filename"], "paper-1.pdf")
            self.assertEqual((temp_root / "paper.pdf").read_bytes(), b"%PDF-1.7\nfirst")
            self.assertEqual((temp_root / "paper-1.pdf").read_bytes(), b"%PDF-1.7\nsecond")
        finally:
            import shutil

            shutil.rmtree(temp_root, ignore_errors=True)

    def test_parse_multipart_upload_accepts_multiple_pdfs(self) -> None:
        boundary = "----gastroagi-test-boundary"
        body = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="first.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "%PDF-1.7 first\r\n"
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; filename="second.pdf"\r\n'
            "Content-Type: application/pdf\r\n\r\n"
            "%PDF-1.7 second\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        uploads = parse_multipart_upload(f"multipart/form-data; boundary={boundary}", body)

        self.assertEqual([upload.filename for upload in uploads], ["first.pdf", "second.pdf"])
        self.assertTrue(all(upload.content.startswith(b"%PDF-") for upload in uploads))

    def test_list_pdf_files_is_content_light(self) -> None:
        temp_root = Path(tempfile.mkdtemp(prefix="gastroagi-local-admin-"))
        try:
            (temp_root / "paper.pdf").write_bytes(b"%PDF-1.7\nclinical source text should not be displayed")

            files = list_pdf_files(temp_root)

            self.assertEqual(files[0]["name"], "paper.pdf")
            self.assertIn("bytes", files[0])
            self.assertNotIn("clinical source text", str(files))
        finally:
            import shutil

            shutil.rmtree(temp_root, ignore_errors=True)

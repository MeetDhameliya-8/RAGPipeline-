from pathlib import Path
import json
import unittest

from src.admin_uploader import AdminUploaderError, load_chunks, load_selectors


class AdminUploaderTests(unittest.TestCase):
    def test_load_chunks_rejects_missing_required_field(self) -> None:
        temp_root = _make_temp_dir()
        try:
            chunks_json = temp_root / "chunks.json"
            chunks_json.write_text(
                json.dumps(
                    {
                        "article": {},
                        "chunks": [
                            {
                                "chunk_number": 1,
                                "topic": "Ulcerative Colitis",
                                "subtopic": "Adults",
                                "section": "Does Treatment Help?",
                                "reference": "",
                                "original_text": "word " * 120,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(AdminUploaderError):
                load_chunks(chunks_json)
        finally:
            _remove_tree(temp_root)

    def test_load_selectors_requires_all_fields(self) -> None:
        temp_root = _make_temp_dir()
        try:
            selector_path = temp_root / "selectors.json"
            selector_path.write_text(
                json.dumps(
                    {
                        "add_more_chunks": "[data-testid='add']",
                        "fields": {
                            "topic": "[data-testid='topic']",
                            "subtopic": "[data-testid='subtopic']",
                        },
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaises(AdminUploaderError):
                load_selectors(selector_path)
        finally:
            _remove_tree(temp_root)


def _make_temp_dir() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp(prefix="gastroagi-admin-test-"))


def _remove_tree(path: Path) -> None:
    import shutil

    shutil.rmtree(path, ignore_errors=True)

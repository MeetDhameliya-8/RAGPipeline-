"""Populate GastroAGI Admin Panel Custom Chunk forms without uploading."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

try:  # pragma: no cover - import mode depends on invocation style
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:  # Supports both `python -m src.admin_uploader` and direct script use.
    from .chunk_validator import REQUIRED_FIELDS, validate_chunks
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.chunk_validator import REQUIRED_FIELDS, validate_chunks


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SELECTOR_PATH = PROJECT_ROOT / "config" / "admin_selectors.example.json"
LOG_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOG_DIR / "admin_uploader.log"
HUMAN_REVIEW_MESSAGE = "FORM POPULATION COMPLETE — HUMAN REVIEW REQUIRED."


@dataclass(frozen=True)
class AdminUploaderConfig:
    """Runtime settings for safe form population."""

    admin_url: str
    selector_path: Path
    browser_profile_dir: Path
    headless: bool
    wait_for_review: bool


class AdminUploaderError(RuntimeError):
    """Raised when admin form population fails safely."""


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    if load_dotenv is not None:
        load_dotenv(PROJECT_ROOT / ".env")

    parser = build_parser()
    args = parser.parse_args(argv)
    logger = configure_logging()

    try:
        chunks_data = load_chunks(Path(args.chunks_json))
        validation_report = validate_chunks(chunks_data)
        if validation_report["status"] == "FAILED":
            raise AdminUploaderError(
                "chunks.json failed validation. Review validation_report.json before form population."
            )

        if args.dry_run:
            print_dry_run_summary(chunks_data, validation_report)
            return 0

        config = AdminUploaderConfig(
            admin_url=args.admin_url.strip(),
            selector_path=Path(args.selectors),
            browser_profile_dir=Path(args.browser_profile_dir),
            headless=args.headless,
            wait_for_review=not args.no_wait,
        )
        populate_admin_form(chunks_data, config, logger)
        return 0
    except AdminUploaderError as exc:
        logger.error("%s", exc)
        print(f"Admin uploader stopped safely: {exc}")
        return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("chunks_json", help="Path to reviewed chunks.json.")
    parser.add_argument(
        "--admin-url",
        default=os.getenv("GASTROAGI_ADMIN_URL", ""),
        help="GastroAGI Admin Panel or Custom Chunk URL.",
    )
    parser.add_argument(
        "--selectors",
        default=str(DEFAULT_SELECTOR_PATH),
        help="Path to selector JSON.",
    )
    parser.add_argument(
        "--browser-profile-dir",
        default=os.getenv("GASTROAGI_BROWSER_PROFILE_DIR", "browser_profile"),
        help="Persistent browser profile directory for an authenticated session.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run headless. Not recommended for human review.",
    )
    parser.add_argument(
        "--no-wait",
        action="store_true",
        help="Do not wait for Enter after populating forms.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Validate inputs only.")
    return parser


def load_chunks(chunks_json: Path) -> dict[str, Any]:
    """Load and minimally validate a chunks.json file."""
    if not chunks_json.exists():
        raise AdminUploaderError(f"chunks.json not found: {chunks_json}")
    try:
        data = json.loads(chunks_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AdminUploaderError(f"Malformed chunks.json: {exc}") from exc

    chunks = data.get("chunks")
    if not isinstance(chunks, list) or not chunks:
        raise AdminUploaderError("chunks.json must contain at least one chunk.")
    for index, chunk in enumerate(chunks, start=1):
        if not isinstance(chunk, dict):
            raise AdminUploaderError(f"Chunk {index} must be an object.")
        missing = [field for field in REQUIRED_FIELDS if not str(chunk.get(field, "")).strip()]
        if missing:
            raise AdminUploaderError(
                f"Chunk {index} is missing required field(s): {', '.join(missing)}."
            )
    return data


def load_selectors(selector_path: Path) -> dict[str, Any]:
    """Load configurable selectors for the Admin Panel UI."""
    if not selector_path.exists():
        raise AdminUploaderError(f"Selector config not found: {selector_path}")
    try:
        selectors = json.loads(selector_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AdminUploaderError(f"Malformed selector config: {exc}") from exc

    fields = selectors.get("fields", {})
    missing = [field for field in REQUIRED_FIELDS if not str(fields.get(field, "")).strip()]
    if missing:
        raise AdminUploaderError(
            "Selector config is missing field selector(s): " + ", ".join(missing) + "."
        )
    if not str(selectors.get("add_more_chunks", "")).strip():
        raise AdminUploaderError("Selector config is missing `add_more_chunks`.")
    return selectors


def populate_admin_form(
    chunks_data: dict[str, Any],
    config: AdminUploaderConfig,
    logger: logging.Logger,
) -> None:
    """Fill all Custom Chunk forms and stop before Upload."""
    if not config.admin_url:
        raise AdminUploaderError(
            "Admin URL is required. Set GASTROAGI_ADMIN_URL or pass --admin-url."
        )

    selectors = load_selectors(config.selector_path)
    chunks = chunks_data["chunks"]

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:  # pragma: no cover - environment dependent
        raise AdminUploaderError(
            "playwright is required for admin form automation. Install requirements.txt and run `python -m playwright install chromium`."
        ) from exc

    config.browser_profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            str(config.browser_profile_dir),
            headless=config.headless,
        )
        page = context.pages[0] if context.pages else context.new_page()
        try:
            page.goto(config.admin_url, wait_until="domcontentloaded")
            maybe_login(page, selectors, logger)

            custom_chunk_url = str(selectors.get("custom_chunk_url", "")).strip()
            if custom_chunk_url:
                page.goto(custom_chunk_url, wait_until="domcontentloaded")

            for index, chunk in enumerate(chunks):
                if index > 0:
                    click_add_more(page, selectors["add_more_chunks"])
                fill_chunk_fields(page, selectors["fields"], chunk, index)
                logger.info("Populated chunk %s: %s", index + 1, chunk.get("section", ""))

            confirm_upload_left_to_human(page, selectors)
            print(HUMAN_REVIEW_MESSAGE)
            logger.info("%s", HUMAN_REVIEW_MESSAGE)
            if config.wait_for_review and not config.headless:
                input("Review the browser. Press Enter here when you are ready to close it.")
        except Exception as exc:
            screenshot_message = capture_failure_screenshot(page)
            if isinstance(exc, AdminUploaderError):
                raise AdminUploaderError(f"{exc} {screenshot_message}") from exc
            raise AdminUploaderError(
                f"Browser automation failed. {screenshot_message}: {exc}"
            ) from exc
        finally:
            context.close()


def maybe_login(page: Any, selectors: dict[str, Any], logger: logging.Logger) -> None:
    """Use optional restricted-account credentials if login selectors are visible."""
    login_selectors = selectors.get("login") or {}
    username = os.getenv("GASTROAGI_UPLOADER_USERNAME", "").strip()
    password = os.getenv("GASTROAGI_UPLOADER_PASSWORD", "").strip()
    if not username or not password or not login_selectors:
        return

    username_selector = str(login_selectors.get("username", "")).strip()
    password_selector = str(login_selectors.get("password", "")).strip()
    submit_selector = str(login_selectors.get("submit", "")).strip()
    if not username_selector or not password_selector or not submit_selector:
        return

    username_field = page.locator(username_selector).first
    if not username_field.is_visible(timeout=1500):
        return

    page.locator(username_selector).fill(username)
    page.locator(password_selector).fill(password)
    page.locator(submit_selector).click()
    page.wait_for_load_state("domcontentloaded")
    logger.info("Authenticated with restricted uploader account from environment.")


def fill_chunk_fields(
    page: Any, field_selectors: dict[str, str], chunk: dict[str, Any], index: int
) -> None:
    """Fill and verify the five GastroAGI fields for one chunk."""
    for field in REQUIRED_FIELDS:
        selector = field_selectors[field]
        value = str(chunk.get(field, ""))
        locator = page.locator(selector).nth(index)
        locator.scroll_into_view_if_needed()
        locator.fill(value)
        verify_field_value(locator, value, field, index + 1)


def verify_field_value(locator: Any, expected: str, field: str, chunk_number: int) -> None:
    """Verify an input, textarea, or contenteditable value after filling."""
    try:
        actual = locator.input_value()
    except Exception:
        actual = locator.evaluate(
            "(element) => element.value ?? element.innerText ?? element.textContent ?? ''"
        )
    if str(actual).strip() != expected.strip():
        raise AdminUploaderError(
            f"Verification failed for chunk {chunk_number} field `{field}`."
        )


def click_add_more(page: Any, selector: str) -> None:
    """Click the configured Add More Chunks control."""
    page.locator(selector).click()
    page.wait_for_timeout(250)


def confirm_upload_left_to_human(page: Any, selectors: dict[str, Any]) -> None:
    """Locate the Upload button if configured, but never click it."""
    upload_selector = str(selectors.get("upload_button", "")).strip()
    if upload_selector:
        page.locator(upload_selector).first.scroll_into_view_if_needed(timeout=1000)


def print_dry_run_summary(
    chunks_data: dict[str, Any], validation_report: dict[str, Any]
) -> None:
    """Print a content-light dry-run summary without full article text."""
    chunks = chunks_data["chunks"]
    print("Dry run: no browser opened and no forms populated.")
    print(f"Chunks: {len(chunks)}")
    print(f"Validation status: {validation_report['status']}")
    for index, chunk in enumerate(chunks, start=1):
        print(f"{index}. {chunk.get('section', '')}")


def configure_logging() -> logging.Logger:
    """Configure uploader logging without printing secrets."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("gastroagi_admin_uploader")
    logger.setLevel(logging.INFO)
    if logger.handlers:
        return logger

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    return logger


def failure_screenshot_path() -> Path:
    """Return an ignored local screenshot path for browser failures."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return LOG_DIR / f"admin_uploader_failure_{timestamp}.png"


def capture_failure_screenshot(page: Any) -> str:
    """Capture a failure screenshot if the browser is still available."""
    screenshot_path = failure_screenshot_path()
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
    except Exception as exc:  # pragma: no cover - browser state dependent
        return f"Screenshot could not be captured: {exc}"
    return f"Screenshot saved to {screenshot_path}"


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())

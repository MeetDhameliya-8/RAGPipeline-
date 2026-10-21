"""Local-only PDF upload panel for the GastroAGI chunk pipeline."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
import html
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import re
import sys
import time
import unicodedata
from typing import Any
from urllib.parse import urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
INPUT_DIR = PROJECT_ROOT / "input_pdfs"
PROCESSED_DIR = PROJECT_ROOT / "processed"
OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_MAX_UPLOAD_BYTES = 100 * 1024 * 1024
PDF_MAGIC = b"%PDF-"


@dataclass(frozen=True)
class UploadedPdf:
    """Parsed PDF upload payload."""

    filename: str
    content: bytes


class UploadError(ValueError):
    """Raised when a local upload request is invalid."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=DEFAULT_HOST, help="Bind host. Defaults to localhost only.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Bind port.")
    parser.add_argument(
        "--max-upload-mb",
        type=int,
        default=DEFAULT_MAX_UPLOAD_BYTES // (1024 * 1024),
        help="Maximum request size in MiB.",
    )
    args = parser.parse_args(argv)

    server = create_server(
        host=args.host,
        port=args.port,
        max_upload_bytes=max(1, args.max_upload_mb) * 1024 * 1024,
    )
    url = f"http://{args.host}:{args.port}"
    print(f"GastroAGI local PDF upload panel: {url}")
    print("Uploads are saved to input_pdfs/. No Azure generation, ingestion, or Admin Panel upload is triggered.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping local upload panel.")
    finally:
        server.server_close()
    return 0


def create_server(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES) -> ThreadingHTTPServer:
    """Create the local-only upload server."""
    class Handler(LocalAdminHandler):
        pass

    Handler.max_upload_bytes = max_upload_bytes
    return ThreadingHTTPServer((host, port), Handler)


class LocalAdminHandler(BaseHTTPRequestHandler):
    """Tiny stdlib HTTP handler for local PDF uploads."""

    server_version = "GastroAGILocalAdmin/1.0"
    max_upload_bytes = DEFAULT_MAX_UPLOAD_BYTES

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path == "/":
            self._send_html(render_panel_html())
            return
        if path == "/api/status":
            self._send_json(local_status())
            return
        self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        path = urlparse(self.path).path
        if path != "/upload":
            self._send_json({"error": "not_found"}, status=HTTPStatus.NOT_FOUND)
            return

        try:
            content_length = int(self.headers.get("Content-Length") or "0")
        except ValueError:
            self._send_json({"error": "invalid_content_length"}, status=HTTPStatus.BAD_REQUEST)
            return
        if content_length <= 0:
            self._send_json({"error": "empty_upload"}, status=HTTPStatus.BAD_REQUEST)
            return
        if content_length > self.max_upload_bytes:
            self._send_json(
                {"error": "upload_too_large", "max_bytes": self.max_upload_bytes},
                status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
            )
            return

        body = self.rfile.read(content_length)
        try:
            uploads = parse_multipart_upload(self.headers.get("Content-Type", ""), body)
            saved = [save_uploaded_pdf(upload, INPUT_DIR) for upload in uploads]
        except UploadError as exc:
            self._send_json({"error": "invalid_upload", "message": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return
        except OSError as exc:
            self._send_json({"error": "save_failed", "message": exc.__class__.__name__}, status=HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        self._send_json({"ok": True, "saved": saved, "status": local_status()})

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("local-admin-panel " + format % args + "\n")

    def _send_html(self, body: str, status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, payload: dict[str, Any], status: HTTPStatus = HTTPStatus.OK) -> None:
        encoded = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)


def parse_multipart_upload(content_type: str, body: bytes) -> list[UploadedPdf]:
    """Parse PDF files from a multipart/form-data request."""
    if "multipart/form-data" not in content_type.lower():
        raise UploadError("Expected multipart/form-data.")
    header = f"Content-Type: {content_type}\r\nMIME-Version: 1.0\r\n\r\n".encode("utf-8")
    message = BytesParser(policy=policy.default).parsebytes(header + body)
    uploads: list[UploadedPdf] = []
    for part in message.iter_parts():
        filename = part.get_filename()
        if not filename:
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes):
            payload = str(part.get_content()).encode("utf-8")
        safe_name = secure_pdf_filename(filename)
        validate_pdf_payload(safe_name, payload)
        uploads.append(UploadedPdf(filename=safe_name, content=payload))
    if not uploads:
        raise UploadError("No PDF files were uploaded.")
    return uploads


def secure_pdf_filename(filename: str) -> str:
    """Return a safe local PDF filename, stripping paths and unusual characters."""
    raw = Path(str(filename).replace("\\", "/")).name.strip()
    normalized = unicodedata.normalize("NFKD", raw).encode("ascii", "ignore").decode("ascii")
    stem = Path(normalized).stem
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-._") or "uploaded-pdf"
    return f"{stem[:90]}.pdf"


def validate_pdf_payload(filename: str, content: bytes) -> None:
    """Validate extension and PDF signature before saving."""
    if not filename.lower().endswith(".pdf"):
        raise UploadError("Only .pdf files are accepted.")
    if not content:
        raise UploadError(f"{filename} is empty.")
    if not content.lstrip()[:5] == PDF_MAGIC:
        raise UploadError(f"{filename} does not look like a PDF file.")


def save_uploaded_pdf(upload: UploadedPdf, input_dir: str | Path = INPUT_DIR) -> dict[str, Any]:
    """Save an uploaded PDF without overwriting an existing queued file."""
    directory = Path(input_dir)
    directory.mkdir(parents=True, exist_ok=True)
    target = unique_path(directory / upload.filename)
    target.write_bytes(upload.content)
    return {
        "filename": target.name,
        "path": str(target),
        "bytes": len(upload.content),
    }


def unique_path(path: Path) -> Path:
    """Return a non-existing path by adding a numeric suffix when needed."""
    if not path.exists():
        return path
    for counter in range(1, 1000):
        candidate = path.with_name(f"{path.stem}-{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise UploadError(f"Could not find an available filename for {path.name}.")


def local_status() -> dict[str, Any]:
    """Return a content-light local workflow status."""
    return {
        "input_pdfs": list_pdf_files(INPUT_DIR),
        "processed_pdfs": list_pdf_files(PROCESSED_DIR),
        "outputs": list_output_dirs(OUTPUT_DIR),
        "safety": {
            "azure_generation_triggered": False,
            "database_ingestion_triggered": False,
            "admin_upload_triggered": False,
        },
    }


def list_pdf_files(directory: str | Path) -> list[dict[str, Any]]:
    path = Path(directory)
    if not path.exists():
        return []
    files = []
    for item in sorted(path.glob("*.pdf"), key=lambda candidate: candidate.name.lower()):
        try:
            stat = item.stat()
        except OSError:
            continue
        files.append(
            {
                "name": item.name,
                "bytes": stat.st_size,
                "modified": int(stat.st_mtime),
            }
        )
    return files


def list_output_dirs(directory: str | Path) -> list[dict[str, Any]]:
    path = Path(directory)
    if not path.exists():
        return []
    outputs = []
    for item in sorted(path.iterdir(), key=lambda candidate: candidate.name.lower()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        validation = item / "validation_report.json"
        status = "unknown"
        if validation.exists():
            try:
                payload = json.loads(validation.read_text(encoding="utf-8"))
                status = str(payload.get("status") or "unknown")
            except (OSError, json.JSONDecodeError):
                status = "unreadable"
        outputs.append({"name": item.name, "validation_status": status})
    return outputs


def render_panel_html() -> str:
    status = local_status()
    queued = len(status["input_pdfs"])
    processed = len(status["processed_pdfs"])
    outputs = len(status["outputs"])
    return f"""<!doctype html>
<html lang=\"en\">
<head>
  <meta charset=\"utf-8\">
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">
  <title>GastroAGI Local PDF Upload</title>
  <style>
    :root {{ color-scheme: light; font-family: Inter, Segoe UI, Arial, sans-serif; }}
    body {{ margin: 0; background: #f6f7f9; color: #1d2733; }}
    header {{ background: #ffffff; border-bottom: 1px solid #d9dee7; padding: 18px 28px; }}
    h1 {{ margin: 0; font-size: 22px; font-weight: 700; letter-spacing: 0; }}
    main {{ max-width: 1100px; margin: 0 auto; padding: 24px; display: grid; gap: 18px; }}
    section {{ background: #ffffff; border: 1px solid #d9dee7; border-radius: 8px; padding: 18px; }}
    h2 {{ margin: 0 0 14px; font-size: 16px; }}
    .summary {{ display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }}
    .metric {{ border: 1px solid #d9dee7; border-radius: 8px; padding: 14px; background: #fbfcfd; }}
    .metric b {{ display: block; font-size: 24px; margin-bottom: 3px; }}
    .drop {{ border: 2px dashed #9aa8bb; border-radius: 8px; padding: 28px; text-align: center; background: #fbfcfd; }}
    input[type=file] {{ width: 100%; max-width: 440px; }}
    button {{ background: #205493; color: white; border: 0; border-radius: 6px; padding: 10px 14px; font-weight: 700; cursor: pointer; }}
    button:disabled {{ background: #94a3b8; cursor: not-allowed; }}
    .note {{ color: #526173; font-size: 13px; line-height: 1.45; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
    th, td {{ padding: 9px 8px; border-bottom: 1px solid #e5e9f0; text-align: left; }}
    th {{ color: #526173; font-weight: 700; }}
    .message {{ min-height: 20px; font-size: 14px; }}
    .ok {{ color: #166534; }} .err {{ color: #b42318; }}
    @media (max-width: 720px) {{ .summary {{ grid-template-columns: 1fr; }} main {{ padding: 14px; }} }}
  </style>
</head>
<body>
  <header><h1>GastroAGI Local PDF Upload</h1></header>
  <main>
    <section class=\"summary\">
      <div class=\"metric\"><b id=\"queued-count\">{queued}</b>Queued PDFs</div>
      <div class=\"metric\"><b id=\"processed-count\">{processed}</b>Processed PDFs</div>
      <div class=\"metric\"><b id=\"output-count\">{outputs}</b>Output folders</div>
    </section>
    <section>
      <h2>Upload PDFs</h2>
      <form id=\"upload-form\" class=\"drop\">
        <p>Select one or more PDF files to place in <code>input_pdfs/</code>.</p>
        <input id=\"files\" name=\"files\" type=\"file\" accept=\"application/pdf,.pdf\" multiple required>
        <p><button id=\"upload-button\" type=\"submit\">Upload PDFs</button></p>
        <div id=\"message\" class=\"message\"></div>
        <p class=\"note\">This panel only queues PDFs locally. It does not call Azure, generate chunks, upload to the Admin Panel, or write to a database.</p>
      </form>
    </section>
    <section>
      <h2>Queued PDFs</h2>
      <table><thead><tr><th>Name</th><th>Size</th><th>Modified</th></tr></thead><tbody id=\"queued-table\"></tbody></table>
    </section>
    <section>
      <h2>Outputs</h2>
      <table><thead><tr><th>Folder</th><th>Validation Status</th></tr></thead><tbody id=\"outputs-table\"></tbody></table>
    </section>
  </main>
<script>
const form = document.getElementById('upload-form');
const files = document.getElementById('files');
const button = document.getElementById('upload-button');
const message = document.getElementById('message');
function fmtBytes(bytes) {{
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}}
function fmtTime(seconds) {{ return new Date(seconds * 1000).toLocaleString(); }}
function row(cells) {{ return '<tr>' + cells.map(value => '<td>' + String(value).replace(/[&<>]/g, ch => ({{'&':'&amp;','<':'&lt;','>':'&gt;'}}[ch])) + '</td>').join('') + '</tr>'; }}
async function refresh() {{
  const response = await fetch('/api/status', {{cache: 'no-store'}});
  const status = await response.json();
  document.getElementById('queued-count').textContent = status.input_pdfs.length;
  document.getElementById('processed-count').textContent = status.processed_pdfs.length;
  document.getElementById('output-count').textContent = status.outputs.length;
  document.getElementById('queued-table').innerHTML = status.input_pdfs.map(file => row([file.name, fmtBytes(file.bytes), fmtTime(file.modified)])).join('') || row(['No PDFs queued', '', '']);
  document.getElementById('outputs-table').innerHTML = status.outputs.map(item => row([item.name, item.validation_status])).join('') || row(['No output folders', '']);
}}
form.addEventListener('submit', async event => {{
  event.preventDefault();
  if (!files.files.length) return;
  button.disabled = true;
  message.className = 'message';
  message.textContent = 'Uploading...';
  const data = new FormData();
  for (const file of files.files) data.append('files', file, file.name);
  try {{
    const response = await fetch('/upload', {{method: 'POST', body: data}});
    const result = await response.json();
    if (!response.ok) throw new Error(result.message || result.error || 'Upload failed');
    message.className = 'message ok';
    message.textContent = 'Uploaded ' + result.saved.length + ' PDF file(s).';
    files.value = '';
    await refresh();
  }} catch (error) {{
    message.className = 'message err';
    message.textContent = error.message;
  }} finally {{ button.disabled = false; }}
}});
refresh();
</script>
</body>
</html>"""


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

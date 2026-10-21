"""Versioned lossless source-map persistence; legacy JSON remains readable.

The in-memory evidence contract is unchanged. Stream model records through gzip
instead of materializing the complete indented JSON string and UTF-8 copy.
"""
from __future__ import annotations

import base64
import binascii
import gzip
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import tempfile
import zlib
from typing import TYPE_CHECKING, Any, Iterator

from pydantic import BaseModel

if TYPE_CHECKING:
    from .source_map import SourceDocument

FORMAT = "gastroagi-source-map-gzip-json-v1"
MAX_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
IDENTITY_FIELDS = ("document_id", "source_file", "page_count", "content_hash", "config_hash")


def _validate_optional_plan(doc: SourceDocument) -> None:
    if doc.source_package_plan is not None:
        from .source_packages import validate_source_package_plan
        if doc.evidence is None:
            raise ValueError("Source packages require the evidence graph.")
        validate_source_package_plan(doc.source_package_plan, doc.evidence)
    if doc.multiview_selection is not None:
        from .multiview import validate_multiview_selection
        validate_multiview_selection(doc.multiview_selection, doc)
    if doc.clinical_plan is not None:
        if doc.evidence is None:raise ValueError("Clinical plan requires its source evidence graph.")
        from .concept_planner import validate_clinical_plan
        validate_clinical_plan(doc.clinical_plan,doc.evidence)


def _parts(value: Any, *, split_model: bool = False) -> Iterator[str]:
    if isinstance(value, BaseModel):
        if not split_model:
            yield value.model_dump_json()
            return
        value = {key: getattr(value, key) for key in type(value).model_fields}
    if isinstance(value, dict):
        yield "{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield ","
            yield json.dumps(key, ensure_ascii=False) + ":"
            # Split the large evidence registry, not individual mapping records.
            yield from _parts(item, split_model=key in {"evidence", "multiview_selection", "alternate_evidence"})
        yield "}"
    elif isinstance(value, (list, tuple)):
        yield "["
        for index, item in enumerate(value):
            if index:
                yield ","
            yield from _parts(item)
        yield "]"
    else:
        yield json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def save_source_map(path: str | Path, doc: SourceDocument, *, overwrite: bool = False) -> None:
    """Write a new artifact atomically; replacement requires an explicit flag."""
    from .evidence import validate_evidence
    path = Path(path)
    if path.exists() and not overwrite:
        raise FileExistsError(path)
    if doc.evidence:
        validate_evidence(doc.evidence)
    _validate_optional_plan(doc)
    compressed = io.BytesIO()
    digest, length = sha256(), 0
    with gzip.GzipFile(fileobj=compressed, mode="wb", compresslevel=6, mtime=0, filename="") as stream:
        pending = []
        size = 0
        for part in _parts(doc, split_model=True):
            pending.append(part)
            size += len(part)
            if size < 65536:
                continue
            data = "".join(pending).encode("utf-8")
            length += len(data)
            if length > MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Source map exceeds the storage byte limit.")
            digest.update(data)
            stream.write(data)
            pending, size = [], 0
        data = "".join(pending).encode("utf-8")
        length += len(data)
        if length > MAX_UNCOMPRESSED_BYTES:
            raise ValueError("Source map exceeds the storage byte limit.")
        digest.update(data)
        stream.write(data)
    envelope = {key: getattr(doc, key) for key in IDENTITY_FIELDS}
    envelope["_source_map_storage"] = {"format": FORMAT, "sha256": digest.hexdigest(),
        "uncompressed_bytes": length, "payload": base64.b64encode(compressed.getvalue()).decode("ascii")}
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", dir=path.parent, delete=False) as output:
            temporary = Path(output.name)
            json.dump(envelope, output, ensure_ascii=False, separators=(",", ":"))
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)  # atomic exclusive creation, including on Windows NTFS
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def load_source_map(path: str | Path) -> SourceDocument:
    """Read legacy JSON or a bounded, checksum-verified compressed artifact."""
    from .source_map import SourceDocument
    from .evidence import validate_evidence
    data = Path(path).read_bytes()
    # Legacy files retain the fast Pydantic JSON path, without a Python dict copy.
    if b'"_source_map_storage"' not in data:
        result = SourceDocument.model_validate_json(data)
    else:
        try:
            envelope = json.loads(data)
            if isinstance(envelope, dict) and "_source_map_storage" not in envelope:
                result = SourceDocument.model_validate_json(data)
                if result.evidence:
                    validate_evidence(result.evidence)
                _validate_optional_plan(result)
                return result
            storage = envelope["_source_map_storage"]
            if storage["format"] != FORMAT:
                raise ValueError("Unsupported source-map storage format.")
            length = storage["uncompressed_bytes"]
            if type(length) is not int or not 0 <= length <= MAX_UNCOMPRESSED_BYTES:
                raise ValueError("Invalid source-map storage size.")
            packed = base64.b64decode(storage["payload"], validate=True)
            with gzip.GzipFile(fileobj=io.BytesIO(packed), mode="rb") as stream:
                raw = stream.read(length + 1)
            if len(raw) != length or sha256(raw).hexdigest() != storage["sha256"]:
                raise ValueError("Source-map storage integrity check failed.")
            result = SourceDocument.model_validate_json(raw)
            if any(envelope.get(key) != getattr(result, key) for key in IDENTITY_FIELDS):
                raise ValueError("Source-map envelope identity mismatch.")
        except (KeyError, TypeError, OSError, EOFError, binascii.Error, zlib.error) as exc:
            raise ValueError("Invalid compressed source-map artifact.") from exc
    if result.evidence:
        validate_evidence(result.evidence)
    _validate_optional_plan(result)
    return result

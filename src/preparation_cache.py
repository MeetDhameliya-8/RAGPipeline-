"""Checksummed, versioned local preparation checkpoints; no clinical approval."""
from __future__ import annotations

from .diagnostics import timed

from hashlib import sha256
from importlib.metadata import version
import json
import os
from pathlib import Path
import tempfile
from typing import Any

from pydantic import BaseModel, ConfigDict

from .evidence_storage import load_source_map, save_source_map
from .source_map import SourceDocument

MAX_EXTRACTION_BYTES = 64 * 1024 * 1024
MODULES = ("preparation_cache", "pdf_extractor", "evidence", "native_layout", "source_map",
           "source_packages", "draft_support", "adaptive_ingestion", "concept_planner", "visual_source", "hybrid_routing",
           "citation_recovery")


class PreparationManifest(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    version: str = "prepared-source-v1"
    key: str
    pdf_sha256: str
    source_file: str
    stage: str
    source_map_sha256: str
    extraction_sha256: str


def preparation_key(pdf_path: Path, *, visual: bool = False) -> tuple[str,str]:
    digest=sha256(pdf_path.read_bytes()).hexdigest()
    root=Path(__file__).parent
    implementation={name:sha256((root/(name+".py")).read_bytes()).hexdigest() for name in MODULES}
    dependencies={name:version(name) for name in ("pypdf","PyMuPDF","pydantic")}
    provider={name:os.getenv(name,"") for name in ("AZURE_OPENAI_ENDPOINT","AZURE_OPENAI_DEPLOYMENT","AZURE_OPENAI_API_VERSION")} if visual else {}
    key=sha256(json.dumps({"version":"prepared-source-v1","pdf":digest,"file":pdf_path.name,
        "implementation":implementation,"dependencies":dependencies,"provider":provider,"visual":visual},sort_keys=True).encode()).hexdigest()
    return key,digest


@timed("checkpoint_lookup")
def load_preparation(pdf_path: Path, directory: Path, *, visual: bool = False) -> tuple[dict[str,Any],SourceDocument] | None:
    key,digest=preparation_key(pdf_path,visual=visual)
    folder=directory/key[:24]
    try:
        manifest=PreparationManifest.model_validate_json((folder/'manifest.json').read_bytes(),strict=True)
        if (manifest.version!="prepared-source-v1" or manifest.key!=key or manifest.pdf_sha256!=digest or
                manifest.source_file!=pdf_path.name or manifest.stage!=("visual" if visual else "native")):
            return None
        metadata_path=folder/'extraction.json'
        if metadata_path.stat().st_size>MAX_EXTRACTION_BYTES:return None
        raw=metadata_path.read_bytes();source_bytes=(folder/'source.json').read_bytes()
        if sha256(raw).hexdigest()!=manifest.extraction_sha256 or sha256(source_bytes).hexdigest()!=manifest.source_map_sha256:return None
        extraction=json.loads(raw)
        source=load_source_map(folder/'source.json')
        if (source.evidence is None or source.evidence.pdf_sha256!=digest or source.source_file!=pdf_path.name or
                extraction['file_name']!=pdf_path.name or extraction['full_text']!=source.evidence.buffers[source.evidence.canonical_buffer_id].text):
            return None
        extraction['evidence']=source.evidence.model_dump(mode='json')
        return extraction,source
    except (OSError,ValueError,KeyError,TypeError):
        return None  # Incompatible or damaged cache is rebuilt from PDF bytes.


def save_preparation(pdf_path: Path, extraction: dict[str,Any], source: SourceDocument,
                     directory: Path, *, visual: bool = False) -> None:
    key,digest=preparation_key(pdf_path,visual=visual)
    if source.evidence is None or source.evidence.pdf_sha256!=digest:raise ValueError('Preparation source identity mismatch.')
    folder=directory/key[:24];folder.mkdir(parents=True,exist_ok=True)
    metadata=json.dumps({k:v for k,v in extraction.items() if k!='evidence'},ensure_ascii=False,separators=(',',':')).encode()
    if len(metadata)>MAX_EXTRACTION_BYTES:raise ValueError('Extraction checkpoint exceeds its byte budget.')
    # Write complete files first; publish their manifest last. A process crash
    # or concurrent stale manifest yields a cache miss, never unverified reuse.
    save_source_map(folder/'source.json',source,overwrite=True)
    _atomic_write(folder/'extraction.json',metadata)
    manifest=PreparationManifest(key=key,pdf_sha256=digest,source_file=pdf_path.name,stage='visual' if visual else 'native',
        source_map_sha256=sha256((folder/'source.json').read_bytes()).hexdigest(),extraction_sha256=sha256(metadata).hexdigest())
    _atomic_write(folder/'manifest.json',manifest.model_dump_json().encode())


def _atomic_write(path: Path, data: bytes) -> None:
    temporary=None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent,delete=False) as output:
            temporary=Path(output.name);output.write(data)
        os.replace(temporary,path)
    finally:
        if temporary:temporary.unlink(missing_ok=True)

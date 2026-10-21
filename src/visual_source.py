"""Bounded Azure image transcription. Corroboration is not clinical approval."""
from __future__ import annotations

from hashlib import sha256
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from .concept_planner import Contract, CLINICAL
from .evidence import EvidenceSpan, add_buffer, get_evidence, span, text_hash, warn
from . import diagnostics as telemetry

PROMPT = ("Transcribe only readable clinical prose paragraphs on this PDF page, verbatim, preserving numbers, signs, "
    "negation, qualifications and paragraph boundaries. Omit author affiliations, headers, tables, figure captions, "
    "references and boilerplate. Never infer missing characters, summarize, correct medical facts or add explanations. "
    "If text is unreadable, omit that paragraph. Return strict JSON: "
    '{"paragraphs":["exact paragraph text"],"uncertainties":["brief extraction uncertainty"]}.')


class PageTranscription(BaseModel):
    model_config = ConfigDict(extra="forbid",strict=True)
    paragraphs: list[str]
    uncertainties: list[str]


class VisualSourcePage(Contract):
    version: Literal["azure-visual-source-v1"] = "azure-visual-source-v1"
    recovery_id: str
    pdf_sha256: str
    page_number: int
    image_sha256: str
    image_width: int = Field(gt=0)
    image_height: int = Field(gt=0)
    render_scale: float
    first_buffer_id: str
    second_buffer_id: str
    paragraph_spans: list[EvidenceSpan]
    provider_fingerprint: str
    uncertainties: list[str]
    clinical_verification: Literal["unverified_requires_source_review"] = "unverified_requires_source_review"


class VisualTranscriptionProvider(Protocol):
    def transcribe_image(self, png: bytes, prompt: str) -> str: ...


@telemetry.observed_run
@telemetry.timed("visual_recovery")
def recover_visual_source(pdf_path: Path, extraction: dict, *, provider=None,
                          cache_directory: Path, max_pages: int | None = None,
                          page_numbers: list[int] | None = None, progress_callback=None,
                          allow_network: bool = True, max_concurrency: int = 2) -> dict:
    """Read selected unsupported pages sequentially, retaining completed page caches.

    No network occurs during plan validation/load. Both immutable transcriptions
    and source page renderings are retained in the local runtime cache.
    """
    import fitz
    from .providers.azure import AzureChunkGenerator
    from .chunk_generator import parse_model_json
    from .providers.base import ChunkGenerationError
    from .source_packages import DAMAGE, STRUCTURED, BOILERPLATE, _words
    doc = get_evidence(extraction)
    digest = sha256(pdf_path.read_bytes()).hexdigest()
    if doc.identity_quality != "pdf_bytes" or digest != doc.pdf_sha256:
        raise ValueError("Visual recovery PDF identity mismatch.")
    if max_pages is not None and max_pages < 1:
        raise ValueError("Visual source page budget must be positive.")
    if max_concurrency not in (1,2):raise ValueError('Visual concurrency must be one or two requests.')
    provider = provider or AzureChunkGenerator.from_environment()
    configuration = {key:getattr(provider,key,None) for key in ("deployment","endpoint","api_version")}
    configuration = {k:v if isinstance(v,str) else type(provider).__name__ for k,v in configuration.items()}
    provider_hash = text_hash(json.dumps(configuration,sort_keys=True))
    cache_directory.mkdir(parents=True,exist_ok=True)
    records = [VisualSourcePage.model_validate(v) for v in extraction.get('visual_source_pages',[])]
    previous_outcome=extraction.get('visual_recovery_outcome') or {}
    failures = list(previous_outcome.get('failures') or [])
    history = list(previous_outcome.get('failure_history') or [])
    skipped=[]
    observation = telemetry.current()
    with fitz.open(pdf_path) as pdf:
        selected_pages = sorted(set(page_numbers if page_numbers is not None else range(1,len(pdf)+1)))
        if any(isinstance(p,bool) or not isinstance(p,int) or not 1 <= p <= len(pdf) for p in selected_pages):
            raise ValueError("Visual recovery page selection is outside the source PDF.")
        if max_pages is not None:
            selected_pages = selected_pages[:max_pages]
        for index,page_number in enumerate(selected_pages,1):
            if any(r.page_number==page_number for r in records):
                history.extend(f for f in failures if f.get('page_number')==page_number)
                failures=[f for f in failures if f.get('page_number')!=page_number]
                continue
            if progress_callback:
                progress_callback("EXTRACTING",{"message":f"Reading source page {page_number} ({index} of {len(selected_pages)} pages needing recovery).",
                    "visual_page":page_number,"visual_pages_total":len(selected_pages)})
            if pdf[page_number-1].rect.width*pdf[page_number-1].rect.height*2.25>12_000_000:
                failures.append({"page_number":page_number,"code":"VISUAL_RENDER_BUDGET","retryable":False})
                continue
            pixels = pdf[page_number-1].get_pixmap(matrix=fitz.Matrix(1.5,1.5))
            png = pixels.tobytes("png");image_hash=sha256(png).hexdigest()
            key=text_hash(f"azure-visual-source-v1:{digest}:{page_number}:{image_hash}:{provider_hash}:{text_hash(PROMPT)}")
            image_path=cache_directory/(image_hash+".png")
            if image_path.exists() and sha256(image_path.read_bytes()).hexdigest()!=image_hash:
                raise ValueError("Cached source page rendering integrity mismatch.")
            paths=[cache_directory/f'{key}-pass-{attempt}.json' for attempt in (1,2)]
            if not allow_network and not all(path.exists() for path in paths):
                skipped.append(page_number);continue
            history.extend(f for f in failures if f.get('page_number')==page_number)
            failures=[f for f in failures if f.get('page_number')!=page_number]
            if not image_path.exists():image_path.write_bytes(png)
            transcripts=[]
            def read_pass(path):
                with telemetry.bind(observation), telemetry.stage("transcription_pass"):
                    return observed_pass(path)

            def observed_pass(path):
                if path.exists():
                    try:
                        parsed=PageTranscription.model_validate_json(path.read_text(encoding='utf-8'),strict=True)
                        telemetry.count("cached_transcription_passes")
                        return parsed
                    except (ValueError,OSError):
                        telemetry.count("invalid_transcription_checkpoints")
                        if not allow_network:
                            raise ChunkGenerationError('Cached visual transcription is invalid; recovery is pending.',
                                code='SOURCE_PASSAGE_HELD',diagnostics={'error':{'retryable':False}}) from None
                raw=telemetry.model_call("visual_transcription",provider.transcribe_image,png,PROMPT)
                try:
                    parsed=PageTranscription.model_validate(parse_model_json(raw),strict=True)
                except (ValueError,ChunkGenerationError) as exc:
                    raise ChunkGenerationError('Visual response did not satisfy the transcription schema.',code='MALFORMED_RESPONSE') from exc
                from .preparation_cache import _atomic_write
                _atomic_write(path,parsed.model_dump_json().encode())
                return parsed
            errors=[]
            try:
                if max_concurrency==1:
                    for path in paths:
                        transcripts.append(read_pass(path))
                else:
                    with ThreadPoolExecutor(max_workers=2,thread_name_prefix='azure-source-pass') as pool:
                        futures=[pool.submit(read_pass,path) for path in paths]
                        transcripts=[None,None]
                        for position,future in enumerate(futures):
                            try:transcripts[position]=future.result()
                            except BaseException as exc:errors.append((position+1,exc))
            except ChunkGenerationError as exc:
                errors.append((len(transcripts)+1,exc))
            if errors:
                unexpected=[exc for _,exc in errors if not isinstance(exc,ChunkGenerationError)]
                if len(unexpected)==1:raise unexpected[0]
                if unexpected:raise BaseExceptionGroup('Visual transcription workers failed; all outcomes were drained.',unexpected)
                for pass_number,exc in errors:
                    failures.append({"page_number":page_number,"pass_number":pass_number,"code":exc.code,
                        "failure_classification":telemetry.failure(exc, exc.code),
                        "retryable":(exc.diagnostics.get("error") or {}).get("retryable")})
                if any(exc.code in {"AUTHENTICATION_FAILED","QUOTA_EXHAUSTED"} for _,exc in errors):
                    break
                continue
            first,second=transcripts
            a="\n\n".join(first.paragraphs);b="\n\n".join(second.paragraphs)
            first_id=add_buffer(doc,"raw",f"visual:{key}:pass1",a,page_number,"azure_visual_transcription_pass1")
            second_id=add_buffer(doc,"raw",f"visual:{key}:pass2",b,page_number,"azure_visual_transcription_pass2")
            paragraphs=[];cursor=0
            normalized=" ".join(b.split())
            for value in first.paragraphs:
                literal=" ".join(value.split())
                if (_words(value)>=90 and CLINICAL.search(value) and literal and normalized.count(literal)==1 and
                        " ".join(a.split()).count(literal)==1 and
                        not DAMAGE.search(value) and not STRUCTURED.search(value) and not BOILERPLATE.search(value)):
                    paragraphs.append(span(first_id,cursor,cursor+len(value)))
                cursor+=len(value)+2
            record=VisualSourcePage(recovery_id="visual:"+key,pdf_sha256=digest,page_number=page_number,
                image_sha256=image_hash,image_width=pixels.width,image_height=pixels.height,render_scale=1.5,
                first_buffer_id=first_id,second_buffer_id=second_id,paragraph_spans=paragraphs,
                provider_fingerprint=provider_hash,uncertainties=list(dict.fromkeys(first.uncertainties+second.uncertainties+
                    ["Two Azure transcriptions are corroboration only; independent medical/source verification remains required."])))
            records.append(record)
            warn(doc,"visual_transcription_requires_review","Visual transcription remains uncertain and cannot complete source coverage.",page_number,True)
    if sha256(pdf_path.read_bytes()).hexdigest()!=digest:
        raise ValueError("PDF changed during visual transcription.")
    return dict(extraction,evidence=doc.model_dump(mode="json"),visual_source_pages=[r.model_dump(mode="json") for r in records],
        visual_recovery_outcome={"status":"draft_transcriptions_available","pages_read":len(records),
            "pages_requested":sorted(set(selected_pages+list(previous_outcome.get('pages_requested') or []))),"failures":failures,
            "failure_history":history,
            "cache_only_pages_skipped":skipped,
            "corroborated_paragraphs":sum(len(r.paragraph_spans) for r in records),"clinical_verification":False})

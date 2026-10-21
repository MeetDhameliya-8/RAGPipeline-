"""Synthetic source/recovery acceptance only. No deployed provider or user PDFs."""
from hashlib import sha256
import json
import logging
from pathlib import Path
import threading
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import Mock

import fitz
import pytest

from src.citation_recovery import recover_citation_readings, ReviewedCitation, apply_reviewed_citation
from src.generation_preflight import require_source_reference
from src.pdf_extractor import extract_pdf, PdfExtractionError
from src.providers.base import ChunkGenerationError
from src.visual_source import recover_visual_source
from test_source_packages import TEXT
from test_visual_source import scan, multi_scan

DOI = 'doi:10.1234/synthetic05'
LOG = logging.getLogger('milestone5.synthetic')
LOG.addHandler(logging.NullHandler()); LOG.propagate = False


def make_pdf(tmp_path, bodies, *, citation=DOI, scanned_front=False, columns=False, table=False, damaged=False):
    path = tmp_path/'synthetic.pdf'
    with fitz.open() as pdf:
        if scanned_front:
            with fitz.open() as front:
                page = front.new_page(width=600, height=800)
                page.insert_text((40, 55), 'Fictional article for offline testing')
                page.insert_text((40, 80), citation)
                png = page.get_pixmap().tobytes('png')
            page = pdf.new_page(width=600, height=800); page.insert_image(page.rect, stream=png)
        for position, body in enumerate(bodies):
            page = pdf.new_page(width=600, height=900)
            if not scanned_front and position == 0 and citation:
                page.insert_text((40, 30), citation, fontsize=9)
            page.insert_text((40, 70), 'Clinical study methods', fontsize=12)
            rectangle = fitz.Rect(40, 100, 285 if columns else 550, 800)
            assert page.insert_textbox(rectangle, body, fontsize=9) > 0
            if columns:
                assert page.insert_textbox(fitz.Rect(320, 100, 565, 800),
                    'Supplementary material\nSource relationships require review.', fontsize=9) > 0
            if table:
                page.insert_text((320, 820), 'Table 1. Treatment results', fontsize=9)
                page.draw_rect(fitz.Rect(320, 830, 550, 870))
            if damaged:
                page.insert_text((40, 840), 'Treatment alpha: (cid:42) mg; damaged numerical qualification.', fontsize=9)
        pdf.save(path)
    return path


class Provider:
    deployment = 'synthetic'; endpoint = 'https://example.invalid'; api_version = 'synthetic'

    def __init__(self, bodies):
        self.bodies = bodies; self.calls = 0; self.image_calls = 0; self.fail_text = None
        self.lock = threading.Lock()

    def generate(self, article_text, source_file, prompt):
        with self.lock: self.calls += 1
        normalized = ' '.join(article_text.split())
        body = next(b for b in self.bodies if ' '.join(b.split()) in normalized)
        if body == self.fail_text:
            raise ChunkGenerationError('Confidential provider content must never be logged',
                code='TRANSPORT_TIMEOUT', diagnostics={'error': {'retryable': True}})
        common = dict(topic='Chronic colitis', subtopic='Study protocol', reference=DOI)
        return json.dumps({'article': dict(common, source_file=source_file, number_of_chunks=1),
            'chunks': [dict(common, section='Study population and clinical assessment protocol', original_text=body)]})

    def transcribe_image(self, png, prompt):
        self.image_calls += 1
        return json.dumps({'paragraphs': [], 'uncertainties': ['Synthetic visual material requires source review']})


def run_pdf(monkeypatch, tmp_path, path, provider, *, reviewed_citation=None):
    from src import pipeline, chunk_generator
    from src.providers.azure import AzureChunkGenerator
    monkeypatch.setattr(pipeline, 'OUTPUT_DIR', tmp_path/'outputs')
    monkeypatch.setattr(chunk_generator, '_provider_from_environment', lambda: provider)
    monkeypatch.setattr(AzureChunkGenerator, 'from_environment', classmethod(lambda cls: provider))
    started = perf_counter()
    result = pipeline.process_pdf(path, draft_packages=True, test_generation=True, logger=LOG,
                                  reviewed_citation=reviewed_citation)
    elapsed = perf_counter()-started
    output = Path(result['output_dir'])
    report = json.loads((output/'validation_report.json').read_text(encoding='utf-8'))
    chunks = json.loads((output/'chunks.json').read_text(encoding='utf-8'))['chunks'] if (output/'chunks.json').exists() else []
    return result, report, chunks, elapsed


def outcome(result, report, chunks, elapsed, provider):
    diag = report.get('diagnostics') or {}
    return {'status': result['status'], 'failure_code': result.get('failure_code'),
        'validation': report['status'], 'saved_candidates': len(chunks), 'elapsed_seconds': round(elapsed, 6),
        'mock_generation_calls': provider.calls, 'mock_image_calls': provider.image_calls,
        'source_holds': diag.get('source_holds'),
        'unit_failure_codes': [u['code'] for u in diag.get('unit_failures', [])],
        'validation_rejections': len(diag.get('validation_rejections', [])),
        'review_required_chunks': report.get('summary', {}).get('review_required_chunks'),
        'rejected_chunks': report.get('summary', {}).get('rejected_chunks'),
        'coverage': report.get('document_concept_coverage', {}).get('coverage_counts'),
        'complete': report.get('human_review', {}).get('source_document_complete', False),
        'request_totals': diag.get('observability', {}).get('request_totals', {}),
        'units_resumed': diag.get('units_resumed', 0)}


@pytest.mark.parametrize('kind', ['research', 'long_review', 'guideline', 'columns', 'statistics', 'table', 'damaged_numeric'])
def test_fresh_pdf_acceptance(kind, tmp_path, monkeypatch, record_property):
    bodies = [TEXT]
    if kind == 'long_review':
        bodies = [TEXT.replace('alpha', name) for name in
                  ('alpha', 'beta', 'gamma', 'delta', 'epsilon', 'zeta', 'eta', 'theta', 'iota')]
    if kind == 'guideline':
        bodies = [TEXT.replace('Adults with active colitis received treatment alpha for 8 weeks under the study protocol;',
            'For adults with active colitis, the fictional panel conditionally suggests treatment alpha; certainty is low;')]
    if kind == 'statistics':
        bodies = [TEXT+' At 8 weeks, 20 of 50 adults reported nausea with alpha; limited follow-up precludes long-term safety conclusions.']
    path = make_pdf(tmp_path, bodies, columns=kind=='columns', table=kind=='table', damaged=kind=='damaged_numeric')
    provider = Provider(bodies)
    result, report, chunks, elapsed = run_pdf(monkeypatch, tmp_path, path, provider)
    record_property('acceptance', json.dumps(outcome(result, report, chunks, elapsed, provider)))
    assert report.get('human_review', {}).get('source_document_complete') is not True
    assert all(c['original_text'] in bodies for c in chunks)
    assert result['status'] == 'partial_success', outcome(result, report, chunks, elapsed, provider)
    assert len(chunks) == len(bodies)
    assert report['summary']['clinically_approved_chunks'] == 0
    if kind == 'damaged_numeric':
        assert report['diagnostics']['source_holds'] > 0
    if kind == 'statistics':
        assert '20 of 50 adults' in chunks[0]['original_text']
        assert 'long-term safety' in chunks[0]['original_text']


@pytest.mark.parametrize('citation', ['', 'Fictional Journal 2020;12:10?20', 'References\ndoi:10.1234/unrelated'])
def test_missing_damaged_or_reference_list_citation_holds(citation, tmp_path, monkeypatch, record_property):
    path = make_pdf(tmp_path, [TEXT], citation=citation); provider = Provider([TEXT])
    result, report, chunks, elapsed = run_pdf(monkeypatch, tmp_path, path, provider)
    record_property('acceptance', json.dumps(outcome(result, report, chunks, elapsed, provider)))
    assert result['failure_code'] == 'REFERENCE_IDENTITY_UNRESOLVED'
    assert not chunks and provider.calls == provider.image_calls == 0


def citation_provider(lines=None):
    p = Mock(); p.transcribe_image.return_value = json.dumps({'lines': lines or [DOI], 'uncertainties': []})
    return p


def reviewed(readings):
    return ReviewedCitation(readings=readings, reading_id=readings.reading_id, review_id='a'*64,
        focal_article_confirmed=True, identifier_matches_rendered_source=True)


def test_scanned_front_requires_explicit_review_then_saves_native_draft(tmp_path, monkeypatch, record_property):
    path = make_pdf(tmp_path, [TEXT], scanned_front=True)
    provider = Provider([TEXT])
    first = run_pdf(monkeypatch, tmp_path, path, provider)
    assert first[0]['failure_code'] == 'REFERENCE_IDENTITY_UNRESOLVED'
    assert provider.calls == provider.image_calls == 0
    reader = citation_provider()
    readings = recover_citation_readings(path, provider=reader, cache_directory=tmp_path/'citation', max_pages=1)
    assert reader.transcribe_image.call_count == 2 and readings.candidate_reference() == DOI
    extraction = extract_pdf(path); before = extraction['full_text']
    applied = apply_reviewed_citation(path, extraction, reviewed(readings))
    assert applied['full_text'] == before and extraction.get('reviewed_citation') is None
    assert require_source_reference(applied) == DOI
    assert len(applied['reference_metadata']['source_spans']) == 2
    final = run_pdf(monkeypatch, tmp_path, path, provider, reviewed_citation=reviewed(readings))
    record_property('acceptance', json.dumps(outcome(*final, provider)))
    assert final[0]['status'] == 'partial_success' and len(final[2]) == 1
    # Stored attestation is reverified, not trusted merely because a cache exists.
    provider.calls = provider.image_calls = 0
    cached = run_pdf(monkeypatch, tmp_path, path, provider)
    assert len(cached[2]) == 1 and provider.calls == provider.image_calls == 0


@pytest.mark.parametrize('lines', [['References', DOI], ['doi:10.1234/a', 'doi:10.1234/b'], ['Unreadable citation']])
def test_citation_cannot_use_unrelated_ambiguous_or_missing_identity(lines, tmp_path):
    path = make_pdf(tmp_path, [TEXT], scanned_front=True)
    readings = recover_citation_readings(path, provider=citation_provider(lines), cache_directory=tmp_path/'cache', max_pages=1)
    assert not readings.candidate_reference()
    with pytest.raises(ValueError, match='incompatible'):
        apply_reviewed_citation(path, extract_pdf(path), reviewed(readings))


def test_citation_resume_bounds_mismatch_and_redaction(tmp_path):
    path = make_pdf(tmp_path, [TEXT], scanned_front=True); p = citation_provider()
    reply = p.transcribe_image.return_value
    p.transcribe_image.side_effect = [reply, ChunkGenerationError('private', code='TRANSPORT_TIMEOUT')]
    with pytest.raises(ChunkGenerationError):
        recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=1)
    p.transcribe_image.side_effect = None; p.transcribe_image.reset_mock()
    readings = recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=1)
    assert p.transcribe_image.call_count == 1
    assert DOI not in repr(readings) and DOI not in repr(reviewed(readings))
    p.transcribe_image.reset_mock()
    assert recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=1,
        allow_network=False) == readings
    p.transcribe_image.assert_not_called()
    with pytest.raises(ValueError, match='restricted'):
        recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=3)
    altered = readings.model_copy(deep=True); altered.pages[0].image_sha256 = 'b'*64
    with pytest.raises(ValueError, match='rendering'):
        apply_reviewed_citation(path, extract_pdf(path), reviewed(altered))
    altered = readings.model_copy(deep=True); altered.pages[0].passes[1].lines = ['doi:10.1234/other']
    assert not altered.candidate_reference()
    with fitz.open(path) as pdf:
        pdf.set_metadata({'title': 'changed'}); pdf.saveIncr()
    with pytest.raises(ValueError, match='incompatible'):
        apply_reviewed_citation(path, extract_pdf(path), reviewed(readings))


def test_reviewed_citation_rejects_native_conflicts_and_stale_attestation(tmp_path, monkeypatch):
    path = make_pdf(tmp_path, [TEXT], scanned_front=True)
    readings = recover_citation_readings(path, provider=citation_provider(), cache_directory=tmp_path/'cache', max_pages=1)
    e = extract_pdf(path); e['reference_metadata'] = {'identity_status': 'conflict'}
    with pytest.raises(ValueError, match='incompatible'):
        apply_reviewed_citation(path, e, reviewed(readings))
    review = reviewed(readings); review.reading_id = 'b'*64
    provider = Provider([TEXT])
    result, report, chunks, _ = run_pdf(monkeypatch, tmp_path, path, provider, reviewed_citation=review)
    assert result['failure_code']=='REFERENCE_IDENTITY_UNRESOLVED'
    assert result['failure_stage']=='source_identity' and not chunks
    assert provider.calls == provider.image_calls == 0


def test_front_matter_reader_has_four_read_ceiling_and_content_free_telemetry(tmp_path):
    from src import diagnostics as telemetry
    path = make_pdf(tmp_path, [TEXT, TEXT, TEXT]); p = citation_provider()
    observation = telemetry.Observation()
    with telemetry.bind(observation):
        readings = recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache')
    assert len(readings.pages)==2 and p.transcribe_image.call_count==4
    snap = observation.snapshot()
    assert snap['request_totals']['citation_transcription_calls']==4
    assert snap['request_totals']['transcription_calls']==4
    assert not snap['request_totals']['http_attempts_complete']
    assert DOI not in json.dumps(snap) and 'synthetic.pdf' not in json.dumps(snap)


def test_partial_drafts_survive_timeout_resume_and_duplicate_upload(tmp_path, monkeypatch, record_property):
    bodies = [TEXT, TEXT.replace('Adults', 'Children').replace('alpha', 'beta')]
    path = make_pdf(tmp_path, bodies); provider = Provider(bodies); provider.fail_text = bodies[1]
    first = run_pdf(monkeypatch, tmp_path, path, provider)
    snapshots = {'partial': outcome(*first, provider)}
    assert len(first[2]) == 1 and first[0]['status'] == 'partial_success'
    first_bytes = (Path(first[0]['output_dir'])/'chunks.json').read_bytes()
    provider.calls = 0; provider.fail_text = None
    resumed = run_pdf(monkeypatch, tmp_path, path, provider)
    snapshots['resumed'] = outcome(*resumed, provider)
    assert len(resumed[2]) == 2 and provider.calls == 1
    assert (Path(first[0]['output_dir'])/'chunks.json').read_bytes() == first_bytes
    provider.calls = 0
    duplicate = run_pdf(monkeypatch, tmp_path, path, provider)
    snapshots['duplicate'] = outcome(*duplicate, provider)
    assert duplicate[2] == resumed[2] and provider.calls == 0
    # Same bytes, changed filename: the source-file identity is intentionally incompatible.
    renamed = tmp_path/'renamed.pdf'; renamed.write_bytes(path.read_bytes())
    provider.calls = 0
    renamed_run = run_pdf(monkeypatch, tmp_path, renamed, provider)
    snapshots['renamed'] = outcome(*renamed_run, provider)
    record_property('acceptance', json.dumps(snapshots))
    assert len(renamed_run[2]) == 2 and provider.calls == 2
    assert all(c['reference'] == DOI for c in renamed_run[2])


def test_fully_scanned_qualitative_source_saves_only_review_draft(tmp_path, monkeypatch, record_property):
    text = TEXT.replace('8 weeks', 'the protocol treatment period')
    native = make_pdf(tmp_path, [text], scanned_front=True)
    path = tmp_path/'fully-scanned.pdf'
    with fitz.open(native) as source, fitz.open() as pdf:
        for page in source:
            target = pdf.new_page(width=page.rect.width, height=page.rect.height)
            target.insert_image(target.rect, stream=page.get_pixmap().tobytes('png'))
        pdf.save(path)
    with fitz.open(path) as pdf:
        clinical_image = sha256(pdf[1].get_pixmap(matrix=fitz.Matrix(1.5,1.5)).tobytes('png')).hexdigest()
    class ScannedProvider(Provider):
        def transcribe_image(self, png, prompt):
            with self.lock: self.image_calls += 1
            return json.dumps({'paragraphs': [text] if sha256(png).hexdigest()==clinical_image else [],
                               'uncertainties': []})
    reader = citation_provider()
    readings = recover_citation_readings(path, provider=reader, cache_directory=tmp_path/'citation', max_pages=1)
    provider = ScannedProvider([text])
    run = run_pdf(monkeypatch, tmp_path, path, provider, reviewed_citation=reviewed(readings))
    record_property('acceptance', json.dumps(outcome(*run, provider)))
    assert len(run[2]) == 1 and run[0]['status'] == 'partial_success'
    assert run[1]['human_review']['approved'] is False
    assert run[1]['human_review']['source_document_complete'] is False
    assert any(i['rule']=='VISUAL_SOURCE_REVIEW' for i in run[1]['issues'])


def test_adversarial_numeric_generation_is_held_and_not_repaid_on_resume(tmp_path, monkeypatch, record_property):
    class InventingProvider(Provider):
        def generate(self, *args):
            reply = json.loads(super().generate(*args))
            reply['chunks'][0]['original_text'] += ' There were 999999999 participants.'
            return json.dumps(reply)
    path = make_pdf(tmp_path, [TEXT]); provider = InventingProvider([TEXT])
    run = run_pdf(monkeypatch, tmp_path, path, provider)
    record_property('acceptance', json.dumps(outcome(*run, provider)))
    assert not run[2] and run[0]['failure_code']=='SOURCE_VALIDATION_FAILED'
    assert provider.calls == 2  # Original candidate and one bounded correction.
    provider.calls = 0
    run = run_pdf(monkeypatch, tmp_path, path, provider)
    assert not run[2] and provider.calls == 0


def test_corrupted_pdf_fails_before_provider(tmp_path, monkeypatch, record_property):
    path = tmp_path/'broken.pdf'; path.write_bytes(b'%PDF-1.4 corrupted synthetic fixture')
    provider = Provider([TEXT]); run = run_pdf(monkeypatch, tmp_path, path, provider)
    record_property('acceptance', json.dumps(outcome(*run, provider)))
    assert run[0]['failure_stage']=='extraction' and not run[2]
    assert provider.calls == provider.image_calls == 0


def test_invalid_citation_pass_rebuilds_only_missing_compatible_work(tmp_path):
    path = make_pdf(tmp_path, [TEXT], scanned_front=True); p = citation_provider()
    recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=1)
    next((tmp_path/'cache').glob('*-pass-2.json')).write_text('{broken')
    p.transcribe_image.reset_mock()
    with pytest.raises(ChunkGenerationError) as caught:
        recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=1, allow_network=False)
    assert caught.value.code == 'SOURCE_PASSAGE_HELD'; p.transcribe_image.assert_not_called()
    recovered = recover_citation_readings(path, provider=p, cache_directory=tmp_path/'cache', max_pages=1)
    assert recovered.candidate_reference() == DOI and p.transcribe_image.call_count == 1


def test_visual_unexpected_exception_drains_other_success_and_propagates(tmp_path):
    path, e, text = scan(tmp_path); p = Mock()
    reply = json.dumps({'paragraphs': [text], 'uncertainties': []})
    p.transcribe_image.side_effect = [RuntimeError('synthetic worker failure'), reply]
    with pytest.raises(RuntimeError, match='synthetic worker failure'):
        recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache')
    assert p.transcribe_image.call_count == 2 and len(list((tmp_path/'cache').glob('*-pass-*.json'))) == 1
    p.transcribe_image.side_effect = None; p.transcribe_image.return_value = reply; p.transcribe_image.reset_mock()
    assert len(recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache')['visual_source_pages']) == 1
    assert p.transcribe_image.call_count == 1


def test_provider_request_parameters_remain_unchanged():
    from src.providers.azure import AzureChunkGenerator
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
        message=SimpleNamespace(content='{}', refusal=None))], usage=None, id='synthetic')
    client = Mock(); client.chat.completions.create.return_value = response
    factory = Mock(return_value=client)
    provider = AzureChunkGenerator('synthetic', 'fake', 'https://example.invalid', '2024-12-01-preview',
        client_factory=factory, sleep=lambda _: None)
    provider.generate('fictional test source', 'synthetic.pdf', 'synthetic instructions')
    assert set(client.chat.completions.create.call_args.kwargs) == {'model', 'messages'}
    assert factory.call_args.kwargs['max_retries'] == 0
    assert factory.call_args.kwargs['timeout'] == 120.0


def test_visual_failures_clear_after_successful_resume_and_corrupt_pass_is_rebuilt(tmp_path):
    path, e, text = scan(tmp_path); p = Mock()
    reply = json.dumps({'paragraphs': [text], 'uncertainties': []})
    p.transcribe_image.side_effect = [reply, ChunkGenerationError('private', code='TRANSPORT_TIMEOUT')]
    first = recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache', max_concurrency=1)
    assert first['visual_recovery_outcome']['failures'][0]['pass_number'] == 2
    p.transcribe_image.side_effect = None; p.transcribe_image.return_value = reply; p.transcribe_image.reset_mock()
    final = recover_visual_source(path, first, provider=p, cache_directory=tmp_path/'cache', max_concurrency=1)
    assert p.transcribe_image.call_count == 1
    assert not final['visual_recovery_outcome']['failures']
    assert final['visual_recovery_outcome']['failure_history'][0]['code'] == 'TRANSPORT_TIMEOUT'
    next((tmp_path/'cache').glob('*-pass-1.json')).write_text('{broken')
    p.transcribe_image.reset_mock()
    recovered = recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache')
    assert len(recovered['visual_source_pages']) == 1 and p.transcribe_image.call_count == 1


def test_visual_pair_accounts_both_failures_and_stops_for_authentication(tmp_path):
    path, e, _ = multi_scan(tmp_path, 3); p = Mock()
    p.transcribe_image.side_effect = [ChunkGenerationError('private', code='TRANSPORT_TIMEOUT'),
                                     ChunkGenerationError('private', code='AUTHENTICATION_FAILED')]
    recovered = recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache')
    assert p.transcribe_image.call_count == 2
    assert [(f['pass_number'], f['code']) for f in recovered['visual_recovery_outcome']['failures']] == [
        (1, 'TRANSPORT_TIMEOUT'), (2, 'AUTHENTICATION_FAILED')]


def test_invalid_visual_cache_without_network_permission_is_a_hold(tmp_path):
    from src.queue_recovery import recovery_decision
    path, e, text = scan(tmp_path); p = Mock()
    p.transcribe_image.return_value = json.dumps({'paragraphs': [text], 'uncertainties': []})
    recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache')
    next((tmp_path/'cache').glob('*-pass-1.json')).write_text('{broken')
    p.transcribe_image.reset_mock()
    result = recover_visual_source(path, e, provider=p, cache_directory=tmp_path/'cache', allow_network=False)
    p.transcribe_image.assert_not_called()
    failures = result['visual_recovery_outcome']['failures']
    assert len(failures)==1 and failures[0]['code']=='SOURCE_PASSAGE_HELD'
    assert recovery_decision({'recovery_signals': failures}) is None


@pytest.mark.parametrize('code', ['SOURCE_PASSAGE_HELD', 'SOURCE_VALIDATION_FAILED', 'REFERENCE_IDENTITY_UNRESOLVED',
                                 'EXTRACTION_BLOCKED', 'CONTENT_FILTERED', 'MODEL_REFUSAL'])
def test_source_holds_cannot_be_overridden_by_retry_hint(code):
    from src.queue_recovery import recovery_decision
    assert recovery_decision({'status': 'failed', 'failure_code': code, 'retryable': True}) is None
    assert recovery_decision({'recovery_signals': [{'code': code, 'retryable': True},
        {'code': 'TRANSPORT_TIMEOUT', 'retryable': True}]}).retry


@pytest.mark.parametrize(('finish', 'refusal', 'code'), [('stop', None, None), ('length', None, 'OUTPUT_TRUNCATED'),
    ('content_filter', None, 'CONTENT_FILTERED'), ('stop', 'private', 'MODEL_REFUSAL'),
    (None, None, 'MALFORMED_RESPONSE'), ('tool_calls', None, 'UNEXPECTED_COMPLETION')])
def test_provider_completion_handling_does_not_accept_truncation(finish, refusal, code):
    from src.providers.azure import _extract_chat_message_content
    value = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
        message=SimpleNamespace(content='{"valid":"json"}', refusal=refusal))])
    if code:
        with pytest.raises(ChunkGenerationError) as caught: _extract_chat_message_content(value)
        assert caught.value.code == code and 'private' not in str(caught.value)
    else:
        assert _extract_chat_message_content(value) == '{"valid":"json"}'

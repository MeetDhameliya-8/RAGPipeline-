"""Latency changes must preserve source identity, unit ownership and rejection."""
import json
import threading
from unittest.mock import Mock

import pytest

from src.chunk_generator import generate_chunks
from src.generation_preflight import require_source_reference
from src.pdf_extractor import extract_document_reference_metadata, _select_reference_metadata
from src.providers.base import ChunkGenerationError
from test_source_packages import fixture, response


@pytest.mark.parametrize('line', [
    'Sample Journal. 2023 May 01; 77(5): 1797\ufffd1835. doi:10.1234/source0323.',
    'Sample   Journal 2022;117:27\ufffd56. https://doi.org/10.1234/source1538; published online',
])
def test_publisher_citation_owns_only_the_readable_doi(line):
    pages = [{'page_number':1, 'raw_text':line}]
    meta = extract_document_reference_metadata(pages)
    assert meta['identity_status'] == 'source_supported'
    assert meta['reference'].startswith('doi:10.1234/source')
    assert '\ufffd' not in meta['reference']
    assert require_source_reference({'pages':pages,'reference_metadata':meta}) == meta['reference']


def test_spacing_is_not_a_bibliographic_conflict():
    a = {'reference':'Sample Journal 2020;12:100-110','journal':'Sample   Journal','publication_year':'2020'}
    b = dict(a, journal='Sample Journal')
    warnings = []
    assert _select_reference_metadata([a,b],warnings)['reference']
    assert not warnings


def test_disputed_bibliography_never_gets_silently_attached_to_doi():
    pages = [{'raw_text':'Long Sample Journal 2020;12:100-110\ndoi:10.1234/source01'}]
    a = extract_document_reference_metadata(pages)
    b = dict(a,journal='Short Sample Journal',doi='',reference='Short Sample Journal 2020;12:100-110',citation_candidates=[])
    selected = _select_reference_metadata([a,b],[])
    assert selected['reference'] == 'doi:10.1234/source01'
    assert selected['bibliography_status'] == 'review_required'
    assert selected['journal'] == '' and selected['citation'] == ''
    assert require_source_reference({'pages':pages,'reference_metadata':selected}) == selected['reference']
    b['doi']='10.1234/other02'
    assert _select_reference_metadata([a,b],[])['identity_status']=='conflict'


def test_missing_reference_stops_before_paid_recovery_and_generation(tmp_path,monkeypatch):
    from src import pipeline, chunk_generator, visual_source
    e,_,_=fixture(tmp_path)
    e['reference_metadata']={'identity_status':'conflict'}
    monkeypatch.setattr(pipeline,'OUTPUT_DIR',tmp_path/'output')
    monkeypatch.setattr(pipeline,'extract_pdf',lambda _:e)
    provider=Mock()
    monkeypatch.setattr(chunk_generator,'_provider_from_environment',lambda:provider)
    monkeypatch.setattr(visual_source,'recover_visual_source',lambda *a,**k:pytest.fail('Paid recovery must not start'))
    result=pipeline.process_pdf(tmp_path/'prose.pdf',draft_packages=True,test_generation=True)
    assert result['failure_code']=='REFERENCE_IDENTITY_UNRESOLVED'
    provider.generate.assert_not_called()
    report=json.loads((tmp_path/'output'/ 'prose'/'validation_report.json').read_text())
    assert report['diagnostics']['requests_started']==0


def test_two_requests_maximum_with_deterministic_ownership_and_resume(tmp_path):
    e,m,p=fixture(tmp_path,pages=4)
    barrier=threading.Barrier(2);lock=threading.Lock();active=maximum=0
    class Provider:
        def generate(self, article_text, source_file, prompt):
            nonlocal active,maximum
            with lock:
                active+=1;maximum=max(maximum,active)
            try:
                barrier.wait(timeout=5)
                assert 'not a complete' in prompt
                assert 'Most papers will require' not in prompt
                return response(e)
            finally:
                with lock:active-=1
    audit={}
    kwargs=dict(source_doc=m,generation_plan=p,attempt_directory=tmp_path/'units',package_concurrency=2)
    generated=generate_chunks(e,provider=Provider(),diagnostics=audit,**kwargs)
    assert maximum==2 and audit['requests_started']==4
    assert len(generated['chunks'])==1
    assert audit['chunk_ownership'][0]['unit_ids']==[v.unit_id for v in p.source_package_plan.packages]
    assert [u['unit_number'] for u in audit['units']]==[1,2,3,4]
    assert all(u['provider_seconds']>=0 for u in audit['units'])
    provider=Provider()
    provider.generate=Mock(side_effect=AssertionError('Completed units must resume'))
    resumed={}
    assert generate_chunks(e,provider=provider,diagnostics=resumed,**kwargs)==generated
    assert resumed['units_resumed']==4


def test_auth_failure_does_not_submit_the_remaining_document(tmp_path):
    e,m,p=fixture(tmp_path,pages=4)
    provider=Mock()
    provider.generate.side_effect=ChunkGenerationError('No access',code='AUTHENTICATION_FAILED')
    audit={}
    with pytest.raises(ChunkGenerationError,match='Every attempted'):
        generate_chunks(e,source_doc=m,generation_plan=p,provider=provider,
                        diagnostics=audit,package_concurrency=2)
    assert provider.generate.call_count==2
    assert audit['requests_started']==2 and len(audit['unit_failures'])==2


def test_valid_abstention_is_held_and_not_regenerated_on_resume(tmp_path):
    e,m,p=fixture(tmp_path)
    empty=json.loads(response(e));empty['chunks']=[];empty['article']['number_of_chunks']=0
    provider=Mock();provider.generate.return_value=json.dumps(empty)
    kwargs=dict(source_doc=m,generation_plan=p,provider=provider,attempt_directory=tmp_path/'units')
    for _ in range(2):
        with pytest.raises(ChunkGenerationError) as caught:
            generate_chunks(e,**kwargs)
        assert caught.value.code=='SOURCE_PASSAGE_HELD'
    assert provider.generate.call_count==1
    from src.queue_recovery import recovery_decision
    assert recovery_decision({'status':'failed','failure_code':'SOURCE_PASSAGE_HELD'}) is None


@pytest.mark.parametrize('count',[False,0.0,'0',1])
def test_malformed_empty_response_is_not_a_source_hold(count):
    from src.source_abstention import is_source_abstention
    candidate={'article':{'source_file':'source.pdf','topic':'','subtopic':'','reference':'',
                          'number_of_chunks':count},'chunks':[]}
    assert not is_source_abstention(candidate,'source.pdf')


def test_embedded_reference_heading_is_held_and_legacy_proof_still_loads(tmp_path):
    import fitz
    from src.pdf_extractor import extract_pdf
    from src.native_layout import recover_native_layout
    from src.source_map import build_source_map
    from src.source_packages import _build, build_source_package_plan, validate_source_package_plan
    from src.evidence_storage import save_source_map,load_source_map
    from test_source_packages import TEXT
    path=tmp_path/'references.pdf'
    with fitz.open() as pdf:
        page=pdf.new_page(width=600,height=1000)
        assert page.insert_textbox(fitz.Rect(40,50,550,950),TEXT+'\nREFERENCES\n'+TEXT,fontsize=10)>0
        pdf.save(path)
    e=recover_native_layout(extract_pdf(path));m=build_source_map(e)
    old=_build(m.evidence,20000,80,90)
    assert old.version=='native-prose-packages-v1'
    m.source_package_plan=old
    saved=tmp_path/'old-map.json';save_source_map(saved,m)
    assert load_source_map(saved).source_package_plan==old
    new=build_source_package_plan(e,m)
    validate_source_package_plan(new,m.evidence)
    assert new.version=='native-prose-packages-v2'
    assert any(h.reason=='reference_list' for h in new.holds)
    held=[h for h in new.holds if h.reason=='reference_list' and h.source_span]
    assert any('REFERENCES' in m.evidence.buffers[h.source_span.buffer_id].text[
        h.source_span.char_start:h.source_span.char_end] for h in held)


def test_source_validation_failure_is_held_until_rules_change(tmp_path,monkeypatch):
    from src import package_generation
    from test_source_packages import TEXT
    e,m,p=fixture(tmp_path)
    provider=Mock();provider.generate.return_value=response(e,TEXT+' There were 999999999 participants.')
    kwargs=dict(source_doc=m,generation_plan=p,provider=provider,attempt_directory=tmp_path/'units')
    for _ in range(2):
        with pytest.raises(ChunkGenerationError) as caught:generate_chunks(e,**kwargs)
        assert caught.value.code=='SOURCE_VALIDATION_FAILED'
    assert provider.generate.call_count==2  # Initial draft + one correction, never again on resume.
    monkeypatch.setattr(package_generation,'validation_fingerprint',lambda:'changed-rules')
    provider.generate.return_value=response(e)
    assert generate_chunks(e,**kwargs)['chunks']
    assert provider.generate.call_count==3

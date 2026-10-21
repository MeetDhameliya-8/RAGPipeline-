"""Reusable performance boundaries; synthetic sources only, no network."""
import json
import threading
from dataclasses import asdict
from pathlib import Path
from unittest.mock import Mock

import pytest

from src.evidence import build_evidence_document,get_evidence,validate_evidence,warn
from src.evidence_storage import save_source_map,load_source_map
from src.hybrid_routing import recovery_decisions
from src.native_layout import recover_native_layout
from src.preparation_cache import save_preparation,load_preparation,preparation_key
from src.source_map import build_source_map
from src.source_packages import SourceHold


def drawing_extraction(count):
    text='Methods\n\nAdults with colitis received treatment under the study protocol.'
    page={'page_number':1,'raw_text':text,'text':text,'extractor':'test'}
    result={'file_name':'drawing.pdf','full_text':text,'raw_full_text':text,'pages':[page],'page_count':1}
    blocks=[{'bbox':[i,500,i+.2,501],'native_order':i,'role':'visual_region','signal_kind':'drawing'} for i in range(count)]
    doc=build_evidence_document(result,pdf_sha256='a'*64,candidates=[dict(page,layout={
        'width':600,'height':800,'rotation':0,'blocks':blocks})])
    result['evidence']=doc.model_dump(mode='json')
    return result


@pytest.mark.parametrize('count',[100,400])
def test_drawing_warning_links_grow_linearly_without_losing_regions(tmp_path,count):
    e=recover_native_layout(drawing_extraction(count));doc=get_evidence(e)
    assert len([b for p in doc.pages for b in p.blocks if b.role=='visual_region'])==count
    assert len([w for w in doc.warnings if w.code=='visual_region_unclassified'])==count
    assert sum(len(r.warning_ids) for r in doc.processing_routes)<=3*count+10
    for r in doc.processing_routes:
        if r.kind=='visual_classification_review':
            assert r.status=='held' and any(w.block_id==r.block_ids[0] and w.warning_id in r.warning_ids for w in doc.warnings)
    m=build_source_map(e);path=tmp_path/'source.json';save_source_map(path,m)
    assert load_source_map(path).evidence==m.evidence
    envelope=json.loads(path.read_text())
    assert envelope['_source_map_storage']['uncompressed_bytes']<count*4000+20000


def test_warning_index_survives_external_list_replacement():
    doc=get_evidence(drawing_extraction(2));old=doc.warnings[0]
    doc.warnings=list(doc.warnings)
    warn(doc,old.code,old.message,old.page_number,old.critical,old.span,bbox=old.bbox,block_id=old.block_id)
    assert sum(w.warning_id==old.warning_id for w in doc.warnings)==1


def test_native_reference_and_blank_routes_do_not_request_images():
    doc=get_evidence(drawing_extraction(1));page=doc.pages[0]
    refs=[SourceHold(block_id='reference',page_number=1,reason='reference_list')]
    assert not recovery_decisions(doc,set(),refs)[0].image_reading
    doc.buffers[page.selected_buffer_id].text=''
    assert recovery_decisions(doc,set())[0].image_reading
    page.blocks=[]
    assert not recovery_decisions(doc,set())[0].image_reading


def test_native_package_priority_and_unresolved_paragraph_recovery():
    doc=get_evidence(drawing_extraction(1));page=doc.pages[0]
    assert not recovery_decisions(doc,{1})[0].image_reading
    doc.buffers[page.selected_buffer_id].text='Adults with colitis received treatment under the study protocol. '*20
    holds=[SourceHold(block_id='paragraph',page_number=1,reason='insufficient_coherent_source_length')]
    decision=recovery_decisions(doc,set(),holds)[0]
    assert decision.image_reading and decision.reason=='native_paragraph_order_unresolved'


def cache_fixture(tmp_path):
    from test_source_packages import fixture
    e,m,plan=fixture(tmp_path);return tmp_path/'prose.pdf',e,m


def test_preparation_checkpoint_is_reusable_and_checksum_guarded(tmp_path):
    path,e,m=cache_fixture(tmp_path);directory=tmp_path/'cache'
    save_preparation(path,e,m,directory)
    restored=load_preparation(path,directory)
    assert restored and restored[1].model_dump_json()==m.model_dump_json()
    key,_=preparation_key(path)
    metadata=directory/key[:24]/'extraction.json'
    metadata.write_text('{}')
    assert load_preparation(path,directory) is None


def test_preparation_cache_binds_pdf_bytes_implementation_and_visual_provider(tmp_path,monkeypatch):
    path,e,m=cache_fixture(tmp_path);directory=tmp_path/'cache'
    save_preparation(path,e,m,directory)
    native_key=preparation_key(path)[0];visual_key=preparation_key(path,visual=True)[0]
    monkeypatch.setenv('AZURE_OPENAI_DEPLOYMENT','different-deployment')
    assert preparation_key(path)[0]==native_key
    assert preparation_key(path,visual=True)[0]!=visual_key
    path.write_bytes(path.read_bytes()+b'\nChanged source')
    assert load_preparation(path,directory) is None


def test_visual_passes_have_bounded_parallelism_and_cache_only_never_calls_provider(tmp_path):
    from test_visual_source import scan
    from src.visual_source import recover_visual_source
    path,e,text=scan(tmp_path);barrier=threading.Barrier(2);lock=threading.Lock();active=0;maximum=0
    class Provider:
        def transcribe_image(self,png,prompt):
            nonlocal active,maximum
            with lock:active+=1;maximum=max(maximum,active)
            try:barrier.wait(timeout=3);return json.dumps({'paragraphs':[text],'uncertainties':[]})
            finally:
                with lock:active-=1
    recover_visual_source(path,e,provider=Provider(),cache_directory=tmp_path/'cache')
    assert maximum==2
    provider=Mock();provider.transcribe_image.side_effect=AssertionError('Network forbidden')
    # A different cache/provider fingerprint with no saved passes is held.
    result=recover_visual_source(path,e,provider=provider,cache_directory=tmp_path/'empty',allow_network=False)
    assert not result['visual_source_pages'];provider.transcribe_image.assert_not_called()


def test_pipeline_restart_reuses_preparation_without_extracting(monkeypatch,tmp_path):
    from src import pipeline
    from test_source_packages import response
    from src import chunk_generator
    path,e,m=cache_fixture(tmp_path)
    monkeypatch.setattr(pipeline,'OUTPUT_DIR',tmp_path/'outputs')
    monkeypatch.setattr(pipeline,'extract_pdf',lambda p:e)
    provider=Mock();provider.generate.return_value=response(e)
    monkeypatch.setattr(chunk_generator,'_provider_from_environment',lambda:provider)
    first=pipeline.process_pdf(path,draft_packages=True,test_generation=True)
    assert first['status'] in {'processed','partial_success'}
    monkeypatch.setattr(pipeline,'extract_pdf',lambda p:pytest.fail('Verified checkpoint must skip extraction'))
    second=pipeline.process_pdf(path,draft_packages=True,test_generation=True)
    assert second['status'] in {'processed','partial_success'}
    report=json.loads((Path(second['output_dir'])/'validation_report.json').read_text())
    assert report['diagnostics']['preparation_checkpoint']['native_reused']
    assert provider.generate.call_count==1

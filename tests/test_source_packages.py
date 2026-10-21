"""Native source package drafting differs from clinical approval; offline tests."""
from src.source_packages import build_source_package_plan, validate_source_package_plan
from test_native_layout_recovery import pdf
from src.pdf_extractor import extract_pdf
from src.native_layout import recover_native_layout
from src.source_map import build_source_map


def test_supported_short_prose_is_preserved_as_held_not_padded(tmp_path):
    e=recover_native_layout(extract_pdf(pdf(tmp_path)));mapped=build_source_map(e)
    p=build_source_package_plan(e,mapped)
    validate_source_package_plan(p,mapped.evidence)
    assert not p.packages
    assert p.holds and not p.document_complete


TEXT = ("Adults with active colitis received treatment alpha for 8 weeks under the study protocol; "
"patients attended scheduled visits at the study center for collection of symptom diaries and assessment of medication use. "
"The study team recorded abdominal symptoms and stool frequency using the same questionnaire at each visit; "
"patients recorded daily symptoms before attending the clinic and brought the completed diary to the study team. "
"Study personnel documented concomitant medication use and reviewed the diary entries with each participant; "
"the protocol specified that clinical assessments would be recorded on the study forms and retained for review. "
"Participants continued attending the scheduled study visits during the treatment period and provided information "
"about tolerability and medication adherence; the clinical team documented the visit assessments together with "
"the participant diary records and retained the source records for subsequent review by the study investigators. "
"The protocol required study staff to record the date and location of each assessment on the source forms.")


def fixture(tmp_path,pages=1):
    import fitz
    path=tmp_path/'prose.pdf';d=fitz.open()
    for i in range(pages):
        p=d.new_page(width=600,height=800);p.insert_text((40,55),'Methods')
        body=TEXT if i==0 else TEXT.replace('Adults','Children').replace('alpha','beta')
        assert p.insert_textbox(fitz.Rect(40,100,550,650),body,fontsize=10)>0
    d.save(path);d.close()
    e=recover_native_layout(extract_pdf(path));e.update(reference_source_text='doi:10.1234/fixture123',reference_metadata={'doi':'10.1234/fixture123','reference':'doi:10.1234/fixture123'})
    mapped=build_source_map(e)
    from src.adaptive_ingestion import build_ingestion_plan,AdaptiveConfig
    plan=build_ingestion_plan(e,AdaptiveConfig(draft_packages=True),source_doc=mapped)
    return e,mapped,plan


def response(e,text=TEXT):
    import json
    common={'topic':'Chronic colitis','subtopic':'Study protocol','reference':e['reference_metadata']['reference']}
    return json.dumps({'article':dict(common,source_file=e['file_name'],number_of_chunks=1),
        'chunks':[dict(common,section='Study population and clinical assessment protocol',original_text=text)]})


def test_package_to_actual_validation_db_and_retrieval(tmp_path):
    import json
    from unittest.mock import Mock
    from src.chunk_generator import generate_chunks
    from src.source_packages import package_validation_sources
    from src.chunk_validator import validate_chunks
    from src.local_chunk_store import ingest_chunks_file,verify_chunks_file_ingested,retrieve
    e,mapped,plan=fixture(tmp_path)
    assert len(plan.batches)==1
    provider=Mock();provider.generate.return_value=response(e);audit={}
    generated=generate_chunks(e,source_doc=mapped,generation_plan=plan,provider=provider,diagnostics=audit,attempt_directory=tmp_path/'attempts')
    sources=package_validation_sources(plan.source_package_plan,mapped.evidence,audit)
    report=validate_chunks(generated,chunk_sources=sources)
    assert report['status']=='REVIEW_REQUIRED',report['issues']
    assert any(i['rule']=='SOURCE_CONTEXT' for i in report['issues'])
    output=tmp_path/'review';output.mkdir();path=output/'chunks.json';path.write_text(json.dumps(generated))
    (output/'validation_report.json').write_text(json.dumps(report))
    db=tmp_path/'review.sqlite3';stored=ingest_chunks_file(path,report['status'],db_path=db)
    assert stored['inserted']==1
    assert verify_chunks_file_ingested(path,report['status'],db_path=db)['verified']
    assert retrieve('colitis alpha symptom diaries',db_path=db)
    generated['chunks'][0]['original_text']+=' There were 999999999 participants.'
    assert any(i['rule']=='SOURCE_FIDELITY' for i in validate_chunks(generated,chunk_sources=sources)['issues'])


def test_later_unit_failure_preserves_first_and_resume_skips_provider(tmp_path):
    from unittest.mock import Mock
    from src.chunk_generator import generate_chunks
    from src.providers.base import ChunkGenerationError
    e,mapped,plan=fixture(tmp_path,pages=2)
    assert len(plan.batches)==2
    provider=Mock();provider.generate.side_effect=[response(e),ChunkGenerationError('unit two malformed')]
    audit={};directory=tmp_path/'attempts'
    first=generate_chunks(e,source_doc=mapped,generation_plan=plan,provider=provider,diagnostics=audit,attempt_directory=directory)
    assert len(first['chunks'])==1 and len(audit['unit_failures'])==1
    provider.generate.side_effect=None;provider.generate.return_value=response(e,TEXT.replace('Adults','Children').replace('alpha','beta'))
    provider.generate.reset_mock();audit={}
    resumed=generate_chunks(e,source_doc=mapped,generation_plan=plan,provider=provider,diagnostics=audit,attempt_directory=directory)
    assert len(resumed['chunks'])==2 and audit['units_resumed']==1
    assert provider.generate.call_count==1


def test_tampered_package_witness_cannot_be_loaded(tmp_path):
    import pytest
    from src.evidence_storage import save_source_map,load_source_map
    e,mapped,plan=fixture(tmp_path);path=tmp_path/'map.json'
    save_source_map(path,mapped);assert load_source_map(path).source_package_plan==plan.source_package_plan
    plan.source_package_plan.packages[0].witnesses[0].candidate_spans[0].char_start+=1
    path.write_text(mapped.model_dump_json())
    with pytest.raises(ValueError):load_source_map(path)


def test_unsupported_candidate_gets_one_source_constrained_regeneration(tmp_path):
    import json
    from unittest.mock import Mock
    from src.chunk_generator import generate_chunks
    e,mapped,plan=fixture(tmp_path);bad=json.loads(response(e))
    bad['chunks'][0]['original_text']=TEXT+' There were 999999999 participants.'
    provider=Mock();provider.generate.side_effect=[json.dumps(bad),response(e)];audit={}
    generated=generate_chunks(e,source_doc=mapped,generation_plan=plan,provider=provider,diagnostics=audit,attempt_directory=tmp_path/'attempts')
    assert provider.generate.call_count==2 and len(generated['chunks'])==1
    assert '999999999' not in generated['chunks'][0]['original_text']
    assert audit['validation_rejections'][0]['rejected_candidates']==1
    assert list((tmp_path/'attempts').glob('*-recovered-v1.json'))
    provider.generate.reset_mock();provider.generate.side_effect=AssertionError('Compatible validated recovery must resume')
    generate_chunks(e,source_doc=mapped,generation_plan=plan,provider=provider,diagnostics={},attempt_directory=tmp_path/'attempts')
    provider.generate.assert_not_called()


def test_resumption_survives_document_plan_budget_change(tmp_path):
    from unittest.mock import Mock
    from src.chunk_generator import generate_chunks
    from src.adaptive_ingestion import build_ingestion_plan,AdaptiveConfig
    e,mapped,plan=fixture(tmp_path);provider=Mock();provider.generate.return_value=response(e)
    directory=tmp_path/'attempts'
    generate_chunks(e,source_doc=mapped,generation_plan=plan,provider=provider,attempt_directory=directory)
    other=build_ingestion_plan(e,AdaptiveConfig(draft_packages=True,max_batches=81),source_doc=mapped)
    assert other.source_package_plan.plan_id!=plan.source_package_plan.plan_id
    provider.generate.reset_mock();provider.generate.side_effect=AssertionError('Completed unit must resume')
    audit={}
    generate_chunks(e,source_doc=mapped,generation_plan=other,provider=provider,attempt_directory=directory,diagnostics=audit)
    assert audit['units_resumed']==1
    provider.generate.assert_not_called()


def test_all_package_mode_expands_document_budget_without_truncating(tmp_path):
    from src.adaptive_ingestion import build_ingestion_plan,AdaptiveConfig
    e,mapped,plan=fixture(tmp_path,pages=2)
    blocked=build_ingestion_plan(e,AdaptiveConfig(draft_packages=True,max_batches=1),source_doc=mapped)
    assert blocked.blockers
    expanded=build_ingestion_plan(e,AdaptiveConfig(draft_packages=True,all_draft_packages=True,max_batches=1),source_doc=mapped)
    assert not expanded.blockers and len(expanded.batches)==len(plan.batches)==2

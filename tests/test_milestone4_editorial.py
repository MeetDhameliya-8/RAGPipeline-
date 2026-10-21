"""Offline editorial quality/safety checks. No qualified clinician labels."""
from copy import deepcopy
import json

import pytest

from editorial_support import cases,fixture,response,Provider
from src.chunk_generator import generate_chunks,DEFAULT_PROMPT_PATH
from src.claim_assessment import assess_editorial_chunk
from src.editorial_generation import generate_editorial_draft,EDITORIAL_PROMPT
from src.providers.base import ChunkGenerationError

CASES=cases()


@pytest.mark.parametrize('case',CASES,ids=lambda x:x['id'])
def test_editorial_examples_keep_envelope_evidence_and_mandatory_review(case):
    e,m,p=fixture(case);before=m.model_dump_json();old=deepcopy(e)
    provider=Provider(response(case));audit={}
    result=generate_chunks(e,provider=provider,source_doc=m,editorial_plan=p,diagnostics=audit)
    assert len(provider.calls)==1 and case['source'] in provider.calls[0][0]
    assert provider.calls[0][2] == EDITORIAL_PROMPT.read_text(encoding='utf-8')
    assert 'Do not paraphrase' not in provider.calls[0][2]
    assert set(result)=={'article','chunks'}
    assert set(result['chunks'][0])=={'chunk_number','topic','subtopic','section','reference','original_text'}
    assert result['chunks'][0]['original_text']==case['editorial']
    assert case['editorial'] != case['source']
    report=audit['editorial']['validation']
    assert report['status'] in {'REVIEW_REQUIRED','FAILED'}
    assert not report['clinically_approved'] and not report['document_complete']
    assert not report['qualified_clinician_reviewed']
    assert not any(r['status']=='PASS' for r in report['chunk_statuses'])
    assert report['claim_totals']['requires_review']>0
    assert report['source_ownership'][0]['ownership']
    assert m.model_dump_json()==before and e==old
    # Diagnostics contain source spans and output offsets, never passages.
    serialized=json.dumps(audit)
    assert case['source'] not in serialized and case['editorial'] not in serialized


ADVERSARIES=[(c,a) for c in CASES for a in c['adversaries']]


@pytest.mark.parametrize('case,attack',ADVERSARIES,ids=[c['id']+'-'+a['id'] for c,a in ADVERSARIES])
def test_adversarial_source_mutations_are_identified_and_never_approved(case,attack):
    e,m,p=fixture(case)
    assert attack['old'] in case['editorial']
    text=case['editorial'].replace(attack['old'],attack['new'])
    result=generate_editorial_draft(e,m,p,Provider(response(case,text)),{})
    report=result.validation
    assert report['chunk_statuses'][0]['status'] in {'REVIEW_REQUIRED','REJECTED'}
    findings=report['claim_assessments'][0]
    reasons=set(findings['reasons'])|{r for claim in findings['claims'] for r in claim['reasons']}
    assert attack['expected_reason'] in reasons
    assert not report['clinically_approved']


def test_literal_copy_is_only_literal_consistency_and_never_clinical_pass():
    c=CASES[0];e,m,p=fixture(c)
    result=generate_editorial_draft(e,m,p,Provider(response(c,c['source'])),{})
    assert result.validation['claim_totals']['supported']>0
    assert result.validation['chunk_statuses'][0]['status']=='REVIEW_REQUIRED'
    assert not result.validation['clinically_approved']


def test_short_and_long_drafts_are_not_padded_or_truncated_and_legacy_limits_remain():
    from src.chunk_validator import validate_chunks
    c=CASES[1];e,m,p=fixture(c);short=c['editorial']
    direct=generate_editorial_draft(e,m,p,Provider(response(c,short)),{})
    assert direct.generated['chunks'][0]['original_text']==short and len(short.split())<90
    assert any(i['rule']=='WORD_COUNT' and i['severity']=='WARNING' for i in direct.validation['issues'])
    legacy=validate_chunks(direct.generated,source_text=c['source'])
    assert any(i['rule']=='WORD_COUNT' and i['severity']=='ERROR' for i in legacy['issues'])
    long=' '.join([c['editorial']]*7)
    result=generate_editorial_draft(e,m,p,Provider(response(c,long)),{})
    assert len(long.split())>300 and result.generated['chunks'][0]['original_text']==long
    assert not any(i['rule']=='WORD_COUNT' and i['severity']=='ERROR' for i in result.validation['issues'])
    assert result.validation['claim_assessments'][0]['length_exception']=='source_specific_justification_required'


@pytest.mark.parametrize('mutation',['no_review','corrupt_source','stale_plan','no_provider','budget','context_budget'])
def test_preflight_blocks_incompatible_source_before_mock_call(mutation):
    from src.document_concepts import build_document_concept_plan
    c=CASES[0];e,m,p=fixture(c);provider=Provider(response(c));kwargs={}
    if mutation=='no_review':p=build_document_concept_plan(e,m)
    if mutation=='corrupt_source':m.evidence.buffers[m.evidence.canonical_buffer_id].text += ' SECRET'
    if mutation=='stale_plan':p.evidence_fingerprint='b'*64
    if mutation=='no_provider':provider=None
    if mutation=='budget':kwargs['max_requests']=0
    if mutation=='context_budget':kwargs['max_source_characters']=200
    with pytest.raises((ChunkGenerationError,ValueError)):
        generate_editorial_draft(e,m,p,provider,{},**kwargs)
    assert provider is None or not provider.calls


@pytest.mark.parametrize('kwargs',[{'attempt_directory':'unused'},{'generation_plan':object()},
    {'package_concurrency':2},{'retry_feedback':'legacy'},{'prompt_path':'custom'}, {'diagnostics':None}])
def test_optin_never_reuses_legacy_checkpoint_or_prompt_semantics(kwargs):
    c=CASES[0];e,m,p=fixture(c);provider=Provider(response(c))
    options={'diagnostics':{}}|kwargs
    with pytest.raises(ChunkGenerationError):generate_chunks(e,provider=provider,source_doc=m,editorial_plan=p,**options)
    assert not provider.calls


def test_missing_evidence_and_unknown_paraphrase_are_not_marked_supported():
    c=CASES[0];e,m,p=fixture(c);q=next(q for q in p.concepts if q.status=='source_bound')
    empty=assess_editorial_chunk(1,'Unsupported text.',m.evidence,[],[],q.roles)
    assert empty.status=='REJECTED' and empty.claims[0].status=='missing_evidence'
    owned=next(r.ownership for r in p.regions if r.region_id in q.owned_region_ids)
    result=assess_editorial_chunk(1,'Treatment alpha cures every patient.',m.evidence,[owned],[],q.roles)
    assert result.claims[0].status=='requires_review' and not result.clinically_approved


def test_same_arm_multiple_source_outcomes_are_ambiguous_not_inferred():
    from src.evidence import build_evidence_document
    from src.document_concepts import BoundRole
    from src.evidence import span
    text='Remission occurred in 40% with treatment alpha. Response occurred in 60% with treatment alpha.'
    e={'full_text':text,'raw_full_text':text,'pages':[{'page_number':1,'text':text,'raw_text':text}]}
    doc=build_evidence_document(e,pdf_sha256='a'*64)
    entity=span(doc.canonical_buffer_id,text.index('treatment alpha'),text.index('treatment alpha')+len('treatment alpha'))
    a=assess_editorial_chunk(1,'Response occurred in 40% with treatment alpha.',doc,
        [span(doc.canonical_buffer_id,0,len(text))],[],[BoundRole(role='intervention',evidence=entity)])
    assert 'arm_endpoint_quantity_ambiguous' in a.claims[0].reasons or a.claims[0].status=='requires_review'


@pytest.mark.parametrize('source_text,expected',[
    ('Remission was not statistically significant.','contradicted'),
    ('Remission was statistically significant. Response was not statistically significant.','requires_review')])
def test_significance_polarity_is_not_borrowed_from_another_named_endpoint(source_text,expected):
    from src.evidence import build_evidence_document,span
    from src.document_concepts import BoundRole
    e={'full_text':source_text,'raw_full_text':source_text,'pages':[{'page_number':1,'text':source_text,'raw_text':source_text}]}
    doc=build_evidence_document(e,pdf_sha256='a'*64)
    role=BoundRole(role='outcome',evidence=span(doc.canonical_buffer_id,0,len('Remission')))
    a=assess_editorial_chunk(1,'Remission was statistically significant.',doc,
        [span(doc.canonical_buffer_id,0,len(source_text))],[],[role])
    assert a.claims[0].status==expected


@pytest.mark.parametrize('output',['not JSON','{"article":{},"chunks":[]}',None])
def test_unexpected_or_malformed_provider_output_has_sanitized_failure(output):
    c=CASES[0];e,m,p=fixture(c);audit={}
    class BadProvider:
        def generate(self,*args):
            if output is None:raise RuntimeError('SECRET confidential medical passage')
            return output
    with pytest.raises(ChunkGenerationError) as exc:generate_editorial_draft(e,m,p,BadProvider(),audit)
    assert 'SECRET' not in str(exc.value) and 'confidential' not in json.dumps(audit)


def test_legacy_source_package_messages_and_validation_are_unchanged(tmp_path):
    from test_source_packages import fixture as native_fixture,response as native_response
    e,m,p=native_fixture(tmp_path)
    provider=Provider(native_response(e));audit={}
    result=generate_chunks(e,source_doc=m,generation_plan=p,provider=provider,diagnostics=audit)
    assert len(result['chunks'])==1 and 'editorial' not in audit
    assert 'Do not paraphrase clinical claims' in provider.calls[0][2]
    assert 'retaining exact wording' in provider.calls[0][2]
    assert result['chunks'][0]['original_text'] in e['full_text']


def test_editorial_calls_retain_logical_vs_unknown_http_accounting():
    c=CASES[0];e,m,p=fixture(c);audit={}
    generate_editorial_draft(e,m,p,Provider(response(c)),audit)
    totals=audit['observability']['request_totals']
    assert totals['logical_calls']==1 and totals['successful_responses']==1
    assert totals['calls_with_unknown_http_attempts']==1
    assert not totals['http_attempts_complete']


@pytest.mark.parametrize('mutation',['source_file','extra_claims','reference'])
def test_untrusted_model_metadata_never_becomes_source_authority(mutation):
    c=CASES[0];e,m,p=fixture(c);data=json.loads(response(c))
    if mutation=='source_file':data['article']['source_file']='another-source.pdf'
    if mutation=='extra_claims':data['chunks'][0]['claims']={'self_verified':True}
    if mutation=='reference':data['chunks'][0]['reference']='doi:10.1234/invented'
    with pytest.raises(ChunkGenerationError):generate_editorial_draft(e,m,p,Provider(json.dumps(data)),{})


def test_editorial_topic_must_retain_the_source_bound_searchable_entity():
    c=CASES[0];e,m,p=fixture(c);data=json.loads(response(c))
    data['chunks'][0]['topic']='Unrelated condition'
    result=generate_editorial_draft(e,m,p,Provider(json.dumps(data)),{})
    assert result.validation['chunk_statuses'][0]['status']=='REJECTED'
    assert any(i['rule']=='EDITORIAL_TOPIC_IDENTITY' for i in result.validation['issues'])


def test_number_of_source_bound_questions_is_not_capped_at_eight():
    from test_concept_planning import source
    from src.source_map import build_source_map
    from src.document_concepts import build_document_concept_plan,ReviewedQuestion,SourceReview,BoundRole
    from src.evidence import span
    names=['alpha','beta','gamma','delta','epsilon','zeta','eta','theta','iota','kappa']
    bodies=[f'Adults with condition delta underwent procedure lambda {n} at the study center. The report described protocol {n} as an independent procedure variant.' for n in names]
    e=source(['Methods\n\n'+'\n\n'.join(bodies)]);e.update(reference_source_text='doi:10.1234/synthetic123',
        reference_metadata={'reference':'doi:10.1234/synthetic123','doi':'10.1234/synthetic123'})
    m=build_source_map(e);base=build_document_concept_plan(e,m);reviews=[]
    for name,body in zip(names,bodies):
        a=e['full_text'].index(body);owned=next(r for r in base.regions if r.ownership and r.ownership.char_start<=a<r.ownership.char_end)
        def anchor(value):
            start=e['full_text'].index(value,a)
            return span(base.canonical_buffer_id,start,start+len(value))
        reviews.append(ReviewedQuestion(review=SourceReview(evidence_fingerprint=base.evidence_fingerprint,review_id='e'*64,basis='human_source_review'),
            question='source_statement',question_anchor=anchor('procedure lambda '+name),owned_region_ids=[owned.region_id],
            roles=[BoundRole(role='topic',evidence=anchor('procedure lambda '+name)),BoundRole(role='population',evidence=anchor('Adults with condition delta'))],
            relationship_evidence=[anchor(body)],same_population_domain_and_interventions=True,arm_outcome_timepoint_checked=True,all_qualifications_included=True))
    plan=build_document_concept_plan(e,m,reviews=reviews)
    class MultipleProvider:
        def __init__(self):self.calls=0
        def generate(self,text,file,prompt):
            i=self.calls;self.calls+=1
            c=dict(CASES[3],section='Procedure lambda '+names[i]+' study protocol')
            data=json.loads(response(c,bodies[i]));data['article']['source_file']=file
            return json.dumps(data)
    provider=MultipleProvider();draft=generate_editorial_draft(e,m,plan,provider,{})
    assert provider.calls==10 and draft.generated['article']['number_of_chunks']==10
    assert [c['chunk_number'] for c in draft.generated['chunks']]==list(range(1,11))
    blocked=MultipleProvider()
    with pytest.raises(ChunkGenerationError):generate_editorial_draft(e,m,plan,blocked,{},max_requests=9)
    assert blocked.calls==0

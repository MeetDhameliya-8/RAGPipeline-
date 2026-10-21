"""Engineer-authored synthetic review stubs, not qualified clinical annotations."""
from copy import deepcopy
import json
from pathlib import Path

from src.document_concepts import (BoundRole, ReviewedQuestion, SourceReview,
    build_document_concept_plan)
from src.evidence import build_evidence_document, span
from src.source_map import build_source_map

CORPUS = Path(__file__).parent/'fixtures'/'editorial_evaluation.json'


def cases():
    return json.loads(CORPUS.read_text(encoding='utf-8'))['cases']


def fixture(case):
    text='Methods\n\n'+case['source']
    page={'page_number':1,'text':text,'raw_text':text,'extractor':'synthetic'}
    extraction={'file_name':'synthetic-editorial.pdf','page_count':1,'pages':[page],
        'full_text':text,'raw_full_text':text,'reference_source_text':'doi:10.1234/synthetic123',
        'reference_metadata':{'reference':'doi:10.1234/synthetic123','doi':'10.1234/synthetic123'}}
    native=dict(page,layout={'width':600,'height':800,'blocks':[{'text':text,'bbox':[40,40,560,700],'native_order':0}]})
    extraction['evidence']=build_evidence_document(extraction,pdf_sha256='a'*64,candidates=[native]).model_dump(mode='json')
    mapped=build_source_map(extraction);before=build_document_concept_plan(extraction,mapped)
    body=next(r for r in before.regions if r.ownership and r.ownership.char_start <= len('Methods\n\n') < r.ownership.char_end)
    def anchor(value):
        a=text.index(value)
        return span(before.canonical_buffer_id,a,a+len(value))
    review=ReviewedQuestion(review=SourceReview(evidence_fingerprint=before.evidence_fingerprint,review_id='e'*64,basis='human_source_review'),
        question='source_statement',question_anchor=anchor(case['roles'][0][1]),owned_region_ids=[body.region_id],
        roles=[BoundRole(role=kind,evidence=anchor(value)) for kind,value in case['roles']],
        relationship_evidence=[anchor(case['source'])],same_population_domain_and_interventions=True,
        arm_outcome_timepoint_checked=True,all_qualifications_included=True)
    plan=build_document_concept_plan(extraction,mapped,reviews=[review])
    assert any(q.status=='source_bound' for q in plan.concepts),[(r.basis,r.reasons) for r in plan.regions]
    return extraction,mapped,plan


def response(case,text=None):
    chunk={k:case[k] for k in ['topic','subtopic','section']}
    chunk.update(reference='doi:10.1234/synthetic123',original_text=text if text is not None else case['editorial'])
    return json.dumps({'article':{'source_file':'synthetic-editorial.pdf','topic':chunk['topic'],
        'subtopic':chunk['subtopic'],'reference':chunk['reference'],'number_of_chunks':1},'chunks':[chunk]})


class Provider:
    def __init__(self,output):
        self.output=output;self.calls=[]

    def generate(self,article_text,source_file,prompt):
        self.calls.append((article_text,source_file,prompt))
        return self.output

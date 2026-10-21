"""Synthetic evidence only: planning is not medical or publication approval."""
from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from src.document_concepts import (BoundRole, DocumentConceptPlan, ReviewedExclusion,
    ReviewedQuestion, SourceReview, build_document_concept_plan, evidence_fingerprint,
    validate_document_concept_plan)
from src.evidence import get_evidence, span, warn
from src.source_map import build_source_map
from test_concept_planning import source


METHODS = ("In a randomized trial, adults with condition delta received treatment alpha or placebo. "
           "The primary outcome was remission at 8 weeks. The secondary outcome was symptom change at 4 weeks.")
PRIMARY = ("For adults with condition delta receiving treatment alpha or placebo, the primary outcome "
           "remission at 8 weeks was 40% versus 38%; the difference was not statistically significant (p=0.70).")
SECONDARY = ("For adults with condition delta receiving treatment alpha or placebo, the secondary outcome "
             "symptom change at 4 weeks was 10 points versus 9 points.")


def inventory(pages):
    e = source(pages)
    mapped = build_source_map(e)
    return e, mapped, build_document_concept_plan(e, mapped)


def locate(e, needle, *, start=0):
    a = e["full_text"].index(needle, start)
    return span(get_evidence(e).canonical_buffer_id, a, a+len(needle))


def region(plan, e, needle):
    a = e["full_text"].index(needle)
    return next(r for r in plan.regions if r.ownership and r.ownership.char_start <= a < r.ownership.char_end)


def review_stamp(plan):
    return SourceReview(evidence_fingerprint=plan.evidence_fingerprint,
                       review_id="e"*64, basis="human_source_review")


def primary_review(e, plan, *, primary=PRIMARY, population="adults with condition delta"):
    owned = region(plan, e, primary)
    context = region(plan, e, METHODS)
    a = e["full_text"].index(primary)
    roles = [("topic", "condition delta", 0), ("population", population, 0),
             ("intervention", "treatment alpha", 0), ("comparator", "placebo", 0),
             ("primary_outcome", "primary outcome", a), ("time_point", "8 weeks", a)]
    return ReviewedQuestion(review=review_stamp(plan), question="primary_outcome",
        question_anchor=locate(e, "primary outcome", start=a),
        owned_region_ids=[owned.region_id], context_region_ids=[context.region_id],
        roles=[BoundRole(role=k, evidence=locate(e, t, start=s)) for k,t,s in roles],
        relationship_evidence=[locate(e, METHODS), locate(e, primary)],
        same_population_domain_and_interventions=True, arm_outcome_timepoint_checked=True,
        all_qualifications_included=True)


def test_complete_inventory_is_deterministic_content_free_and_not_chunk_coverage():
    e, mapped, p = inventory(["Methods\n\n"+METHODS+"\n\nResults\n\n"+PRIMARY+"\n\n"+SECONDARY])
    assert p == build_document_concept_plan(e, mapped)
    assert DocumentConceptPlan.model_validate_json(p.model_dump_json()) == p
    validate_document_concept_plan(p, mapped)
    assert sum(p.canonical_character_counts.values()) == len(e["full_text"])
    assert sum(p.coverage_counts.values()) == len(p.regions)
    assert p.coverage_counts["represented_in_concept"] == 0
    assert p.coverage_counts["not_yet_assessed"] == 3
    assert p.coverage_counts["supporting_context_only"] == 2
    assert not p.document_complete and not p.clinically_approved and not p.planning_accounted
    assert all(c.status == "unresolved" and not c.roles and not c.relationship_evidence for c in p.concepts)
    assert all(c.relationship == "literal_cue_only" for r in p.regions for c in r.cues)
    serialized = p.model_dump_json()
    assert METHODS not in serialized and PRIMARY not in serialized and '"text":' not in serialized
    assert "adversarial" not in serialized
    assert all(sid.startswith('section:') and len(sid)==72 for r in p.regions for sid in r.section_ids)


def test_reviewed_trial_question_keeps_two_arms_primary_secondary_and_nonsignificance_separate():
    e, mapped, before = inventory(["Methods\n\n"+METHODS+"\n\nResults\n\n"+PRIMARY+"\n\n"+SECONDARY])
    r = primary_review(e, before)
    after = build_document_concept_plan(e, mapped, reviews=[r])
    q = next(c for c in after.concepts if c.relationship_basis == "human_source_review")
    assert q.status == "source_bound"
    assert q.owned_region_ids == [region(before,e,PRIMARY).region_id]
    assert q.context_region_ids == [region(before,e,METHODS).region_id]
    assert {x.role for x in q.roles} >= {"population","intervention","comparator","primary_outcome","time_point"}
    # Numbers, negation, p-value and time point remain in one entire source
    # region; no efficacy/significance conclusion is generated from a regex.
    owned = next(x for x in after.regions if x.region_id == q.owned_region_ids[0])
    assert e['full_text'][owned.ownership.char_start:owned.ownership.char_end].strip() == PRIMARY
    secondary = next(x for x in after.regions if x.region_id == region(before,e,SECONDARY).region_id)
    assert secondary.coverage == "not_yet_assessed" and not secondary.concept_ids
    assert any(c.question == "secondary_outcome" and c.status == "unresolved" for c in after.concepts)
    assert after.coverage_counts["represented_in_concept"] == 1
    assert not after.planning_accounted and not after.document_complete
    validate_document_concept_plan(after, mapped)


def test_cross_page_population_is_never_auto_bound_but_exact_review_can_supply_context():
    e, mapped, p = inventory(["Methods\n\n"+METHODS, "Results\n\n"+PRIMARY])
    assert all(not c.context_region_ids and c.status == "unresolved" for c in p.concepts)
    r = primary_review(e,p)
    q = next(c for c in build_document_concept_plan(e,mapped,reviews=[r]).concepts if c.relationship_basis == 'human_source_review')
    assert q.status == 'source_bound'
    assert region(p,e,METHODS).page_numbers == [1]
    assert region(p,e,PRIMARY).page_numbers == [2]
    assert all(f.precision == 'exact' for rid in q.owned_region_ids+q.context_region_ids for f in next(x for x in p.regions if x.region_id == rid).trace.fragments)


def test_conditional_guideline_strength_certainty_and_applicability_stay_literal():
    text = ("For adults with condition delta, the panel suggests treatment alpha only after shared discussion; "
            "this is a conditional recommendation with low certainty of evidence, excluding children.")
    e,m,p = inventory(["Recommendations\n\n"+text])
    body = region(p,e,text)
    assert {c.role for c in body.cues} >= {'population','recommendation','recommendation_strength','evidence_certainty','applicability','qualification'}
    r=ReviewedQuestion(review=review_stamp(p),question='recommendation',question_anchor=locate(e,'conditional recommendation'),
        owned_region_ids=[body.region_id], roles=[BoundRole(role=k,evidence=locate(e,t)) for k,t in [
            ('topic','condition delta'),('population','adults with condition delta'),('intervention','treatment alpha'),
            ('recommendation','suggests treatment alpha'),('recommendation_strength','conditional'),
            ('evidence_certainty','low certainty of evidence'),('applicability','excluding children')]],
        relationship_evidence=[locate(e,text)],same_population_domain_and_interventions=True,
        arm_outcome_timepoint_checked=True,all_qualifications_included=True)
    after=build_document_concept_plan(e,m,reviews=[r])
    assert next(c for c in after.concepts if c.review_id).status == 'source_bound'
    assert e['full_text'][body.ownership.char_start:body.ownership.char_end].strip() == text
    assert not after.document_complete


def test_adjacent_safety_qualification_cannot_be_omitted_even_with_review_attestation():
    safety='However, adverse events were assessed only during the first 2 weeks; delayed events were not measured.'
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+PRIMARY+'\n\n'+safety])
    r=primary_review(e,p)
    held=build_document_concept_plan(e,m,reviews=[r])
    assert next(c for c in held.concepts if c.review_id).status == 'held'
    assert 'adjacent_qualification_not_included' in next(c for c in held.concepts if c.review_id).reasons
    r.context_region_ids.append(region(p,e,safety).region_id)
    r.relationship_evidence.append(locate(e,safety))
    after=build_document_concept_plan(e,m,reviews=[r])
    q=next(c for c in after.concepts if c.review_id)
    assert q.status == 'source_bound' and region(p,e,safety).region_id in q.context_region_ids
    assert next(x for x in after.regions if x.region_id == region(p,e,safety).region_id).coverage == 'supporting_context_only'


@pytest.mark.parametrize('text,reason',[
    ('Patients receiving ABC had a response.','abbreviation_requires_source_review'),
    ('These patients had a response as described above.','referent_requires_source_review'),
    ('Patients had a response in Table 1.','structured_relationship_unproven'),
    ('Figure 2. Treatment alpha 20% placebo 10%.','structured_relationship_unproven'),
    ('* Adverse events exclude missing observations.','structured_relationship_unproven'),
    ('Adults received treatment \ufffd alpha.','damaged_source_passage')])
def test_unproven_relationships_and_damage_stay_visible(text,reason):
    e,m,p=inventory(['Results\n\n'+text])
    r=region(p,e,text)
    assert r.coverage == 'held_for_source_review' and reason in r.reasons
    assert any(c.owned_region_ids == [r.region_id] and c.status == 'held' for c in p.concepts)
    assert not p.planning_accounted and p.coverage_counts['held_for_source_review'] >= 1


def test_missing_canonical_passage_and_empty_page_are_held_not_erased():
    records=[{'page_number':1,'text':'Methods\n\nAdults received treatment alpha.',
              'raw_text':'Methods\n\nAdults received treatment alpha.\n\nAdverse events were not reported.', 'extractor':'fixture'},
             {'page_number':2,'text':'','raw_text':'','extractor':'fixture'}]
    e={'file_name':'synthetic.pdf','page_count':2,'pages':records,'full_text':records[0]['text'],
       'raw_full_text':records[0]['raw_text']}
    from src.evidence import build_evidence_document
    e['evidence']=build_evidence_document(e,pdf_sha256='a'*64).model_dump(mode='json')
    m=build_source_map(e);p=build_document_concept_plan(e,m)
    assert any(r.basis == 'unmapped_native' and r.trace.fragments for r in p.regions)
    assert any(r.basis == 'empty_page' and r.page_numbers == [2] for r in p.regions)
    assert sum(p.canonical_character_counts.values()) == len(e['full_text'])
    assert p.coverage_counts['held_for_source_review'] >= 2 and not p.planning_accounted


def test_unlocalized_warning_is_separate_concern_and_cannot_be_hidden_by_exclusion():
    e=source(['Methods\n\nAdults received treatment alpha.'])
    doc=get_evidence(e);warn(doc,'missing_source','Synthetic missing passage.',critical=True)
    e['evidence']=doc.model_dump(mode='json');m=build_source_map(e);p=build_document_concept_plan(e,m)
    assert any(r.basis == 'warning' and not r.ownership and r.warning_ids for r in p.regions)
    r=region(p,e,'Adults')
    exclusion=ReviewedExclusion(review=review_stamp(p),region_id=r.region_id,reason='nonclinical_boilerplate',source_basis=locate(e,'Adults'))
    with pytest.raises(ValueError,match='uncertainty'):
        build_document_concept_plan(e,m,exclusions=[exclusion])


def test_excluded_references_remain_visible_with_justification():
    e,m,p=inventory(['Methods\n\nAdults received treatment alpha.\n\nReferences\n\nA synthetic bibliography entry.'])
    r=region(p,e,'A synthetic')
    ex=ReviewedExclusion(review=review_stamp(p),region_id=r.region_id,reason='reference_list',source_basis=locate(e,'A synthetic bibliography entry.'))
    after=build_document_concept_plan(e,m,exclusions=[ex])
    excluded=next(x for x in after.regions if x.region_id == r.region_id)
    assert excluded.coverage == 'intentionally_excluded' and excluded.reasons == ['reviewed_reference_list']
    assert excluded.ownership and excluded.trace.fragments and after.exclusions == [ex]
    assert after.coverage_counts['not_yet_assessed'] == 1 and not after.document_complete


@pytest.mark.parametrize('mutation',['full_text','buffer','mapping','section','warning','pdf','review','plan','source_map_config','source_map_content'])
def test_corrupted_identity_stale_review_or_modified_plan_is_rejected(mutation):
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+PRIMARY])
    r=primary_review(e,p);after=build_document_concept_plan(e,m,reviews=[r])
    if mutation == 'full_text':
        e['full_text'] += ' Modified.'
        with pytest.raises(ValueError):build_document_concept_plan(e,m,reviews=[r])
        return
    if mutation == 'buffer':m.evidence.buffers[m.evidence.canonical_buffer_id].text += ' Modified.'
    if mutation == 'mapping':m.evidence.mappings[m.evidence.canonical_buffer_id][0].target.char_end -= 1
    if mutation == 'section':m.sections[0].section_id += ':modified'
    if mutation == 'warning':warn(m.evidence,'new_gap','Synthetic warning.',critical=True)
    if mutation == 'pdf':m.evidence.pdf_sha256='b'*64
    if mutation == 'review':after.reviews[0].review.evidence_fingerprint='b'*64
    if mutation == 'plan':after.regions[0].coverage='represented_in_concept'
    if mutation == 'source_map_config':m.config_hash='b'*64
    if mutation == 'source_map_content':m.content_hash='b'*64
    with pytest.raises(ValueError):validate_document_concept_plan(after,m)


@pytest.mark.parametrize('mutation',['missing_context','missing_comparator','two_populations','overlapping_owners','false_attestation','anchor_outside','partial_passage'])
def test_invalid_review_does_not_create_an_unsupported_question(mutation):
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+PRIMARY])
    r=primary_review(e,p)
    reviews=[r]
    if mutation == 'missing_context':r.context_region_ids=[]
    if mutation == 'missing_comparator':
        r.question='comparative_outcome'
        r.roles=[x for x in r.roles if x.role != 'comparator']+[BoundRole(role='outcome',evidence=locate(e,'remission'))]
    if mutation == 'two_populations':r.roles.append(BoundRole(role='population',evidence=locate(e,'adults')))
    if mutation == 'overlapping_owners':reviews.append(r.model_copy(deep=True))
    if mutation == 'false_attestation':r.all_qualifications_included=False
    if mutation == 'anchor_outside':r.roles[0].evidence=span(p.canonical_buffer_id,0,len(e['full_text'])+1)
    if mutation == 'partial_passage':r.relationship_evidence=[locate(e,'adults'),locate(e,'primary outcome',start=e['full_text'].index(PRIMARY))]
    with pytest.raises(ValueError):build_document_concept_plan(e,m,reviews=reviews)


def test_different_populations_interventions_and_domains_never_auto_merge():
    e,m,p=inventory(['Domain delta\n\nAdults received treatment alpha; response was assessed at 8 weeks.',
                     'Domain epsilon\n\nChildren received treatment beta; safety was assessed at 4 weeks.'])
    assert all(len(c.owned_region_ids)==1 and not c.context_region_ids and not c.roles for c in p.concepts)
    assert not any(c.relationship_basis == 'human_source_review' for c in p.concepts)
    assert all(r.coverage != 'represented_in_concept' for r in p.regions)


def test_generation_route_provenance_and_actual_validation_unchanged_with_inventory(tmp_path,monkeypatch):
    from test_source_packages import fixture,response
    from src import adaptive_ingestion as adaptive
    from src.chunk_generator import generate_chunks
    from src.chunk_validator import validate_chunks
    from src.source_packages import package_validation_sources
    from src.pipeline import build_generation_diagnostics
    e,m,planned=fixture(tmp_path)
    before=deepcopy(e);source_before=m.model_dump(mode='json')
    assert planned.document_concept_plan and not planned.document_concept_plan.document_complete
    def run(plan):
        provider=Mock();provider.generate.return_value=response(e);audit={}
        result=generate_chunks(e,source_doc=m,generation_plan=plan,provider=provider,diagnostics=audit)
        validation=validate_chunks(result,chunk_sources=package_validation_sources(plan.source_package_plan,m.evidence,audit))
        return result,validation,audit,provider
    with_inventory=run(planned)
    baseline=adaptive._build_ingestion_plan(e,adaptive.AdaptiveConfig(draft_packages=True),source_doc=m)
    without_inventory=run(baseline)
    assert planned.batches == baseline.batches
    assert planned.source_package_plan == baseline.source_package_plan
    assert with_inventory[:2] == without_inventory[:2]
    assert with_inventory[2]['chunk_ownership'] == without_inventory[2]['chunk_ownership']
    assert with_inventory[3].generate.call_args_list == without_inventory[3].generate.call_args_list
    assert set(with_inventory[0]['chunks'][0]) == {'chunk_number','topic','subtopic','section','reference','original_text'}
    assert e == before and m.model_dump(mode='json') == source_before
    diagnostics=build_generation_diagnostics(e,generation_plan=planned)
    assert diagnostics['document_concept_plan'] == planned.document_concept_plan.model_dump(mode='json')
    assert 'Adults with active colitis' not in json.dumps(diagnostics['document_concept_plan'])


def test_legacy_text_route_keeps_optional_inventory_absent():
    from src.adaptive_ingestion import build_ingestion_plan,IngestionPlan
    plan=build_ingestion_plan({'full_text':'Synthetic source statement.'})
    assert plan.document_concept_plan is None
    old=plan.model_dump();old.pop('document_concept_plan')
    assert IngestionPlan.model_validate(old).document_concept_plan is None


def test_review_validation_errors_do_not_echo_confidential_input():
    from pydantic import ValidationError
    with pytest.raises(ValidationError) as exc:
        SourceReview(evidence_fingerprint='CONFIDENTIAL SOURCE PASSAGE',review_id='SECRET',basis='human_source_review')
    assert 'CONFIDENTIAL' not in str(exc.value) and 'SECRET' not in str(exc.value)


@pytest.mark.parametrize('chunk_count',[1,4,8])
def test_literal_generation_cannot_complete_unassessed_document(tmp_path,monkeypatch,chunk_count):
    import logging
    from src import pipeline
    e=source(['Methods\n\nAdults received treatment alpha.'])
    path=tmp_path/'synthetic.pdf';path.write_bytes(b'synthetic PDF boundary')
    monkeypatch.setattr(pipeline,'extract_pdf',lambda _:e)
    monkeypatch.setattr(pipeline,'OUTPUT_DIR',tmp_path/'output')
    def generated(*args,**kwargs):
        unit=kwargs['generation_plan'].batches[0]
        kwargs['diagnostics']['chunk_ownership']=[{'chunk_number':i,'unit_ids':[unit.unit_id]} for i in range(1,chunk_count+1)]
        return {'article':{'source_file':'synthetic.pdf','topic':'Synthetic treatment','subtopic':'Synthetic adults','reference':'Synthetic reference','number_of_chunks':chunk_count},
            'chunks':[{'chunk_number':i,'topic':'Synthetic treatment','subtopic':'Synthetic adults','section':'Synthetic treatment','reference':'Synthetic reference',
                       'original_text':'Adults received treatment alpha.'} for i in range(1,chunk_count+1)]}
    monkeypatch.setattr(pipeline,'generate_chunks',generated)
    monkeypatch.setattr(pipeline,'validate_chunks',lambda *args,**kw:{'status':'PASS',
        'summary':{'chunks':chunk_count,'passed':chunk_count,'warnings':0,'errors':0},'issues':[],
        'chunk_statuses':[{'chunk':i,'status':'PASS'} for i in range(1,chunk_count+1)],'source_evidence':[]})
    move=Mock(side_effect=AssertionError('Unassessed document must retain source'))
    monkeypatch.setattr(pipeline,'move_to_processed',move)
    result=pipeline.process_pdf(path,logger=logging.getLogger('synthetic-concepts'))
    assert result['status']=='partial_success' and not result['source_coverage_complete']
    assert path.exists() and not move.called
    report=json.loads((__import__('pathlib').Path(result['output_dir'])/'validation_report.json').read_text())
    assert report['status']=='PASS'
    assert report['clinical_concept_coverage']['literal_source_coverage_complete']
    assert not report['clinical_concept_coverage']['complete']
    assert not report['human_review']['source_document_complete']
    assert report['document_concept_coverage']['coverage_counts']['not_yet_assessed']==1


def test_limitation_applicability_and_unclassified_regions_remain_assessable():
    e,m,p=inventory(['Limitations\n\nThe trial was underpowered and excluded children.\n\n17 20 unknown material'])
    assert any(c.question=='applicability' and c.status=='unresolved' for c in p.concepts)
    assert region(p,e,'17 20').coverage=='not_yet_assessed'
    assert not p.document_complete


def test_reviewed_planning_accounting_still_does_not_mean_generated_or_approved():
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+PRIMARY])
    after=build_document_concept_plan(e,m,reviews=[primary_review(e,p)])
    assert after.planning_accounted
    assert after.coverage_counts['not_yet_assessed']==0
    assert not after.document_complete and not after.clinically_approved


def test_building_plan_does_not_share_mutable_evidence_or_review_inputs():
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+PRIMARY])
    r=primary_review(e,p);after=build_document_concept_plan(e,m,reviews=[r])
    before=after.model_dump_json();r.roles[0].evidence.char_start=0
    assert after.model_dump_json()==before
    q=next(c for c in after.concepts if c.review_id)
    q.roles[0].evidence.char_start=0
    assert after.reviews[0].roles[0].evidence.char_start != 0
    q.question_anchor.char_start=0
    assert after.reviews[0].question_anchor.char_start != 0


def test_unique_exact_native_duplicate_is_corroboration_and_ambiguous_duplicate_is_held():
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+PRIMARY])
    assert not any(r.basis=='unmapped_native' for r in p.regions)
    assert any(r.literal_duplicate_native_spans for r in p.regions)
    for r in p.regions:
        for duplicate in r.literal_duplicate_native_spans:
            raw=m.evidence.buffers[duplicate.buffer_id].text[duplicate.char_start:duplicate.char_end]
            assert raw in e['full_text'][r.ownership.char_start:r.ownership.char_end]
    # A repeated block cannot be assigned to a canonical occurrence by text alone.
    from src.evidence import add_buffer
    doc=m.evidence;page=doc.pages[0]
    duplicate_text='treatment alpha'
    bid=add_buffer(doc,'raw','ambiguous-native',duplicate_text,1,'fixture')
    page.blocks[0].span=span(bid,0,len(duplicate_text))
    changed=build_document_concept_plan(e,m)
    assert any(r.basis=='unmapped_native' for r in changed.regions)


def test_review_never_clears_unproven_table_or_extraction_uncertainty():
    text=PRIMARY+' Table 1 contains additional results.'
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\nResults\n\n'+text])
    r=primary_review(e,p,primary=text)
    after=build_document_concept_plan(e,m,reviews=[r])
    q=next(c for c in after.concepts if c.review_id)
    assert q.status=='held' and 'structured_relationship_unproven' in q.reasons
    assert after.source_warning_ids
    assert after.coverage_counts['held_for_source_review'] and not after.planning_accounted


def test_warning_ledger_retains_nonblocking_extraction_notices():
    e=source(['Methods\n\nAdults received treatment alpha.'])
    doc=get_evidence(e);warn(doc,'synthetic_notice','Synthetic source notice.',1,False)
    e['evidence']=doc.model_dump(mode='json');m=build_source_map(e)
    p=build_document_concept_plan(e,m)
    assert set(p.source_warning_ids)=={w.warning_id for w in m.evidence.warnings}
    assert p.coverage_counts['not_yet_assessed']==1


def test_shared_held_context_cannot_leave_another_question_source_bound():
    safety='However, adverse events were assessed only during the first 2 weeks.'
    # Shared methods have an adjacent qualification: one review includes it,
    # another omits it. A held shared context cannot promote either question.
    e,m,p=inventory(['Methods\n\n'+METHODS+'\n\n'+safety+'\n\nResults\n\n'+PRIMARY+'\n\n'+SECONDARY])
    first=primary_review(e,p)
    first.context_region_ids.append(region(p,e,safety).region_id)
    first.relationship_evidence.append(locate(e,safety))
    second=primary_review(e,p)
    second.question='secondary_outcome';a=e['full_text'].index(SECONDARY)
    second.question_anchor=locate(e,'secondary outcome',start=a)
    second.owned_region_ids=[region(p,e,SECONDARY).region_id]
    second.roles=[x for x in second.roles if x.role not in {'primary_outcome','time_point'}]+[
        BoundRole(role='secondary_outcome',evidence=locate(e,'secondary outcome',start=a)),
        BoundRole(role='time_point',evidence=locate(e,'4 weeks',start=a))]
    second.relationship_evidence=[locate(e,METHODS),locate(e,SECONDARY)]
    after=build_document_concept_plan(e,m,reviews=[first,second])
    reviewed=[q for q in after.concepts if q.review_id]
    assert len(reviewed)==2 and all(q.status=='held' for q in reviewed)

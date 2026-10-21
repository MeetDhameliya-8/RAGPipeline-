"""P1-B1 source ownership and conservative concept planning, without providers."""
import importlib
import json

import pytest

from src.evidence import build_evidence_document, get_evidence, span, warn, trace_to_raw
from src.source_map import build_source_map


def api():
    return importlib.import_module("src.concept_planner")


def source(pages):
    records = [{"page_number": i, "text": text, "raw_text": text, "extractor": "fixture"} for i, text in enumerate(pages, 1)]
    e = {"file_name": "adversarial.pdf", "page_count": len(records), "pages": records,
         "full_text": "\n\n".join(pages), "raw_full_text": "\n\n".join(pages)}
    candidates = [dict(p, layout={"width": 600, "height": 800, "blocks": [
        {"text": p["text"], "bbox": [40,40,560,700], "native_order":0}]}) for p in records]
    e["evidence"] = build_evidence_document(e, pdf_sha256="a"*64, candidates=candidates).model_dump(mode="json")
    return e


def plan(e, **kwargs):
    return api().build_clinical_plan(e, build_source_map(e), **kwargs)


def test_source_partition_and_stable_named_ownership():
    e = source(["Methods\n\nAdults received treatment A for 8 weeks.\n\nResults\n\nGroup A had a response at 8 weeks."])
    a, b = plan(e), plan(e)
    assert a.model_dump_json() == b.model_dump_json()
    owned = sorted((s.char_start, s.char_end) for c in a.concepts for s in c.ownership)
    assert owned[0][0] == 0 and owned[-1][1] == len(e["full_text"])
    assert all(x[1] == y[0] for x,y in zip(owned, owned[1:]))
    assert all(s.buffer_id == a.buffer_id for c in a.concepts for s in c.ownership)
    assert all(c.section_ids and c.page_numbers for c in a.concepts)
    assert len({c.concept_id for c in a.concepts}) == len(a.concepts)


def test_cross_section_context_and_literal_mentions_are_not_invented_associations():
    e = source(["Methods\n\nAdults with active disease received treatment A. The primary endpoint was response.\n\nResults\n\nGroup A had a response after 8 weeks."])
    p = plan(e)
    result = next(c for c in p.concepts if any(m.kind == "timeframe" for m in c.mentions))
    unit = next(u for u in p.units if result.concept_id in u.concept_ids)
    assert unit.context_ids
    assert all(m.relationship == "source_literal_only" for c in p.concepts for m in c.mentions)
    assert any(m.kind == "endpoint" for c in p.concepts for m in c.mentions)
    assert all(c.coverage_status != "completed" for c in p.concepts)


def test_table_notes_and_incomplete_figure_relationships_remain_blocked():
    p = plan(source(["Results\n\nPatients had a response as shown in Table 1 and Figure 2.\n\nTable 1. Response\n\n* Response excludes missing observations."]))
    concept = next(c for c in p.concepts if any(d.kind == "figure" for d in c.dependencies))
    assert any(d.kind == "table" and d.supporting_ids for d in concept.dependencies)
    assert any(d.kind == "figure" and d.status == "missing" for d in concept.dependencies)
    assert concept.generation_eligibility == "blocked"
    assert any(c.role == "table_note" for c in p.concepts)


def test_local_critical_warning_does_not_poison_unrelated_page_but_blocks_affected_unit():
    e = source(["First condition\n\nAdults received treatment A with response at 8 weeks.",
                "Second condition\n\nChildren received treatment B with response at 4 weeks."])
    doc = get_evidence(e)
    start = e["full_text"].index("Adults")
    warn(doc, "clinical_gap", "Result requires review.", 1, True, span(doc.canonical_buffer_id,start,start+6))
    e["evidence"] = doc.model_dump(mode="json")
    p = plan(e)
    assert any(u.dependency_decision == "confirmed_affected" and u.generation_eligibility == "blocked" for u in p.units)
    assert any(u.dependency_decision == "independently_supported" and u.generation_eligibility == "eligible_draft" for u in p.units)
    assert not p.document_complete


def test_unknown_document_warning_and_anaphora_remain_unresolved():
    e = source(["Results\n\nThese patients had a response as described above."])
    doc = get_evidence(e)
    warn(doc, "unknown_material_gap", "Unlocalized missing clinical source.", critical=True)
    e["evidence"] = doc.model_dump(mode="json")
    p = plan(e)
    assert not any(u.generation_eligibility == "eligible_draft" for u in p.units)
    assert any(d.kind == "referent" and d.status == "unresolved" for c in p.concepts for d in c.dependencies)
    assert any(u.dependency_decision == "uncertain_dependency" for u in p.units)


def test_deduplication_never_overlaps_source_ownership_or_confuses_contexts():
    e = source(["Population A\n\nAdults had a response.\n\nPopulation B\n\nAdults had a response."])
    p = plan(e)
    assert len({c.concept_id for c in p.concepts}) == len(p.concepts)
    assert len({(s.char_start,s.char_end) for c in p.concepts for s in c.ownership}) == sum(len(c.ownership) for c in p.concepts)
    assert p.source_accounting["unassigned_characters"] == 0


def test_context_only_output_cannot_complete_owned_concepts():
    e = source(["Methods\n\nAdults received treatment A.\n\nResults\n\nGroup A had a response after 8 weeks."])
    p = plan(e)
    unit = next(u for u in p.units if u.context_ids)
    context = next(c for c in p.concepts if c.concept_id in unit.context_ids)
    text = ''.join(e['full_text'][s.char_start:s.char_end] for s in context.ownership).strip()
    report = {"chunk_statuses":[{"chunk":1,"status":"PASS"}]}
    coverage = api().reconcile_concept_coverage(p, get_evidence(e), {"chunks":[{"chunk_number":1,"original_text":text}]}, report,
        {"chunk_ownership":[{"chunk_number":1,"unit_ids":[unit.unit_id]}]})
    assert not coverage["complete"]
    assert not any(r["status"] == "source_supported" for r in coverage["concepts"] if r["concept_id"] in unit.concept_ids)


def test_materialization_budget_and_context_labels_and_manifest_contains_no_text_copy():
    e = source(["Methods\n\nAdults received treatment A.\n\nResults\n\nGroup A had a response at 8 weeks."])
    p = plan(e, max_unit_chars=1000)
    unit = next(u for u in p.units if u.generation_eligibility == "eligible_draft")
    text = api().materialize_unit(p, unit, get_evidence(e))
    assert "OWNED SOURCE" in text and "CONTEXT ONLY" in text
    assert len(text) <= 1000
    assert '"text":' not in p.model_dump_json()


def test_storage_reads_old_and_new_optional_plan_without_losing_evidence(tmp_path):
    from src.evidence_storage import save_source_map, load_source_map
    e = source(["Methods\n\nAdults received treatment A."])
    mapped = build_source_map(e)
    assert mapped.clinical_plan is None
    mapped.clinical_plan = api().build_clinical_plan(e, mapped)
    path = tmp_path/'source_map.json'
    save_source_map(path,mapped)
    loaded = load_source_map(path)
    assert loaded.clinical_plan == mapped.clinical_plan
    assert loaded.evidence == mapped.evidence


def test_evidence_index_matches_original_exact_and_region_traces():
    e = source(["Methods\n\nAdults received treatment A.","Results\n\nPatients had a response."])
    doc = get_evidence(e)
    index = api().EvidenceIndex(doc)
    for start,end in [(0,10),(10,len(e['full_text'])),(0,len(e['full_text']))]:
        owned=span(doc.canonical_buffer_id,start,end)
        assert index.trace(owned) == trace_to_raw(doc,owned)


def test_unresolved_context_cannot_promote_a_result_and_unknown_scope_is_blocked():
    p = plan(source(["Methods\n\nThese patients in group A received treatment A.\n\nResults\n\nGroup A had a response after 8 weeks."]))
    result = next(c for c in p.concepts if any(m.kind=="timeframe" for m in c.mentions))
    assert result.generation_eligibility == "blocked"
    assert "unresolved_supporting_context" in result.block_reasons
    isolated = plan(source(["Results\n\nThe response rate was high."]))
    assert not any(u.generation_eligibility=="eligible_draft" for u in isolated.units)


def test_abbreviation_does_not_cut_treatment_qualification():
    e=source(["Methods\n\nPatients received drug X vs. Drug Y for 8 weeks."])
    p=plan(e)
    clinical=[c for c in p.concepts if c.role=="clinical_proposition"]
    assert len(clinical)==1
    assert any(m.kind=="timeframe" for m in clinical[0].mentions)


def test_ingestion_uses_source_plan_and_materializes_only_eligible_units_with_five_fields():
    from unittest.mock import Mock
    from src.adaptive_ingestion import build_ingestion_plan
    from src.chunk_generator import generate_chunks
    e=source(["Methods\n\nAdults received treatment A for 8 weeks.","Second condition\n\nChildren received treatment B for 4 weeks."])
    # A located source warning blocks one domain only.
    doc=get_evidence(e); a=e['full_text'].index('Adults')
    warn(doc,"clinical_gap","Review this dose.",1,True,span(doc.canonical_buffer_id,a,a+6));e['evidence']=doc.model_dump(mode='json')
    # A real source identity is supplied independently from generated metadata.
    e['reference_source_text']='doi:10.1234/source123'
    e['reference_metadata']={'reference':'doi:10.1234/source123','doi':'10.1234/source123'}
    mapped=build_source_map(e)
    ingestion=build_ingestion_plan(e, source_doc=mapped)
    assert ingestion.clinical_plan is mapped.clinical_plan
    assert ingestion.batches and all(not b.text for b in ingestion.batches)
    provider=Mock()
    provider.generate.return_value=json.dumps({'article':{'source_file':'adversarial.pdf','topic':'Treatment B','subtopic':'Children','reference':'doi:10.1234/source123','number_of_chunks':1},
        'chunks':[{'topic':'Treatment B','subtopic':'Children','section':'Source response','reference':'doi:10.1234/source123','original_text':'Children received treatment B for 4 weeks.'}]})
    diagnostics={}
    result=generate_chunks(e,provider=provider,source_doc=mapped,generation_plan=ingestion,diagnostics=diagnostics)
    assert provider.generate.call_count==len(ingestion.batches)
    assert all('Adults received' not in c.args[0] for c in provider.generate.call_args_list)
    assert all('OWNED SOURCE' in c.args[0] for c in provider.generate.call_args_list)
    assert set(result['chunks'][0])=={'chunk_number','topic','subtopic','section','reference','original_text'}
    assert diagnostics['chunk_ownership']


def test_one_passing_chunk_cannot_complete_a_whole_section():
    from src.source_map import mark_processed_sections_from_validation,initial_processing_state
    e=source(["Methods\n\nAdults received treatment alpha. Patients had a response after 8 weeks."])
    mapped=build_source_map(e);p=api().build_clinical_plan(e,mapped)
    unit=next(u for u in p.units if u.generation_eligibility=='eligible_draft')
    cid=unit.concept_ids[0];c=next(c for c in p.concepts if c.concept_id==cid);text=e['full_text'][c.ownership[0].char_start:c.ownership[0].char_end].strip()
    report={'chunk_statuses':[{'chunk':1,'status':'PASS'}],'source_evidence':[{'chunk':1,'source_section_ids':[mapped.sections[0].section_id]}]}
    coverage=api().reconcile_concept_coverage(p,mapped.evidence,{'chunks':[{'chunk_number':1,'original_text':text}]},report,
        {'chunk_ownership':[{'chunk_number':1,'unit_ids':[unit.unit_id]}]})
    assert not coverage['complete']
    state=mark_processed_sections_from_validation(mapped,initial_processing_state(mapped),report,concept_coverage=coverage)
    assert all(r.status!='processed' for r in state.sections)


def test_short_numeric_or_unknown_material_is_not_hidden_as_a_completed_heading():
    p=plan(source(["Results\n\n17 vs 4\n\nUnknown source fragment\n\nAdults received treatment alpha."]))
    unknown=[c for c in p.concepts if c.role=='unclassified_source']
    assert len(unknown)>=2
    assert all(c.generation_eligibility=='blocked' and c.coverage_status=='requires_adjudication' for c in unknown)


def test_group_definition_and_population_context_are_not_assumed_interchangeable():
    p=plan(source(["Methods\n\nAdults in group A received treatment alpha.\n\nResults\n\nGroup B had a response after 8 weeks."]))
    result=next(c for c in p.concepts if any(m.kind=='timeframe' for m in c.mentions))
    assert result.generation_eligibility=='blocked'
    assert 'group_definition_unresolved' in result.block_reasons
    p=plan(source(["Methods\n\nAdults received treatment alpha.\n\nResults\n\nChildren had a response after 8 weeks."]))
    result=next(c for c in p.concepts if any(m.kind=='timeframe' for m in c.mentions))
    assert result.generation_eligibility=='blocked'
    assert 'population_context_ambiguous' in result.block_reasons


def test_explicit_group_definition_resolves_to_exact_source_context():
    e=source(["Methods\n\nAdults in group A received treatment alpha.\n\nResults\n\nGroup A had a response after 8 weeks."])
    p=plan(e)
    result=next(c for c in p.concepts if any(m.kind=='timeframe' for m in c.mentions))
    definition=next(c for c in p.concepts if any(m.kind=='intervention' for m in c.mentions))
    assert any(d.kind=='referent' and d.status=='available_context' and d.supporting_ids==[definition.concept_id] for d in result.dependencies)
    assert result.generation_eligibility=='eligible_draft'


def test_invalid_owned_span_duplicate_id_or_promoted_blocked_unit_fails_closed():
    e=source(["Methods\n\nAdults received treatment alpha.\n\nResults\n\nThese patients had a response."])
    mapped=build_source_map(e);p=api().build_clinical_plan(e,mapped)
    api().validate_clinical_plan(p,mapped.evidence)
    bad=p.model_copy(deep=True);bad.concepts[0].ownership[0].char_end=999999
    with pytest.raises(ValueError):api().validate_clinical_plan(bad,mapped.evidence)
    bad=p.model_copy(deep=True);bad.concepts[1].qualifications=[span(bad.buffer_id,0,999999)]
    with pytest.raises(ValueError):api().validate_clinical_plan(bad,mapped.evidence)
    bad=p.model_copy(deep=True);bad.concepts[1].concept_id=bad.concepts[0].concept_id
    with pytest.raises(ValueError):api().validate_clinical_plan(bad,mapped.evidence)
    bad=p.model_copy(deep=True);u=next(u for u in bad.units if u.generation_eligibility=='blocked');u.generation_eligibility='eligible_draft'
    with pytest.raises(ValueError):api().validate_clinical_plan(bad,mapped.evidence)


def test_identical_supported_propositions_same_context_are_planned_once_with_all_occurrences():
    e=source(["Methods\n\nAdults received treatment alpha.\n\nAdults received treatment alpha."])
    p=plan(e)
    clinical=[c for c in p.concepts if c.role=='clinical_proposition']
    assert len(clinical)==1 and len(clinical[0].ownership)==2
    assert p.deduplicated_occurrences==1
    assert p.source_accounting['owned_characters']==len(e['full_text'])


def test_budgets_block_without_discarding_owned_source_or_required_context():
    e=source(["Methods\n\n"+" ".join(f"Adults received treatment number {n} for 8 weeks." for n in range(30))])
    p=plan(e,max_unit_chars=300,max_units=1)
    assert p.blockers or all(u.generation_eligibility=='blocked' for u in p.units)
    assert p.source_accounting['owned_characters']==len(e['full_text'])
    assert p.source_accounting['unassigned_characters']==0


def test_real_pdf_visual_page_does_not_authorize_or_poison_an_unrelated_source_page(tmp_path):
    import fitz
    from hashlib import sha256
    from src.pdf_extractor import extract_pdf
    path=tmp_path/'adversarial-layout.pdf'
    with fitz.open() as pdf:
        first=pdf.new_page()
        first.insert_text((40,40),'Methods')
        first.insert_text((40,90),'Adults received treatment alpha. Figure 1 shows response.')
        first.draw_rect(fitz.Rect(40,160,260,300))
        second=pdf.new_page()
        second.insert_text((40,40),'Independent evidence')
        second.insert_text((40,90),'Children received treatment beta for 4 weeks.')
        pdf.save(path)
    original=sha256(path.read_bytes()).hexdigest()
    e=extract_pdf(path);p=plan(e)
    assert any(u.generation_eligibility=='eligible_draft' for u in p.units)
    assert any(u.generation_eligibility=='blocked' for u in p.units)
    assert not p.document_complete
    assert sha256(path.read_bytes()).hexdigest()==original


def test_incomplete_planned_document_returns_partial_draft_and_keeps_source_pdf(tmp_path,monkeypatch):
    import logging
    from unittest.mock import Mock
    from src import pipeline
    e=source(["Methods\n\nAdults received treatment alpha.\n\nUnknown numeric material 17 vs 4"])
    path=tmp_path/'input.pdf';path.write_bytes(b'fake PDF boundary fixture')
    monkeypatch.setattr(pipeline,'OUTPUT_DIR',tmp_path/'output')
    monkeypatch.setattr(pipeline,'extract_pdf',lambda _:e)
    generated={'article':{'source_file':'input.pdf','topic':'Treatment alpha','subtopic':'Adults','reference':'Fixture reference','number_of_chunks':1},
        'chunks':[{'chunk_number':1,'topic':'Treatment alpha','subtopic':'Adults','section':'Source treatment','reference':'Fixture reference','original_text':'Adults received treatment alpha.'}]}
    monkeypatch.setattr(pipeline,'generate_chunks',lambda *args,**kwargs:generated)
    monkeypatch.setattr(pipeline,'validate_chunks',lambda *args,**kwargs:{'status':'PASS','summary':{'chunks':1,'passed':1,'warnings':0,'errors':0},
        'issues':[],'chunk_statuses':[{'chunk':1,'status':'PASS'}],'source_evidence':[]})
    move=Mock(side_effect=AssertionError('Incomplete source must not be moved'))
    monkeypatch.setattr(pipeline,'move_to_processed',move)
    result=pipeline.process_pdf(path,logger=logging.getLogger('test-source-plan'))
    assert result['status']=='partial_success' and result['reason']=='incomplete_clinical_concept_coverage'
    assert path.exists() and not move.called


def test_reconciliation_requires_real_owned_clinical_work_and_can_credit_exact_validated_source():
    e=source(["Methods\n\nResults"]);p=plan(e)
    assert not api().reconcile_concept_coverage(p,get_evidence(e),{'chunks':[]},{'chunk_statuses':[]},{})['complete']
    e=source(["Methods\n\nAdults received treatment alpha."]);p=plan(e)
    unit=next(u for u in p.units if u.generation_eligibility=='eligible_draft')
    generated={'chunks':[{'chunk_number':1,'original_text':'Adults received treatment alpha.'}]}
    report={'chunk_statuses':[{'chunk':1,'status':'PASS'}]}
    coverage=api().reconcile_concept_coverage(p,get_evidence(e),generated,report,{'chunk_ownership':[{'chunk_number':1,'unit_ids':[unit.unit_id]}]})
    assert coverage['complete'] and coverage['clinically_approved_concepts']==0


def test_owned_result_without_required_population_and_intervention_context_is_not_completed():
    e=source(["Methods\n\nAdults in group A received treatment alpha.\n\nResults\n\nGroup A had a response after 8 weeks."])
    p=plan(e)
    result=next(c for c in p.concepts if any(m.kind=='timeframe' for m in c.mentions))
    unit=next(u for u in p.units if result.concept_id in u.concept_ids)
    generated={'chunks':[{'chunk_number':1,'original_text':'Group A had a response after 8 weeks.'}]}
    coverage=api().reconcile_concept_coverage(p,get_evidence(e),generated,{'chunk_statuses':[{'chunk':1,'status':'PASS'}]},
        {'chunk_ownership':[{'chunk_number':1,'unit_ids':[unit.unit_id]}]})
    assert next(r for r in coverage['concepts'] if r['concept_id']==result.concept_id)['status']!='source_supported'


@pytest.mark.parametrize('status',['PASS','FAILED'])
def test_legacy_outputs_are_not_reused_as_concept_complete_or_overwritten(tmp_path,status):
    from src.pipeline import find_reusable_output_for_source,select_output_dir_for_processing
    from src.evidence_storage import save_source_map
    e=source(["Methods\n\nAdults received treatment alpha."])
    mapped=build_source_map(e)
    old=tmp_path/'old-review';old.mkdir()
    save_source_map(old/'source_map.json',mapped)
    (old/'chunks.json').write_text('{"chunks":[]}')
    (old/'validation_report.json').write_text(json.dumps({'status':status}))
    original=(old/'source_map.json').read_bytes()
    mapped.clinical_plan=api().build_clinical_plan(e,mapped)
    assert find_reusable_output_for_source(old,mapped) is None
    assert select_output_dir_for_processing(old,mapped,force=False)!=old
    assert (old/'source_map.json').read_bytes()==original


def test_real_evidence_generator_default_cannot_bypass_concept_eligibility():
    from unittest.mock import Mock
    from src.chunk_generator import generate_chunks,ChunkGenerationError
    e=source(["Results\n\nPatients had a response as shown in Figure 2."])
    provider=Mock();provider.generate.side_effect=AssertionError('Blocked evidence must not reach a provider')
    with pytest.raises(ChunkGenerationError,match='eligible draft units'):
        generate_chunks(e,provider=provider)
    provider.generate.assert_not_called()

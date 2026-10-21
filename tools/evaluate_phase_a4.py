"""Offline A4 compatibility, physical hold diagnosis and synthetic measurements."""
from __future__ import annotations

from collections import Counter
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import runpy
from statistics import median
import sys
from time import perf_counter
import tracemalloc
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT),str(ROOT/'tests')]
from clinical_proposals import propose_concepts
from coverage_reconciliation import reconcile_coverage,capture_generation_context,write_report
from editorial_integration_support import BODY,REFERENCE,FakeProvider,prepared,execute
from proposal_support import CASES,fixture
from proposal_editorial.requests import generation_contract,validation_policy
from src.package_generation import validation_fingerprint


def normalize(r):
    return {'status':r.status,'plan':r.proposal_plan_id,'evidence':r.evidence_fingerprint,
        'eligibility':[e.model_dump(mode='json') for e in r.eligibility],
        'drafts':[d.model_dump(mode='json') for d in r.drafts],
        'units':[{k:getattr(u,k) for k in ('proposal_id','source_order','status','code','candidate_key',
            'candidate_sha256','resumed','validation','storage')} for u in r.units]}


def compatibility():
    baseline=ROOT/'tmp/a4/base'
    manifest=json.loads((baseline/'manifest.json').read_text(encoding='utf-8'))
    changed=[n for n,h in manifest.items() if sha256((ROOT/n).read_bytes()).hexdigest()!=h]
    assert not changed,changed
    dependencies=json.loads((baseline/'versions.json').read_text(encoding='utf-8'))
    assert all(version(n)==v for n,v in dependencies.items())
    a1=runpy.run_path(str(ROOT/'tools/evaluate_phase_a1.py'))['compare']();a1.pop('elapsed_seconds',None)
    assert a1==json.loads((baseline/'a1.json').read_text(encoding='utf-8'))
    a2={}
    for name,pages in CASES.items():
        _,source,inventory=fixture(pages)
        a2[name]=propose_concepts(source,inventory).model_dump(mode='json')
    assert a2==json.loads((baseline/'a2.json').read_text(encoding='utf-8'))
    fingerprints={'legacy_validation':validation_fingerprint(),'a3_generation':generation_contract(),
        'a3_validation':validation_policy()}
    assert fingerprints==json.loads((baseline/'fingerprints.json').read_text(encoding='utf-8'))
    old=json.loads((baseline/'a3.json').read_text(encoding='utf-8'))
    cases={}
    root=ROOT/'tmp/a4/p'/uuid4().hex[:6]
    for name,paragraphs in [('one',[BODY]),('several',[BODY,BODY.replace('alpha','beta')]),
        ('duplicate',[BODY,BODY]),('held',CASES['two_arm_trial'])]:
        inputs=prepared(paragraphs);provider=FakeProvider()
        result=execute(root/name,inputs=inputs,provider=provider)
        resumed=FakeProvider();cached=execute(root/name,inputs=inputs,provider=resumed)
        actual={'fresh':normalize(result),'resume':normalize(cached),'fresh_calls':len(provider.calls),'resumed_calls':len(resumed.calls)}
        assert actual==old[name],name
        report=reconcile_coverage(inputs[1],inputs[2],propose_concepts(inputs[1],inputs[2]),cached,
            extraction=inputs[0],workspace=root/name/'editorial',db_path=root/name/'review.sqlite3',
            generation=capture_generation_context(resumed))
        assert report.technical_reconciliation_complete,report.verification_issues
        write_report(report,root/name/'coverage')
        cases[name]={'reconciliation_id':report.reconciliation_id,'counts':report.counts,
            'technical_reconciliation_complete':report.technical_reconciliation_complete,
            'clinically_complete':report.clinically_complete,'review_tasks':report.review_tasks_by_reason}
    return {'protected_files':len(manifest),'modified_files':changed,'dependencies_unchanged':True,
        'a1_comparison_identical':True,'a1_summary':a1['evaluation']['summary'],'a2_identical_cases':len(a2),
        'a3_identical_cases':len(cases),'a3_resumed_model_calls':0,'fingerprints':fingerprints,'examples':cases}


def physical_hold():
    import fitz
    from src.pdf_extractor import extract_pdf
    from src.native_layout import recover_native_layout
    from src.source_map import build_source_map
    from src.document_concepts import build_document_concept_plan
    from src.evidence import get_evidence
    from src.generation_preflight import require_source_reference
    root=ROOT/'tmp/a4/ph'/uuid4().hex[:6];root.mkdir(parents=True)
    path=root/'synthetic.pdf'
    with fitz.open() as document:
        p=document.new_page();p.insert_text((40,45),REFERENCE,fontsize=9);p.insert_text((40,80),'Methods')
        assert p.insert_textbox(fitz.Rect(40,110,550,700),BODY,fontsize=10)>0
        document.save(path)
    stages=[]
    def capture(stage,doc):
        canon=doc.buffers[doc.canonical_buffer_id].text
        stages.append({'stage':stage,'warning_counts':dict(Counter(w.code for w in doc.warnings)),
            'canonical_characters':len(canon),'canonical_sha256':sha256(canon.encode()).hexdigest(),
            'citation_in_canonical':REFERENCE in canon,
            'body_retained': ' '.join(BODY.split()) in ' '.join(canon.split())})
    extraction=extract_pdf(path);capture('extract_pdf',get_evidence(extraction))
    extraction=recover_native_layout(extraction);capture('recover_native_layout',get_evidence(extraction))
    source=build_source_map(extraction);capture('build_source_map',source.evidence)
    inventory=build_document_concept_plan(extraction,source)
    code='text_transformation_unreviewed'
    first=next(s['stage'] for s in stages if s['warning_counts'].get(code))
    doc=source.evidence
    unmapped=[s for r in inventory.regions if r.basis=='unmapped_native' for s in r.trace.fragments]
    native_values=[doc.buffers[s.source.buffer_id].text[s.source.char_start:s.source.char_end] for s in unmapped]
    transformations=[]
    for target,mappings in doc.mappings.items():
        for m in mappings:
            if m.relation not in {'exact','inserted'}:
                original=doc.buffers[m.source.buffer_id].text[m.source.char_start:m.source.char_end] if m.source else ''
                if original.strip() and ('doi:' in original.lower() or REFERENCE in original):
                    transformations.append({'relation':m.relation,'reason':m.reason,'source_characters':len(original),
                        'target_characters':m.target.char_end-m.target.char_start,
                        'source_value_sha256':sha256(original.encode()).hexdigest()})
    provider=FakeProvider()
    result=execute(root,inputs=(extraction,source,inventory),provider=provider)
    report=reconcile_coverage(source,inventory,propose_concepts(source,inventory),result,
        extraction=extraction,workspace=root/'editorial',db_path=root/'review.sqlite3',
        generation=capture_generation_context(provider))
    write_report(report,root/'coverage')
    assert not provider.calls and not result.drafts
    assert all(s['body_retained'] for s in stages)
    return {'introducing_stage':first,'stage_snapshots':stages,'citation_preflight_verified':require_source_reference(extraction)==REFERENCE,
        'transformations':transformations,'unmapped_native_regions':len(native_values),
        'unmapped_native_characters_per_representation':[len(v) for v in native_values],
        'unmapped_native_only_citation':all(v.strip()==REFERENCE for v in native_values),
        'original_citation_retained_in_raw':any(REFERENCE in b.text for b in doc.buffers.values() if b.stage=='raw'),
        'region_counts':dict(Counter(r.basis for r in inventory.regions)),
        'source_states':dict(Counter(r.source_state for r in report.regions)),
        'canonical_character_accounting':report.source_characters_by_state,'counts':report.counts,
        'model_calls':len(provider.calls),'clinically_complete':report.clinically_complete,
        'technical_reconciliation_complete':report.technical_reconciliation_complete}


def benchmark():
    root=ROOT/'tmp/a4/m'/uuid4().hex[:6]
    results=[]
    for n in (4,16,48):
        # Alphabetic entity IDs avoid unproven numeric protocol associations.
        inputs=prepared([BODY.replace('alpha','agent'+chr(97+i//26)+chr(97+i%26)) for i in range(n)])
        provider=FakeProvider();run=execute(root/str(n),inputs=inputs,provider=provider)
        plan=propose_concepts(inputs[1],inputs[2]);context=capture_generation_context(provider)
        def measure():
            return reconcile_coverage(inputs[1],inputs[2],plan,run,extraction=inputs[0],
                workspace=root/str(n)/'editorial',db_path=root/str(n)/'review.sqlite3',generation=context)
        samples=[]
        for _ in range(3):
            start=perf_counter();report=measure();samples.append(perf_counter()-start)
            assert report.technical_reconciliation_complete
            assert report.counts['verified_stored_review_drafts']==n
        tracemalloc.start();start=perf_counter();memory_report=measure();memory_seconds=perf_counter()-start
        _,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
        assert memory_report==report
        results.append({'proposals':n,'source_regions':len(inputs[2].regions),'source_characters':len(inputs[0]['full_text']),
            'repetitions':3,'elapsed_seconds':samples,'median_seconds':median(samples),
            'traced_peak_bytes':peak,'tracemalloc_run_seconds':memory_seconds,
            'verified_rows':n,'reconciliation_model_calls':0,'deterministic_reports':True})
    return results


def main():
    # Defense in depth for this standalone evaluator outside pytest guards.
    import socket
    def forbidden(*_,**__):
        raise RuntimeError('Network prohibited in offline A4 evaluation')
    socket.socket.connect=forbidden
    data={'version':'phase-a4-offline-evaluation-v1','live_calls':0,'clinical_reviewed':False,
        'compatibility':compatibility(),'physical_pdf':physical_hold(),'benchmarks':benchmark()}
    path=ROOT/'tmp/a4/evaluation.json';path.write_text(json.dumps(data,indent=2)+'\n',encoding='utf-8')
    print(json.dumps({'compatibility':{k:v for k,v in data['compatibility'].items() if k not in {'examples','a1_summary'}},
        'physical_pdf':data['physical_pdf'],'benchmarks':data['benchmarks']}))


if __name__=='__main__':
    main()

"""Repeatable offline A3 group/rolling comparison and protected baseline checks.

Explicit fake providers, fictional prepared sources and dedicated tmp/phase_a3
workspaces only. This is not a fresh-upload or Azure performance benchmark.
"""
from __future__ import annotations

import argparse
from hashlib import sha256
from importlib.metadata import version
import json
from pathlib import Path
import runpy
from statistics import median
import sys
from time import perf_counter,sleep
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT),str(ROOT/'tests')]
from editorial_integration_support import BODY,FakeProvider,prepared,response
from proposal_support import CASES,fixture
from clinical_proposals import propose_concepts
from src.bounded_scheduler import run_bounded,ScheduleRun,Outcome
from src.package_generation import validation_fingerprint
from proposal_editorial import run_proposal_editorial
import proposal_editorial.workflow as workflow


def fixed_groups(worker,total,concurrency,control,progress=None):
    """Benchmark-only reference: same two-worker implementation plus group barriers."""
    outcomes = []
    maximum = 0
    interruption = None
    for start in range(0,total,concurrency):
        run = run_bounded(lambda i:worker(start+i),min(concurrency,total-start),concurrency,control)
        maximum = max(maximum,run.max_in_flight)
        outcomes.extend(Outcome(index=start+o.index,value=o.value,error=o.error) for o in run.outcomes)
        if run.interruption or control.stopped.is_set():
            interruption = run.interruption
            break
    return ScheduleRun(tuple(outcomes),interruption,maximum)


def protected_comparison():
    baseline = ROOT/'tmp/phase_a3/baseline'
    manifest = json.loads((baseline/'sha256-manifest.json').read_text(encoding='utf-8'))
    changed = [name for name,expected in manifest.items() if sha256((ROOT/name).read_bytes()).hexdigest()!=expected]
    assert not changed,changed
    dependencies = json.loads((baseline/'versions.json').read_text(encoding='utf-8'))
    assert all(version(name)==expected for name,expected in dependencies.items())
    proposals = {name:propose_concepts(fixture(pages)[1],fixture(pages)[2]).model_dump(mode='json')
                 for name,pages in CASES.items()}
    assert proposals == json.loads((baseline/'a2-proposals.json').read_text(encoding='utf-8'))
    a1 = runpy.run_path(str(ROOT/'tools/evaluate_phase_a1.py'))['compare']()
    old = json.loads((baseline/'a1-comparison.json').read_text(encoding='utf-8'))
    for item in (a1,old):
        item.pop('elapsed_seconds',None)
    assert a1 == old,'A1 changed'
    assert validation_fingerprint() == (baseline/'validation-fingerprint.txt').read_text().strip()
    return {'protected_files':len(manifest),'modified_protected_files':changed,
        'dependencies_unchanged':True,'a2_cases_identical':len(proposals),
        'a1_comparison_identical':True,'a1_summary':a1['evaluation']['summary'],
        'validation_fingerprint':validation_fingerprint()}


def benchmark():
    root = ROOT/'tmp/phase_a3/b'/uuid4().hex[:8]
    inputs = prepared([BODY.replace('alpha',name) for name in ('alpha','beta','gamma','omega')])
    rows = []
    documents = None
    for name,scheduler in (('fixed_group_barrier',fixed_groups),('rolling',run_bounded)):
        timings = []
        resumed_times = []
        maximum = []
        for n in range(3):
            path = root/('g' if name == 'fixed_group_barrier' else 'r')/str(n)
            def action(request,*_):
                order = json.loads(request.split('\n\n')[0])['source_order']
                sleep(.18 if order%2 == 0 else .025)
                return response(request)
            provider = FakeProvider(action)
            workflow.run_bounded = scheduler
            started = perf_counter()
            result = run_proposal_editorial(*inputs,provider=provider,workspace=path/'editorial',
                db_path=path/'review.sqlite3',diagnostics={})
            timings.append(perf_counter()-started)
            assert len(provider.calls)==4 and len(result.drafts)==4
            assert all(u.storage['readback_verified'] for u in result.units)
            maximum.append(provider.high_water)
            current = [d.document for d in result.drafts]
            if documents is None:
                documents = current
            assert current == documents,'Ordered clinical outputs changed'
            resumed = FakeProvider(lambda *_: (_ for _ in ()).throw(AssertionError('Cache made a call')))
            started = perf_counter()
            cached = run_proposal_editorial(*inputs,provider=resumed,workspace=path/'editorial',
                db_path=path/'review.sqlite3',diagnostics={})
            resumed_times.append(perf_counter()-started)
            assert not resumed.calls and [d.document for d in cached.drafts] == documents
        rows.append({'scheduler':name,'repetitions':3,'fresh_workspace_seconds':timings,
            'fresh_median_seconds':median(timings),'cached_median_seconds':median(resumed_times),
            'max_in_flight':maximum,'ordered_outputs_identical':True,'logical_calls_each_fresh':4,
            'logical_calls_each_resumed':0,'cached_rows_unchanged':True})
    workflow.run_bounded = run_bounded
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='tmp/phase_a3/evaluation.json')
    output = (ROOT/parser.parse_args().output).resolve()
    if not output.is_relative_to((ROOT/'tmp/phase_a3').resolve()):
        raise SystemExit('Output must stay below tmp/phase_a3.')
    result = {'version':'phase-a3-offline-evaluation-v1','live_calls':0,'clinician_reviewed':False,
        'compatibility':protected_comparison(),'benchmarks':benchmark()}
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(result))


if __name__ == '__main__':
    main()

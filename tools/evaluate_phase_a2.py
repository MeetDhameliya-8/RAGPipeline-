"""Offline proposals and isolated synthetic microbenchmarks. No provider/PDF I/O."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import median
import sys
from time import perf_counter
import tracemalloc

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests')]
from clinical_proposals import ProposalLimits, propose_concepts, validate_proposal_plan
from proposal_support import CASES, fixture
from src.package_generation import validation_fingerprint


def evaluate() -> dict:
    fingerprint = validation_fingerprint()
    rows = []
    for name, pages in CASES.items():
        started = perf_counter()
        extraction, mapped, inventory = fixture(pages)
        preparation_seconds = perf_counter() - started
        started = perf_counter()
        plan = propose_concepts(mapped, inventory)
        proposal_seconds = perf_counter() - started
        validate_proposal_plan(plan, mapped, inventory)
        assert not plan.generation_eligible and not plan.clinically_approved and not plan.document_complete
        assert all(not p.human_source_reviewed and not p.generation_eligible for p in plan.proposals)
        assert sum(c.value for c in plan.canonical_character_counts) == len(extraction['full_text'])
        rows.append({'case_id': name, 'plan_id': plan.plan_id, 'source_characters': len(extraction['full_text']),
            'inventory_preparation_seconds': preparation_seconds, 'proposal_seconds': proposal_seconds,
            'proposals': len(plan.proposals), 'statuses': dict(Counter(p.status for p in plan.proposals)),
            'region_counts': {n.name: n.value for n in plan.region_counts},
            'canonical_character_counts': {n.name: n.value for n in plan.canonical_character_counts},
            'source_concerns': len(plan.source_concerns), 'diagnostics': {n.name: n.value for n in plan.diagnostics},
            'example_proposals': [p.model_dump(mode='json') for p in plan.proposals[:2]]})

    benchmarks = []
    for n in (10, 50, 150):
        pages = ['Methods\n\n' + '\n\n'.join(
            f'Adults with condition delta received treatment alpha in protocol {i}. Follow-up was limited to 8 weeks.'
            for i in range(n))]
        extraction, mapped, inventory = fixture(pages)
        timings = []
        for _ in range(5):
            started = perf_counter()
            plan = propose_concepts(mapped, inventory)
            timings.append(perf_counter() - started)
        tracemalloc.start()
        memory_plan = propose_concepts(mapped, inventory)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        assert memory_plan == plan
        limited = propose_concepts(mapped, inventory, limits=ProposalLimits(max_proposals=5))
        benchmarks.append({'clinical_regions': n, 'source_characters': len(extraction['full_text']),
            'repetitions': 5, 'median_seconds': median(timings), 'min_seconds': min(timings),
            'max_seconds': max(timings), 'traced_peak_bytes': peak, 'proposals': len(plan.proposals),
            'diagnostics': {n.name:n.value for n in plan.diagnostics},
            'limited_to_five': {'proposals': len(limited.proposals),
                'omitted_questions': next(c.value for c in limited.diagnostics if c.name == 'omitted_questions_by_limit')}})
    assert validation_fingerprint() == fingerprint
    return {'version': 'phase-a2-synthetic-evaluation-v1', 'live_model_calls': 0, 'mock_model_calls': 0,
        'tokens_measured': False, 'qualified_clinician_reviewed': False,
        'validation_fingerprint': fingerprint, 'cases': rows, 'benchmarks': benchmarks}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='tmp/phase_a2/evaluation.json')
    path = (ROOT / parser.parse_args().output).resolve()
    if not path.is_relative_to((ROOT / 'tmp/phase_a2').resolve()):
        raise SystemExit('Phase A2 output must stay below tmp/phase_a2.')
    result = evaluate()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'source_cases': len(result['cases']), 'live_model_calls': 0,
        'statuses': dict(sum((Counter(r['statuses']) for r in result['cases']), Counter())),
        'benchmarks': result['benchmarks']}))


if __name__ == '__main__':
    main()

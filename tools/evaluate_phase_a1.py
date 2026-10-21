"""Synthetic-only Phase A1 comparison. No PDF, queue, database or provider I/O."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'tests'), str(ROOT / 'tools')]
from editorial_support import Provider, cases, fixture, response
from evaluate_milestone4 import evaluate
from src.chunk_validator import validate_chunks, _validate_source_grounding, _validate_editorial_source_grounding
from src.editorial_generation import generate_editorial_draft


def compare() -> dict:
    started = perf_counter()
    result = evaluate()
    contract = json.loads((ROOT / 'tests/fixtures/phase_a1_regression_contract.json').read_text(encoding='utf-8'))
    case_map = {c['id']: c for c in cases()}
    actual_rows = {(r['case_id'], r['variant']): r for r in result['results']}
    comparisons = []
    for row in contract['rows']:
        case = case_map[row['case_id']]
        name = row['variant']
        if name == 'editorial_example':
            text = case['editorial']
        elif name == 'extractive_comparison':
            text = case['source']
        else:
            attack = next(a for a in case['adversaries'] if a['id'] == name)
            text = case['editorial'].replace(attack['old'], attack['new'])
        extraction, _, _ = fixture(case)
        generated = json.loads(response(case, text))
        generated['chunks'][0]['chunk_number'] = 1
        report = validate_chunks(generated, chunk_sources={1: [{'text': extraction['full_text'], 'review_required': True}]})
        digest = sha256(json.dumps(report, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        comparisons.append({'case_id': row['case_id'], 'variant': name,
            'legacy_report_equal': digest == row['legacy_report_sha256'],
            'editorial_status_equal': actual_rows[row['case_id'], name]['status'] == row['editorial_status'],
            'expected_reason_retained': (not row['expected_reason'] or bool(
                actual_rows[row['case_id'], name]['expected_reason_identified']))})

    errors = []
    for name in ('review_uncertainty', 'conditional_guideline', 'uncertain_dose'):
        case = case_map[name]
        extraction, _, _ = fixture(case)
        counts = []
        for validator in (_validate_source_grounding, _validate_editorial_source_grounding):
            findings = []
            validator(1, case['editorial'], extraction['full_text'], findings)
            counts.append(sum(i['severity'] == 'ERROR' for i in findings))
        errors.append({'case_id': name, 'baseline_grounding_errors': counts[0], 'editorial_grounding_errors': counts[1]})

    case = case_map['conditional_guideline']
    extraction, mapped, plan = fixture(case)
    text = case['source'].replace('The recommendation is conditional, with low certainty of evidence.',
        'The recommendation remains conditional and is based on low certainty of evidence.')
    provider = Provider(response(case, text))
    draft = generate_editorial_draft(extraction, mapped, plan, provider, {})
    controlled = {'basis': 'separate single-grammar-change control, outside original corpus',
        'status': draft.validation['chunk_statuses'][0]['status'],
        'clinically_approved': draft.validation['clinically_approved'],
        'new_review_findings': sum(i['rule'] == 'EDITORIAL_GROUNDING_REVIEW' for i in draft.validation['issues']),
        'mock_calls': len(provider.calls)}
    return {'evaluation': result, 'comparisons': comparisons, 'grounding_errors': errors,
        'controlled_example': controlled, 'corpus_unchanged': result['corpus_sha256'] == contract['corpus_sha256'],
        'prompt_unchanged': result['prompt_sha256'] == contract['prompt_sha256'],
        'live_model_calls': 0, 'elapsed_seconds': perf_counter() - started}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', default='tmp/phase_a1/final-comparison.json')
    path = (ROOT / parser.parse_args().output).resolve()
    if not path.is_relative_to((ROOT / 'tmp/phase_a1').resolve()):
        raise SystemExit('Phase A1 comparison output must stay below tmp/phase_a1.')
    result = compare()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'summary': result['evaluation']['summary'],
        'legacy_reports_equal': sum(c['legacy_report_equal'] for c in result['comparisons']),
        'editorial_statuses_equal': sum(c['editorial_status_equal'] for c in result['comparisons']),
        'grounding_errors': result['grounding_errors'], 'controlled_example': result['controlled_example'],
        'corpus_unchanged': result['corpus_unchanged'], 'prompt_unchanged': result['prompt_unchanged'],
        'elapsed_seconds': result['elapsed_seconds']}))
    if (not result['corpus_unchanged'] or not result['prompt_unchanged'] or
            not all(c['legacy_report_equal'] and c['editorial_status_equal'] and c['expected_reason_retained']
                for c in result['comparisons'])):
        raise SystemExit(1)


if __name__ == '__main__':
    main()

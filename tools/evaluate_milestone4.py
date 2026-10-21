"""Reproducible synthetic editorial evaluation: injected fixtures, zero network.

No clinician labels, model-quality benchmark, PDF ingestion or state migration.
Only sanitized metrics/reason codes are written below tmp/milestone4.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/'tests'))
from editorial_support import cases,fixture,response,Provider
from src.editorial_generation import generate_editorial_draft,EDITORIAL_PROMPT
from src.evidence import text_hash


def evaluate():
    rows=[]
    for case in cases():
        e,m,p=fixture(case)
        variations=[('editorial_example',case['editorial'],None),('extractive_comparison',case['source'],None)]
        for a in case['adversaries']:
            if a['old'] not in case['editorial']:raise ValueError('Invalid synthetic mutation fixture.')
            variations.append((a['id'],case['editorial'].replace(a['old'],a['new']),a['expected_reason']))
        for name,text,expected in variations:
            provider=Provider(response(case,text));draft=generate_editorial_draft(e,m,p,provider,{})
            report=draft.validation;finding=report['claim_assessments'][0]
            reasons=set(finding['reasons'])|{r for c in finding['claims'] for r in c['reasons']}
            rows.append({'case_id':case['id'],'kind':case['kind'],'variant':name,
                'word_count':len(text.split()),'normal_word_target':100<=len(text.split())<=200,
                'copied_full_passage':text==case['source'],'status':report['chunk_statuses'][0]['status'],
                'claim_totals':report['claim_totals'],'reason_codes':sorted(reasons),
                'deterministic_error_rules':sorted({i['rule'] for i in report['issues'] if i['severity']=='ERROR'}),
                'expected_reason':expected,'expected_reason_identified':expected in reasons if expected else None,
                'mock_generation_calls':len(provider.calls)})
    examples=[r for r in rows if r['variant']=='editorial_example']
    attacks=[r for r in rows if r['expected_reason']]
    return {'version':'milestone4-offline-evaluation-v1','corpus_sha256':text_hash((ROOT/'tests/fixtures/editorial_evaluation.json').read_text(encoding='utf-8')),
        'prompt_sha256':text_hash(EDITORIAL_PROMPT.read_text(encoding='utf-8')),
        'qualified_clinician_reviewed':False,'live_model_calls':0,
        'summary':{'source_cases':len(examples),'variants':len(rows),'mock_generation_calls':sum(r['mock_generation_calls'] for r in rows),
            'editorial_examples_review_required':sum(r['status']=='REVIEW_REQUIRED' for r in examples),
            'editorial_examples_rejected':sum(r['status']=='REJECTED' for r in examples),
            'adversarial_variants':len(attacks),'expected_reasons_identified':sum(r['expected_reason_identified'] for r in attacks),
            'adversarial_rejected':sum(r['status']=='REJECTED' for r in attacks),
            'adversarial_review_required':sum(r['status']=='REVIEW_REQUIRED' for r in attacks),
            'automatic_passes':sum(r['status']=='PASS' for r in rows)},'results':rows}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',default='tmp/milestone4/evaluation.json')
    args=parser.parse_args();output=(ROOT/args.output).resolve()
    if not output.is_relative_to((ROOT/'tmp/milestone4').resolve()):
        raise SystemExit('Evaluation output must stay below tmp/milestone4.')
    result=evaluate();output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result['summary']))


if __name__=='__main__':main()

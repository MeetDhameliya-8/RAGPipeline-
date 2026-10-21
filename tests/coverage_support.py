"""Fictional A2/A3 execution with explicit fake providers and disposable storage."""
from editorial_integration_support import prepared,FakeProvider,execute
from clinical_proposals import propose_concepts
from coverage_reconciliation import reconcile_coverage,capture_generation_context


def run(tmp_path,*,paragraphs=None,provider=None,config=None,**kwargs):
    inputs=prepared(paragraphs)
    provider=provider or FakeProvider()
    result=execute(tmp_path,inputs=inputs,provider=provider,config=config,**kwargs)
    context=capture_generation_context(provider,config)
    plan=propose_concepts(inputs[1],inputs[2])
    report=reconcile_coverage(inputs[1],inputs[2],plan,result,extraction=inputs[0],
        workspace=tmp_path/'editorial',db_path=tmp_path/'review.sqlite3',generation=context)
    return report,inputs,plan,result,context


def again(tmp_path,inputs,plan,result,context,**changes):
    values=dict(source=inputs[1],inventory=inputs[2],proposals=plan,workflow=result,
        extraction=inputs[0],workspace=tmp_path/'editorial',db_path=tmp_path/'review.sqlite3',generation=context)
    values.update(changes)
    return reconcile_coverage(**values)

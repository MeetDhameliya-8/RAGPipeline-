"""Explicit proposal-to-local-review orchestration; no queue or production route."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import sqlite3
from collections import Counter
from time import perf_counter
from typing import Callable

from clinical_proposals import ProposalLimits, ProposalPlan, propose_concepts, validate_proposal_plan
from clinical_proposals.source_adapter import digest, pin_inputs
from src import diagnostics as telemetry
from src.bounded_scheduler import SubmissionControl, run_bounded
from src.document_concepts import DocumentConceptPlan, evidence_fingerprint
from src.source_map import SourceDocument
from src.generation_preflight import require_source_reference
from src.providers.base import ChunkGenerationError
from src.queue_recovery import SOURCE_HOLD_CODES, RETRY_CODES
from . import requests, validation, storage
from .checkpoints import Checkpoints, workspace_lock, check_path_budget
from .eligibility import assess_eligibility
from .models import (WorkflowConfig, WorkflowError, WorkflowInterrupted, WorkflowResult, UnitResult, ReviewDraft)


def _extraction_identity(extraction):
    return digest({k:extraction.get(k) for k in ('file_name','full_text','reference_source_text','reference_metadata')})


@telemetry.observed_run
@telemetry.timed('proposal_editorial_workflow')
def _run_proposal_editorial(extraction: dict, source_doc: SourceDocument, inventory: DocumentConceptPlan, *,
                          provider, workspace: Path, db_path: Path, diagnostics: dict,
                          proposal_plan: ProposalPlan | None = None,
                          config: WorkflowConfig | None = None,
                          control: SubmissionControl | None = None,
                          progress: Callable[[int], None] | None = None) -> WorkflowResult:
    """Write review drafts only through an explicit provider, workspace and DB.

    Inputs are prepared in memory. This API performs no extraction, environment
    provider selection, service startup, retry loop, approval or RAG publishing.
    Each source-ordered draft retains the existing independent JSON envelope.
    """
    config = config or WorkflowConfig()
    if not isinstance(config,WorkflowConfig) or provider is None or not isinstance(diagnostics,dict):
        raise WorkflowError('CONFIGURATION_ERROR')
    try:
        provider_hash = requests.provider_identity(provider)
        source, pinned_inventory, _, _ = pin_inputs(source_doc,inventory,
            proposal_plan.limits if proposal_plan else ProposalLimits())
        prepared = deepcopy(extraction)
        if (prepared.get('full_text') != source.evidence.buffers[source.evidence.canonical_buffer_id].text
                or prepared.get('file_name') != source.source_file or source.evidence.identity_quality != 'pdf_bytes'):
            raise WorkflowError('EXTRACTION_BLOCKED')
        reference = require_source_reference(prepared)
        with telemetry.stage('proposal_identity_validation'):
            proposals = proposal_plan or propose_concepts(source,pinned_inventory)
            if proposal_plan is not None:
                validate_proposal_plan(proposals,source,pinned_inventory)
        extraction_hash = _extraction_identity(prepared)
        inventory_hash = digest(pinned_inventory.model_dump(mode='json'))
        source_hash = digest(source.model_dump(mode='json'))
        proposals_hash = digest(proposals.model_dump(mode='json'))
        workspace, db_path = Path(workspace).resolve(), Path(db_path).resolve()
        check_path_budget(workspace)
        storage.check_target(db_path)
        prompt = requests.editorial_prompt()
        policy = requests.validation_policy()
    except WorkflowError:
        raise
    except ChunkGenerationError as exc:
        raise WorkflowError(telemetry.safe_code(exc.code)) from None
    except (ValueError,KeyError,TypeError,AttributeError,OSError):
        raise WorkflowError('EXTRACTION_BLOCKED') from None

    def check_current():
        try:
            if (evidence_fingerprint(source_doc) != proposals.evidence_fingerprint or
                digest(source_doc.model_dump(mode='json')) != source_hash or
                digest(inventory.model_dump(mode='json')) != inventory_hash or
                _extraction_identity(extraction) != extraction_hash or
                (proposal_plan is not None and digest(proposal_plan.model_dump(mode='json')) != proposals_hash)):
                raise ValueError()
        except (ValueError,TypeError,KeyError,AttributeError):
            raise WorkflowError('EXTRACTION_BLOCKED') from None

    check_current()
    with telemetry.stage('proposal_draft_eligibility'):
        eligibility = assess_eligibility(source,proposals,config)
    schedule = [e for e in eligibility if e.state == 'draftable_review']
    control = control or SubmissionControl()
    audit = diagnostics
    audit.update(version='proposal-editorial-v1',proposal_plan_id=proposals.plan_id,
        evidence_fingerprint=proposals.evidence_fingerprint,validation_policy=policy,
        proposed_concepts=len(eligibility),eligible_drafting_units=len(schedule),planned_generation_calls=len(schedule),
        corrective_calls=0,document_complete=False,clinically_approved=False,production_ingestion=False)
    audit['eligibility_counts'] = dict(Counter(e.state for e in eligibility))
    for name, value in (('proposed_concepts',len(eligibility)),('planned_source_units',len(schedule)),
                        ('eligible_drafting_units',len(schedule)),('planned_generation_calls',len(schedule))):
        telemetry.count(name,value,replace=True)
    telemetry.count('held_proposal_units',sum(e.state != 'draftable_review' for e in eligibility),replace=True)
    observation = telemetry.current()
    results = {e.proposal_id:UnitResult(proposal_id=e.proposal_id,source_order=e.source_order,
        status=e.state,code=None,audit={'reasons':e.reasons}) for e in eligibility if e.state != 'draftable_review'}
    documents = {}
    resumed_count = 0
    unexpected = False
    interruption = None

    with workspace_lock(workspace):
        checkpoints = Checkpoints(workspace)

        def worker(index):
            item = schedule[index]
            with telemetry.bind(observation):
                try:
                    check_current()
                    request, alias, key = requests.build_request(source,proposals,item,reference,provider_hash,prompt)
                    part = {'proposal_id':item.proposal_id,'source_order':item.source_order,'logical_calls':0}
                    with telemetry.stage('checkpoint_lookup'):
                        record = checkpoints.load(key)
                    resumed = record is not None
                    if resumed:
                        telemetry.count('resumed_candidate_units')
                    else:
                        failure_path = checkpoints.path(key,'last-failure')
                        if failure_path.exists():
                            import json
                            failure = json.loads(failure_path.read_text(encoding='utf-8'))
                            if failure.get('code') in SOURCE_HOLD_CODES:
                                telemetry.count('cached_source_hold_units')
                                return UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,
                                    status='source_hold',code=failure['code'],candidate_key=key,audit=part),None
                        if checkpoints.uncertain(key) and not config.retry_uncertain_requests:
                            telemetry.incomplete_candidates()
                            return UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,
                                status='unknown_request_outcome',code='GENERATION_FAILED',candidate_key=key,audit=part),None
                        with control.gate:
                            if control.stopped.is_set():
                                return UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,
                                    status='deferred_after_stop',candidate_key=key,audit=part),None
                            checkpoints.intent(key)
                            part['logical_calls'] = 1
                        try:
                            provider_started = perf_counter()
                            raw = telemetry.model_call('generation',provider.generate,request,alias,prompt)
                        except ChunkGenerationError as exc:
                            part['provider_seconds'] = perf_counter()-provider_started
                            code = telemetry.safe_code(exc.code)
                            detail = telemetry.failure(exc,code)
                            telemetry.incomplete_candidates()
                            if code in {'AUTHENTICATION_FAILED','QUOTA_EXHAUSTED'}:
                                control.stop(code)
                            checkpoints.failure(key,{'code':code,'failure_classification':detail})
                            part['failure_classification'] = detail
                            return UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,
                                status='provider_failure',code=code,candidate_key=key,audit=part),None
                        part['provider_seconds'] = perf_counter()-provider_started
                        if not isinstance(raw,str):
                            raise WorkflowError('MALFORMED_RESPONSE')
                        # Publish before parsing or considering mutable caller
                        # inputs again; returned responses survive validation failure.
                        with telemetry.stage('candidate_publication'):
                            record = checkpoints.publish(key,str(raw),telemetry.transport_metadata(getattr(raw,'metadata',{})))
                        check_current()
                    part['response_metadata'] = telemetry.transport_metadata(record.get('transport',{}))
                    validation_started = perf_counter()
                    assessment = validation.assess_response(record['raw'],alias=alias,reference=reference,
                        extraction=prepared,source=source,eligibility=item,resumed=resumed)
                    part['validation_seconds'] = perf_counter()-validation_started
                    previous = checkpoints.decision(key,policy,record,assessment.report)
                    part['prior_validation_decision'] = previous
                    telemetry.count('validation_decisions_recomputed')
                    if previous == 'changed_policy':
                        telemetry.count('validation_policy_reconsidered_units')
                    verdict = 'review_required' if assessment.code is None else 'source_hold' if assessment.code == 'SOURCE_PASSAGE_HELD' else 'quarantined'
                    return UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,status=verdict,
                        code=assessment.code,candidate_key=key,candidate_sha256=record['raw_sha256'],resumed=resumed,
                        validation=assessment.report,audit=part),assessment.document
                except BaseException:
                    telemetry.incomplete_candidates()
                    control.stop()
                    raise

        run = run_bounded(worker,len(schedule),config.concurrency,control,progress)
        if isinstance(run.interruption,(KeyboardInterrupt,SystemExit)):
            interruption = run.interruption
        elif run.interruption is not None:
            unexpected = True
            audit['scheduler_failure'] = telemetry.failure(run.interruption,'GENERATION_FAILED')
        for outcome in run.outcomes:
            item = schedule[outcome.index]
            if outcome.error is not None:
                if isinstance(outcome.error,(KeyboardInterrupt,SystemExit)):
                    interruption = outcome.error
                else:
                    unexpected |= not outcome.cancelled
                detail = telemetry.failure(outcome.error,'GENERATION_FAILED')
                results[item.proposal_id] = UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,
                    status='cancelled' if outcome.cancelled else 'unexpected_failure',code=telemetry.safe_code(getattr(outcome.error,'code','GENERATION_FAILED')),
                    audit={'failure_classification':detail})
            else:
                unit, document = outcome.value
                results[item.proposal_id] = unit
                resumed_count += int(unit.resumed)
                if document is not None:
                    documents[item.proposal_id] = document
        for item in schedule:
            if item.proposal_id not in results:
                results[item.proposal_id] = UnitResult(proposal_id=item.proposal_id,source_order=item.source_order,
                    status='not_submitted',code=control.fatal_code)

        audit['scheduler'] = {'mode':'rolling','max_in_flight':run.max_in_flight,
            'submitted_units':len(run.outcomes),'unsubmitted_units':len(schedule)-len(run.outcomes),
            'cancelled_units':sum(o.cancelled for o in run.outcomes),'submission_stopped':control.stopped.is_set()}
        telemetry.count('scheduler_in_flight_high_water',run.max_in_flight,replace=True)
        drafts = []
        seen = {}
        for item in eligibility:
            unit = results[item.proposal_id]
            if unit.status == 'quarantined' and unit.candidate_key:
                try:
                    storage.quarantine_owned(workspace,unit.candidate_key,policy,db_path)
                except (WorkflowError,OSError,ValueError,sqlite3.Error) as exc:
                    unexpected = True
                    results[item.proposal_id] = unit.model_copy(update={'storage':{
                        'quarantine_failed':True,'failure_classification':telemetry.failure(exc,'GENERATION_FAILED')}})
            if unit.status != 'review_required':
                continue
            document = documents[item.proposal_id]
            content = document['chunks'][0]
            content_id = digest({k:content[k] for k in ('topic','subtopic','section','reference','original_text')})
            if content_id in seen:
                telemetry.count('duplicate_candidates')
                results[item.proposal_id] = unit.model_copy(update={'status':'duplicate_generated',
                    'audit':dict(unit.audit,duplicate_of=seen[content_id])})
                continue
            seen[content_id] = item.proposal_id
            linkage = {'version':'proposal-editorial-v1','candidate_key':unit.candidate_key,
                'proposal_id':item.proposal_id,'source_order':item.source_order,
                'pdf_sha256':source.evidence.pdf_sha256,'evidence_fingerprint':proposals.evidence_fingerprint,
                'ownership':[s.model_dump() for s in item.ownership],'context':[s.model_dump() for s in item.context],
                'human_source_reviewed':False,'clinically_approved':False}
            try:
                check_current()
                stored = storage.save_review(workspace,unit.candidate_key,document,unit.validation,linkage,db_path)
                status = 'stored_review' if stored.get('persisted') else 'existing_row_preserved'
                results[item.proposal_id] = unit.model_copy(update={'status':status,'storage':stored})
                if stored.get('persisted'):
                    telemetry.count('saved_review_drafts')
                    telemetry.count('final_stored_rows')
            except (WorkflowError,OSError,ValueError,sqlite3.Error) as exc:
                detail = telemetry.failure(exc,'GENERATION_FAILED')
                results[item.proposal_id] = unit.model_copy(update={'status':'storage_failed',
                    'storage':{'readback_verified':False,'code':getattr(exc,'code','STORAGE_FAILED'),
                        'failure_classification':detail}})
            drafts.append(ReviewDraft(proposal_id=item.proposal_id,source_order=item.source_order,document=document,
                validation=unit.validation,local_package_id=(results[item.proposal_id].storage or {}).get('local_package_id')))

        ordered = tuple(results[e.proposal_id] for e in eligibility)
        stored_count = sum(u.status == 'stored_review' for u in ordered)
        gaps = sum(u.status not in {'stored_review','duplicate_generated','existing_row_preserved'} for u in ordered)
        status = 'REVIEW_DRAFTS_SAVED' if stored_count and not gaps else 'PARTIAL_REVIEW_DRAFTS_SAVED' if stored_count else 'HELD_OR_FAILED'
        if interruption is not None:
            status = 'INTERRUPTED'
        elif unexpected:
            status = 'FAILED_WITH_RECOVERABLE_CANDIDATES'
        audit.update(resumed_units=resumed_count,reviewable_drafts=len(drafts),persisted_drafts=stored_count,
            validation_rejections=sum(u.status == 'quarantined' for u in ordered),source_proposal_gaps=gaps,
            unit_outcomes=[{'proposal_id':u.proposal_id,'source_order':u.source_order,'status':u.status,'code':u.code,
                'eligibility_state':e.state,'eligibility_reasons':e.reasons,'audit':u.audit,'storage':u.storage}
                for e,u in zip(eligibility,ordered)],
            recovery_signals=[{'code':u.code,'retryable':u.code in RETRY_CODES} for u in ordered if u.code],
            source_region_counts=[c.model_dump() for c in proposals.region_counts],
            source_regions_not_yet_assessed=sum(r.state == 'not_yet_assessed' for r in proposals.regions),
            source_regions_blocked=sum(r.state == 'blocked_source_region' for r in proposals.regions),
            document_coverage_complete=False)
        telemetry.count('merged_candidates',len(drafts),replace=True)
        result = WorkflowResult(status=status,eligibility=eligibility,units=ordered,drafts=tuple(drafts),diagnostics=audit,
            proposal_plan_id=proposals.plan_id,evidence_fingerprint=proposals.evidence_fingerprint)
        telemetry.attach(result.diagnostics)
        if observation is not None:
            observation.publish()
    if interruption is not None:
        raise WorkflowInterrupted(result) from None
    if unexpected:
        raise WorkflowError('GENERATION_FAILED',result=result) from None
    return result


def run_proposal_editorial(extraction: dict, source_doc: SourceDocument, inventory: DocumentConceptPlan, *,
                          provider, workspace: Path, db_path: Path, diagnostics: dict,
                          proposal_plan: ProposalPlan | None = None,
                          config: WorkflowConfig | None = None,
                          control: SubmissionControl | None = None,
                          progress: Callable[[int], None] | None = None) -> WorkflowResult:
    """Explicit opt-in API. Persist the completed M1 snapshot after its timers close."""
    result = None
    try:
        result = _run_proposal_editorial(extraction,source_doc,inventory,provider=provider,
            workspace=workspace,db_path=db_path,diagnostics=diagnostics,proposal_plan=proposal_plan,
            config=config,control=control,progress=progress)
        return result
    except (WorkflowError,WorkflowInterrupted) as exc:
        result = exc.result
        raise
    finally:
        if result is not None:
            Checkpoints(Path(workspace).resolve()).run_report(result.diagnostics)

"""Explicit local reconciliation, never generation, repair or approval."""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from pathlib import Path
import re
from typing import Any
from pydantic import BaseModel

from clinical_proposals import ProposalPlan,ProposalLimits,propose_concepts
from clinical_proposals.source_adapter import digest,pin_inputs
from clinical_proposals.models import SpanRef
from proposal_editorial.models import WorkflowResult
from proposal_editorial.models import WorkflowConfig
from proposal_editorial.eligibility import assess_eligibility
from proposal_editorial import requests
from src.document_concepts import DocumentConceptPlan
from src.source_map import SourceDocument
from src.generation_preflight import require_source_reference
from src import diagnostics as telemetry
from .models import (CoverageReport,GenerationContext,ReconciliationLimits,RegionLink,ProposalLink,
    SourceIdentity,StageObservation,StageRecord,VerificationError)
from .accounting import union_characters,task
from .artifacts import ArtifactReader
from .verification import (HEX,candidate_proof,verify_workflow,verify_context,expected_link)
from .storage import StorageSnapshot,verify_storage

STAGES=('extraction','citation','source_inventory','proposals','generation','validation','storage','clinical_review')
COUNT_NAMES=('source_regions','canonical_characters','proposals','nonduplicate_proposal_units',
    'unique_owned_regions','owned_characters_union','context_characters_union','context_link_characters',
    'ownership_and_context_characters_union','generated_candidates','known_generated_candidates',
    'verified_response_units','known_eligible_candidate_events','unique_reviewable_drafts',
    'rejected_units','held_units','deferred_units','unknown_generation_units','resumed_units',
    'verified_stored_review_drafts','revoked_storage_units','unknown_storage_units',
    'independent_clinical_facts')


def implementation_identity() -> str:
    return digest({p.name:__import__('hashlib').sha256(p.read_bytes()).hexdigest()
        for p in sorted(Path(__file__).parent.glob('*.py'))})


def _check_budget(values,limits):
    source,inventory,plan,workflow=values
    if ((inventory is not None and len(inventory.regions)>limits.max_regions)
            or (plan is not None and len(plan.proposals)>limits.max_proposals)
            or (workflow is not None and (len(workflow.drafts)>limits.max_drafts
                or len(workflow.units)>limits.max_proposals or len(workflow.eligibility)>limits.max_proposals))):
        raise VerificationError('INPUT_BUDGET_EXCEEDED')
    stack=[iter(values)]
    records=characters=0
    while stack:
        try:
            value=next(stack[-1])
        except StopIteration:
            stack.pop();continue
        records+=1
        if isinstance(value,str):
            characters+=len(value)
        elif isinstance(value,BaseModel):
            stack.append(iter(map(value.__getattribute__,type(value).model_fields)))
        elif isinstance(value,dict):
            stack.append(iter(value.values()))
        elif isinstance(value,(tuple,list)):
            stack.append(iter(value))
        if records>limits.max_input_records or characters>limits.max_input_characters:
            raise VerificationError('INPUT_BUDGET_EXCEEDED')


def _claim_reasons(report: dict) -> tuple[str,...]:
    # Only fresh, deterministically reconstructed assessments enter this helper.
    values={i['rule'] for i in report.get('issues',[])}
    values.update(r for c in report.get('claim_assessments',[]) for r in c.get('reasons',[]))
    return tuple(sorted(values))


def _proposal(item,proposal,*,source,plan,extraction,reference,reader,snapshot,context,policy,unit,draft,
              workflow_verified,seen):
    values=dict(proposal_id=proposal.proposal_id,category=proposal.category,region_ids=proposal.source_region_ids,
        ownership=proposal.ownership,context=tuple(c.evidence for c in proposal.supporting_context),
        a2_status=proposal.status,eligibility=item.state if workflow_verified else None,
        eligibility_reasons=item.reasons,claimed_generation_status=unit.status if unit else None,
        duplicate_of=proposal.duplicate_of)
    problems=[]
    if item.state!='draftable_review':
        outcome={'relationship_review':'held_before_generation','blocked_source':'held_before_generation',
            'insufficient_evidence':'insufficient_source_evidence','duplicate_deferred':'duplicate_deferred'}[item.state]
        if 'generation_request_budget_exceeded' in item.reasons:
            outcome='request_budget_deferred'
        if unit and unit.status!=item.state:
            problems.append('HELD_PROPOSAL_OUTCOME_MISMATCH')
        return ProposalLink(**values,generation=outcome,checkpoint='not_scheduled',
            generated_candidate_count=0 if workflow_verified else None,validation='not_started',
            persistence='not_stored' if workflow_verified else 'reconciliation_required',verification_issues=tuple(problems))
    try:
        if context is None or reference is None:
            raise VerificationError('GENERATION_CONFIGURATION_OR_CITATION_UNVERIFIED')
        request,alias,key=requests.build_request(source,plan,item,reference,context.provider_sha256,requests.editorial_prompt())
        values['candidate_key']=key
        if unit and unit.candidate_key is not None and unit.candidate_key!=key:
            raise VerificationError('CANDIDATE_IDENTITY_INVALID')
        proof=candidate_proof(reader,key,policy,alias=alias,reference=reference,
            extraction=extraction,source=source,eligibility=item)
        values.update(checkpoint=proof.checkpoint,attempt_count=proof.attempts,
            response_sha256=proof.raw_hash,generated_candidate_count=proof.count,validation_policy=policy)
        if unit and unit.candidate_sha256 is not None and unit.candidate_sha256!=proof.raw_hash:
            raise VerificationError('CANDIDATE_DIGEST_MISMATCH')
        if proof.assessment is None:
            if proof.checkpoint=='request_failed':
                if not unit or unit.status!='provider_failure' or unit.code!=proof.failure_code:
                    problems.append('REQUEST_FAILURE_LINK_UNVERIFIED')
                return ProposalLink(**values,generation='provider_failure',failure_code=proof.failure_code,validation='not_started',
                    persistence='not_stored',verification_issues=tuple(problems))
            if proof.checkpoint=='no_request_record' and unit and unit.status in {'not_submitted','deferred_after_stop','cancelled'}:
                return ProposalLink(**{**values,'generated_candidate_count':0},generation='service_stop_deferred',
                    failure_code=telemetry.safe_code(unit.code) if unit.code else None,validation='not_started',persistence='not_stored')
            if unit and unit.status not in {'unknown_request_outcome','unexpected_failure','cancelled'}:
                problems.append('CANDIDATE_RECORD_MISSING')
            return ProposalLink(**values,generation='interrupted_or_unknown' if proof.attempts else 'not_started',
                validation='incomplete',persistence='reconciliation_required',verification_issues=tuple(problems))
        if not proof.validation_verified:
            raise VerificationError('VALIDATION_RECORD_UNAVAILABLE')
        verdict=proof.assessment
        if unit and unit.validation is not None and unit.validation!=verdict.report:
            raise VerificationError('WORKFLOW_VALIDATION_MISMATCH')
        if unit and unit.resumed:
            if unit.audit.get('logical_calls')!=0:
                problems.append('RESUMPTION_CLAIM_UNVERIFIED')
            else:
                values['resumed_from_checkpoint']=True
        elif unit:
            values['resumed_from_checkpoint']=False
        values['validation_reasons']=_claim_reasons(verdict.report)
        eligible=verdict.code is None
        if verdict.code=='SOURCE_PASSAGE_HELD':
            return ProposalLink(**values,generation='generated_source_hold',validation='source_hold',persistence='not_stored')
        values.update(generation='draft_generated_review_eligible' if eligible else 'generated_rejected',
            validation='review_required' if eligible else 'rejected',
            clinical_verification='clinical_review_required' if eligible else 'not_clinically_reviewed')
        if eligible and unit and unit.status not in {'stored_review','storage_failed','existing_row_preserved','review_required','duplicate_generated'}:
            problems.append('VALIDATION_DISPOSITION_MISMATCH')
        if not eligible and unit and unit.status!='quarantined':
            problems.append('VALIDATION_DISPOSITION_MISMATCH')
        if verdict.document is None:
            return ProposalLink(**values,persistence='not_stored',verification_issues=tuple(problems))
        if draft is not None and (draft.source_order!=item.source_order or draft.document!=verdict.document or draft.validation!=verdict.report):
            problems.append('DRAFT_LINK_INVALID')
        if workflow_verified and eligible and unit.status!='duplicate_generated' and draft is None:
            problems.append('DRAFT_LINK_UNAVAILABLE')
        content=verdict.document['chunks'][0]
        content_id=digest({k:content[k] for k in ('topic','subtopic','section','reference','original_text')})
        if eligible and content_id in seen:
            if unit and (unit.status!='duplicate_generated' or unit.audit.get('duplicate_of')!=seen[content_id]):
                problems.append('GENERATED_DUPLICATE_LINK_INVALID')
            return ProposalLink(**{**values,'generation':'duplicate_deferred','duplicate_of':seen[content_id]},
                persistence='not_stored',verification_issues=tuple(problems))
        if eligible:
            seen[content_id]=proposal.proposal_id
        persistence,chunk_id,storage_issues=verify_storage(reader,snapshot,key,verdict.document,
            expected_link(source,plan,item,key),eligible=eligible,policy=policy)
        problems.extend(storage_issues)
        if unit and unit.storage and unit.storage.get('chunk_id') and unit.storage['chunk_id']!=chunk_id:
            problems.append('STORAGE_CLAIM_ID_MISMATCH')
        if unit and unit.status=='stored_review' and persistence!='verified_local_review':
            problems.append('CLAIMED_STORAGE_NOT_VERIFIED')
        if unit and unit.status=='storage_failed' and persistence!='eligibility_revoked':
            persistence='readback_incomplete'
        if problems and persistence=='verified_local_review':
            persistence='reconciliation_required';chunk_id=None
        return ProposalLink(**values,persistence=persistence,chunk_id=chunk_id,verification_issues=tuple(sorted(set(problems))))
    except VerificationError as exc:
        problems.append(exc.code)
    except (ValueError,TypeError,KeyError,AttributeError,OSError):
        problems.append('CANDIDATE_OR_STORAGE_VERIFICATION_INCOMPLETE')
    return ProposalLink(**values,verification_issues=tuple(sorted(set(problems))))


def _regions(inventory,plan,links,source=None):
    owners={r.region_id:[] for r in inventory.regions}
    contexts={r.region_id:[] for r in inventory.regions}
    roles={r.region_id:set(c.role for c in r.cues) for r in inventory.regions}
    a2={r.region_id:r for r in plan.regions} if plan else {}
    if plan:
        for p in plan.proposals:
            for rid in p.source_region_ids:
                owners[rid].append(p.proposal_id)
                roles[rid].update(r.role for r in p.potential_roles if r.role=='contraindication')
            for c in p.supporting_context:
                contexts[c.region_id].append(p.proposal_id)
    by_id={p.proposal_id:p for p in links}
    exclusions={e.region_id for e in inventory.exclusions}
    result=[]
    for r in inventory.regions:
        assigned=tuple(owners[r.region_id]); supporting=tuple(contexts[r.region_id])
        state='not_yet_assessed'
        if r.coverage=='held_for_source_review' or (r.region_id in a2 and a2[r.region_id].state=='blocked_source_region'):
            state='existing_source_hold'
        elif r.region_id in exclusions:
            state='reviewed_nonclinical_exclusion'
        elif r.coverage=='intentionally_excluded' or (r.region_id in a2 and a2[r.region_id].state=='excluded_or_nonclinical'):
            state='structural_candidate_exclusion'
        elif assigned:
            state='owned_by_proposals'
        elif supporting or r.coverage=='supporting_context_only':
            state='supporting_context_only'
        reviewable=tuple(pid for pid in assigned if pid in by_id and by_id[pid].generation=='draft_generated_review_eligible'
            and not by_id[pid].verification_issues)
        stored=tuple(pid for pid in assigned if pid in by_id and by_id[pid].persistence=='verified_local_review')
        concern_cues=roles[r.region_id]&{'primary_outcome','safety','contraindication','recommendation',
            'recommendation_strength','evidence_certainty','limitation','applicability'}
        if source is not None and r.ownership is not None:
            selected=source.evidence.buffers[r.ownership.buffer_id].text[r.ownership.char_start:r.ownership.char_end]
            if 'recommendation' in roles[r.region_id] and re.search(r'\bconditional\b',selected,re.I):
                concern_cues.add('conditional_recommendation')
            if re.search(r'\b(?:very low|low)[ -]certainty\b',selected,re.I):
                concern_cues.add('low_certainty_evidence')
        cues=tuple(sorted(concern_cues))
        result.append(RegionLink(region_id=r.region_id,basis=r.basis,
            span=SpanRef.from_evidence(r.ownership) if r.ownership else None,
            native_spans=tuple(SpanRef.from_evidence(f.source) for f in r.trace.fragments),
            pages=tuple(r.page_numbers),sections=tuple(r.section_ids),source_state=state,inventory_state=r.coverage,
            proposal_ids=assigned,context_for=supporting,proposal_links_verified=plan is not None,reviewable_proposals=reviewable,
            verified_storage_proposals=stored,literal_concern_cues=cues,reasons=tuple(r.reasons)))
    return tuple(result)


def _tasks(source,regions,links,global_issues):
    revision=source.evidence_fingerprint if source else None
    by_region={r.region_id:r for r in regions}
    tasks=[]
    def region_task(r,reason,kind='document_coverage',classification='uncertainty'):
        tasks.append(task(revision=revision,regions=(r.region_id,),spans=(r.span,) if r.span else r.native_spans,
            pages=r.pages,reason=reason,kind=kind,outcome=r.source_state,classification=classification))
    for r in regions:
        if r.source_state=='existing_source_hold':
            region_task(r,'SOURCE_EVIDENCE_HELD','source_extraction','source_blocking')
        if 'structured_relationship_unproven' in r.reasons:
            region_task(r,'UNVERIFIED_TABLE_FIGURE_RELATIONSHIP','clinical_relationship')
        if r.source_state=='not_yet_assessed':
            region_task(r,'SOURCE_REGION_UNASSESSED')
        if not r.proposal_ids and r.source_state in {'not_yet_assessed','existing_source_hold'}:
            region_task(r,'SOURCE_REGION_WITHOUT_PROPOSAL' if r.proposal_links_verified else 'PROPOSAL_LINK_UNAVAILABLE')
        if r.proposal_ids and not r.reviewable_proposals:
            region_task(r,'CONCEPT_WITHOUT_REVIEWABLE_DRAFT')
        if not r.verified_storage_proposals:
            for cue in r.literal_concern_cues:
                region_task(r,'LITERAL_'+cue.upper()+'_COVERAGE_CONCERN')
        if r.source_state=='structural_candidate_exclusion':
            region_task(r,'NONCLINICAL_EXCLUSION_UNREVIEWED')
    for p in links:
        pages=tuple(sorted({page for rid in p.region_ids for page in by_region[rid].pages}))
        def proposal_task(reason,kind,classification='uncertainty'):
            tasks.append(task(revision=revision,regions=p.region_ids,spans=p.ownership+p.context,pages=pages,
                proposal=p.proposal_id,candidate=p.candidate_key,reason=reason,kind=kind,
                outcome=p.generation+'/'+p.persistence,classification=classification))
        if p.generation=='draft_generated_review_eligible':
            proposal_task('CLINICAL_PARAPHRASE_FIDELITY_REVIEW','clinical_paraphrase_fidelity','mandatory_clinical_review')
        if p.generation=='held_before_generation':
            proposal_task('CLINICAL_RELATIONSHIP_UNRESOLVED','clinical_relationship')
        if p.generation=='generated_source_hold':
            proposal_task('MODEL_SOURCE_ABSTENTION','source_extraction','source_blocking')
        if p.context or any('context' in r or 'qualification' in r for r in p.eligibility_reasons):
            proposal_task('CONTEXT_OR_QUALIFICATION_REVIEW','missing_qualification')
        if p.generation=='generated_rejected':
            proposal_task('GENERATED_CANDIDATE_REJECTED','clinical_paraphrase_fidelity')
        if p.generation in {'interrupted_or_unknown','verification_incomplete','not_started','provider_failure'}:
            proposal_task('GENERATION_EVIDENCE_INCOMPLETE','document_coverage','technical_incomplete')
        if p.generation in {'duplicate_deferred','request_budget_deferred','service_stop_deferred','insufficient_source_evidence'}:
            proposal_task('DEFERRED_OR_INSUFFICIENT_CONCEPT','document_coverage')
        if p.persistence in {'readback_incomplete','reconciliation_required','eligibility_revoked'}:
            proposal_task('LOCAL_STORAGE_RECONCILIATION','local_storage_reconciliation','technical_incomplete')
        if 'INVALID_REFERENCE' in p.validation_reasons:
            proposal_task('CITATION_IDENTITY_REVIEW','citation_identity','source_blocking')
    for reason in sorted(set(global_issues)):
        tasks.append(task(revision=revision,reason=reason,
            kind='citation_identity' if 'CITATION' in reason or 'REFERENCE' in reason else 'document_coverage',
            outcome='verification_incomplete',classification='technical_incomplete'))
    return tuple(sorted({t.task_id:t for t in tasks}.values(),key=lambda t:t.task_id))


@telemetry.timed('coverage_reconciliation')
def reconcile_coverage(source: SourceDocument | None=None,inventory: DocumentConceptPlan | None=None,
                       proposals: ProposalPlan | None=None,workflow: WorkflowResult | None=None,*,
                       extraction: dict | None=None,workspace: Path | None=None,db_path: Path | None=None,
                       generation: GenerationContext | None=None,observations: tuple[StageObservation,...]=(),
                       limits: ReconciliationLimits | None=None,run_id: str | None=None) -> CoverageReport:
    """Reconcile explicit inputs and read-only snapshots; unknown counts stay null.

    No provider call, queue access, DB initialization, checkpoint write, source
    repair or approval occurs. Physical PDF bytes are not reread: verification
    is against the current prepared evidence graph and its recorded PDF hash.
    """
    invalid_limits=False
    try:
        limits=ReconciliationLimits.model_validate((limits or ReconciliationLimits()).model_dump())
    except (ValueError,AttributeError,TypeError):
        limits=ReconciliationLimits();invalid_limits=True
    stages={s:StageRecord(stage=s) for s in STAGES}
    counts={name:None for name in COUNT_NAMES}
    issues=[]; identity=None; plan=None; links=(); regions=(); chars={}; state_hash=digest(None)
    safe_observations=()
    reader=ArtifactReader(workspace,limits); storage_evidence={}; result_verified=False
    policy=requests.validation_policy()
    try:
        if invalid_limits:
            raise VerificationError('INPUT_LIMITS_INVALID')
        _check_budget((source,inventory,proposals,workflow),limits)
        observations=tuple(StageObservation.model_validate(o.model_dump()) for o in observations)
        safe_observations=observations
        if len({o.stage for o in observations})!=len(observations):
            raise VerificationError('STAGE_OBSERVATIONS_INVALID')
        state_hash=digest({'source':source.model_dump(mode='json') if source else None,
            'inventory':inventory.model_dump(mode='json') if inventory else None,
            'proposals':proposals.model_dump(mode='json') if proposals else None,
            'workflow':workflow.model_dump(mode='json') if workflow else None,
            'context':generation.model_dump(mode='json') if generation else None,
            'observations':[o.model_dump() for o in observations],'limits':limits.model_dump()})
        if source is None or inventory is None:
            raise VerificationError('SOURCE_INVENTORY_UNAVAILABLE')
        with telemetry.stage('coverage_source_verification'):
            pinned,pinned_inventory,_,_=pin_inputs(source,inventory,proposals.limits if proposals else ProposalLimits())
        source=pinned; inventory=pinned_inventory
        if source.evidence.identity_quality!='pdf_bytes' or not HEX.fullmatch(source.evidence.pdf_sha256 or ''):
            raise VerificationError('SOURCE_IDENTITY_UNVERIFIED')
        identity=SourceIdentity(pdf_sha256=source.evidence.pdf_sha256,evidence_document_id=source.evidence.document_id,
            canonical_buffer_id=source.evidence.canonical_buffer_id,evidence_fingerprint=inventory.evidence_fingerprint,
            source_map_sha256=digest(source.model_dump(mode='json')),inventory_sha256=digest(inventory.model_dump(mode='json')))
        stages['source_inventory']=StageRecord(stage='source_inventory',available=True,state='completed_verified')
        counts['source_regions']=len(inventory.regions)
        counts['canonical_characters']=len(source.evidence.buffers[source.evidence.canonical_buffer_id].text)
        reference=None
        if extraction is not None:
            if extraction.get('full_text')!=source.evidence.buffers[source.evidence.canonical_buffer_id].text or extraction.get('file_name')!=source.source_file:
                raise VerificationError('PREPARED_EXTRACTION_IDENTITY_INVALID')
            extraction=deepcopy(extraction)
            stages['extraction']=StageRecord(stage='extraction',available=True,state='completed_verified',
                reasons=('PREPARED_GRAPH_VERIFIED_WITHOUT_PDF_REEXTRACTION',))
            try:
                reference=require_source_reference(extraction)
                stages['citation']=StageRecord(stage='citation',available=True,state='completed_verified')
            except Exception:
                issues.append('REFERENCE_IDENTITY_UNRESOLVED')
                stages['citation']=StageRecord(stage='citation',available=True,state='failed',reasons=('REFERENCE_IDENTITY_UNRESOLVED',))
        else:
            issues.append('PREPARED_EXTRACTION_UNAVAILABLE')
        if proposals is not None:
            with telemetry.stage('coverage_proposal_verification'):
                expected=propose_concepts(source,inventory,limits=proposals.limits)
            if proposals!=expected:
                raise VerificationError('PROPOSAL_IDENTITY_INVALID')
            plan=expected
            stages['proposals']=StageRecord(stage='proposals',available=True,state='completed_verified')
        else:
            issues.append('PROPOSAL_STAGE_UNAVAILABLE')
        if plan is not None:
            context=None; value=None
            try:
                context=verify_context(generation)
            except VerificationError as exc:
                issues.append(exc.code)
            eligibility=assess_eligibility(source,plan,context.config if context else WorkflowConfig())
            if workflow is not None:
                try:
                    value=verify_workflow(workflow,plan,eligibility,reader,run_id)
                    if value.diagnostics.get('validation_policy')!=policy:
                        raise VerificationError('VALIDATION_POLICY_STALE')
                    result_verified=True
                except VerificationError as exc:
                    issues.append(exc.code)
            else:
                issues.append('WORKFLOW_STAGE_UNAVAILABLE')
            units={u.proposal_id:u for u in value.units} if result_verified else {}
            drafts={d.proposal_id:d for d in value.drafts} if result_verified else {}
            seen={}
            with StorageSnapshot(db_path,limits) as snapshot:
                with telemetry.stage('coverage_indexed_join'):
                    links=tuple(_proposal(e,p,source=source,plan=plan,extraction=extraction,reference=reference,
                        reader=reader,snapshot=snapshot,context=context,policy=policy,unit=units.get(p.proposal_id),
                        draft=drafts.get(p.proposal_id),workflow_verified=result_verified,seen=seen)
                        for e,p in zip(eligibility,plan.proposals))
                storage_evidence=snapshot.evidence
            counts.update(proposals=len(plan.proposals),nonduplicate_proposal_units=sum(not p.duplicate_of for p in links),
                unique_owned_regions=len({rid for p in plan.proposals for rid in p.source_region_ids}),
                owned_characters_union=union_characters([s for p in plan.proposals for s in p.ownership]),
                context_characters_union=union_characters([c.evidence for p in plan.proposals for c in p.supporting_context]),
                context_link_characters=sum(c.evidence.char_end-c.evidence.char_start for p in plan.proposals for c in p.supporting_context),
                ownership_and_context_characters_union=union_characters([s for p in plan.proposals for s in
                    p.ownership+tuple(c.evidence for c in p.supporting_context)]),
                known_generated_candidates=sum(p.generated_candidate_count or 0 for p in links),
                generated_candidates=sum(p.generated_candidate_count for p in links) if result_verified and all(
                    p.generated_candidate_count is not None for p in links) else None,
                verified_response_units=sum(p.checkpoint=='response_verified' for p in links),
                known_eligible_candidate_events=sum(p.validation=='review_required' for p in links),
                unique_reviewable_drafts=sum(p.generation=='draft_generated_review_eligible' and not p.verification_issues for p in links),
                rejected_units=sum(p.generation=='generated_rejected' for p in links),
                held_units=sum(p.generation in {'held_before_generation','generated_source_hold'} for p in links),
                deferred_units=sum(p.generation in {'duplicate_deferred','request_budget_deferred','service_stop_deferred'} for p in links),
                unknown_generation_units=sum(p.generation in {'interrupted_or_unknown','verification_incomplete','not_started','provider_failure'} for p in links),
                resumed_units=sum(p.resumed_from_checkpoint is True for p in links),
                verified_stored_review_drafts=len({p.chunk_id for p in links if p.persistence=='verified_local_review'}),
                revoked_storage_units=sum(p.persistence=='eligibility_revoked' for p in links),
                unknown_storage_units=sum(p.persistence in {'reconciliation_required','readback_incomplete'} for p in links))
            for stage in ('generation','validation','storage'):
                incomplete=(not result_verified or any(p.verification_issues for p in links)
                    or (stage=='generation' and counts['unknown_generation_units']>0)
                    or (stage=='validation' and any(p.validation=='incomplete' for p in links))
                    or (stage=='storage' and counts['unknown_storage_units']>0))
                stages[stage]=StageRecord(stage=stage,available=result_verified,
                    state='failed' if stage=='generation' and any(p.generation=='provider_failure' for p in links)
                        else 'incomplete' if incomplete else 'completed_verified')
            if result_verified and stages['generation'].state=='completed_verified' and all(
                    p.checkpoint in {'not_scheduled','no_request_record'} for p in links):
                stages['generation']=StageRecord(stage='generation',available=True,state='not_started',reasons=('NO_COMMITTED_REQUESTS_OBSERVED',))
            if result_verified and all(p.validation=='not_started' for p in links):
                stages['validation']=StageRecord(stage='validation',available=True,state='not_started')
            if result_verified and all(p.persistence=='not_stored' and not p.verification_issues for p in links):
                stages['storage']=StageRecord(stage='storage',available=True,state='not_started')
            if value is not None and value.status=='INTERRUPTED':
                stages['generation']=StageRecord(stage='generation',available=True,state='incomplete',reasons=('INTERRUPTED_WORKFLOW_RESULT',))
        regions=_regions(inventory,plan,links,source)
    except VerificationError as exc:
        issues.append(exc.code)
    except Exception as exc:
        # Never render arbitrary error contents, field names or source text.
        code=getattr(exc,'code',None)
        issues.append(code if code in {'SOURCE_IDENTITY_INVALID','PROPOSAL_IDENTITY_INVALID','INPUT_BUDGET_EXCEEDED'}
            else 'INPUT_VERIFICATION_INCOMPLETE')
    if identity and not regions:
        regions=_regions(inventory,plan,links,source)
    for r in regions:
        if r.span:
            chars[r.source_state]=chars.get(r.source_state,0)+r.span.char_end-r.span.char_start
    if identity and sum(chars.values())!=counts['canonical_characters']:
        issues.append('CANONICAL_CHARACTER_ACCOUNTING_MISMATCH')
    for o in safe_observations:
        if stages[o.stage].state=='completed_verified':
            issues.append('STAGE_OBSERVATION_CONFLICT')
        stages[o.stage]=StageRecord(stage=o.stage,state=o.state,available=False,reasons=(o.code,'CALLER_FAILURE_OBSERVATION'))
    stages['clinical_review']=StageRecord(stage='clinical_review',state='not_started',available=False,
        reasons=('NO_VERIFIED_AUTHENTICATED_CLINICAL_APPROVAL_AUTHORITY',))
    if not reader.stable():
        issues.append('ARTIFACT_CHANGED_DURING_RECONCILIATION')
        links=tuple(p.model_copy(update={'persistence':'reconciliation_required','chunk_id':None,
            'verification_issues':tuple(sorted(set(p.verification_issues+('ARTIFACT_CHANGED_DURING_RECONCILIATION',))))}) for p in links)
        counts['verified_stored_review_drafts']=0 if links else None
        if identity:
            regions=_regions(inventory,plan,links,source)
    issues=sorted(set(issues))
    tasks=_tasks(identity,regions,links,issues)
    technical=bool(identity is not None and plan is not None and result_verified and not issues
        and not any(p.verification_issues for p in links) and counts['unknown_generation_units']==0
        and counts['unknown_storage_units']==0 and stages['citation'].state=='completed_verified'
        and all(stages[s].state in {'completed_verified','not_started'} for s in ('generation','validation','storage')))
    artifact_hash=digest({'files':{str(p.relative_to(reader.root)):h for p,h in reader.watched.items()},
        'storage':storage_evidence,'storage_available':db_path is not None})
    implementation=implementation_identity()
    payload=dict(version='coverage-reconciliation-v1',implementation_sha256=implementation,source=identity,
        proposal_plan_id=plan.plan_id if plan else None,validation_policy=policy,input_state_sha256=state_hash,
        artifact_state_sha256=artifact_hash,stages=tuple(stages[s] for s in STAGES),regions=regions,proposals=links,
        review_tasks=tasks,counts=counts,source_characters_by_state=chars,
        review_tasks_by_reason=dict(Counter(t.reason for t in tasks)),verification_issues=tuple(issues),
        technical_reconciliation_complete=technical)
    report=CoverageReport(reconciliation_id='pending',**payload)
    return report.model_copy(update={'reconciliation_id':'coverage:'+digest(report.model_dump(
        mode='json',exclude={'reconciliation_id'}))})

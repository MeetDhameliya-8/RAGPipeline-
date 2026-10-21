"""Source-bounded requests and separate generation/validation identities."""
from __future__ import annotations

from pathlib import Path
import json
from src.evidence import text_hash
from src.editorial_generation import EDITORIAL_PROMPT
from clinical_proposals.source_adapter import digest
from .models import VERSION, ProposalDraftEligibility, WorkflowError


def provider_identity(provider) -> str:
    from src.providers.azure import AzureChunkGenerator
    if isinstance(provider, AzureChunkGenerator):
        config = {k:getattr(provider,k) for k in ('deployment','endpoint','api_version','request_timeout_seconds','max_retries')}
        config['injected_client'] = provider.client_factory is not None
        config['implementation'] = text_hash(Path(__import__('src.providers.azure',fromlist=['x']).__file__).read_text(encoding='utf-8'))
    else:
        config = getattr(provider, 'generation_identity', None)
        if not isinstance(config, dict) or not config:
            raise WorkflowError('CONFIGURATION_ERROR')
        if any(not isinstance(k,str) or type(v) not in (str,int,float,bool,type(None)) for k,v in config.items()):
            raise WorkflowError('CONFIGURATION_ERROR')
    try:
        return digest({'class':type(provider).__module__+'.'+type(provider).__qualname__,'config':config})
    except (TypeError,ValueError):
        raise WorkflowError('CONFIGURATION_ERROR') from None


def generation_contract() -> str:
    root = Path(__file__).parent
    # Validation-only revisions intentionally do not invalidate raw responses.
    return digest({'version':VERSION, 'implementation':{name:text_hash((root/name).read_text(encoding='utf-8'))
        for name in ('requests.py','eligibility.py','models.py','workflow.py')}})


def validation_policy() -> str:
    from src.package_generation import validation_fingerprint
    return digest({'source_policy':validation_fingerprint(), 'adapter':text_hash(
        Path(__file__).with_name('validation.py').read_text(encoding='utf-8'))})


def build_request(source, proposals, eligibility: ProposalDraftEligibility, reference: str,
                  provider_hash: str, prompt: str) -> tuple[str,str,str]:
    alias = 'source-'+source.evidence.pdf_sha256[:20]+'.pdf'
    sections = []
    for label, spans in (('OWNED EVIDENCE',eligibility.ownership), ('UNBOUND SUPPORTING CONTEXT',eligibility.context)):
        for s in spans:
            value = source.evidence.buffers[s.buffer_id].text[s.char_start:s.char_end]
            sections.append(f'[{label}; buffer={s.buffer_id}; start={s.char_start}; end={s.char_end}]\n'+value)
    labels = [{'role':r.role,'evidence':r.evidence.model_dump(), 'basis':r.basis} for r in eligibility.scope]
    proposal = next(p for p in proposals.proposals if p.proposal_id == eligibility.proposal_id)
    header = {'input_contract':VERSION,'human_source_reviewed':False,'clinically_approved':False,
        'proposed_question':eligibility.clinical_question, 'document_category':eligibility.document_category,
        'proposal_id':eligibility.proposal_id, 'source_order':eligibility.source_order, 'evidence_fingerprint':proposals.evidence_fingerprint,
        'verified_reference':reference,'source_file':alias,'literal_scope_candidates':labels,
        'page_numbers':proposal.page_numbers,'section_ids':proposal.section_ids,'category':proposal.category,
        'candidate_topic':proposal.candidate_topic.model_dump() if proposal.candidate_topic else None,
        'candidate_subtopic':proposal.candidate_subtopic.model_dump() if proposal.candidate_subtopic else None,
        'source_concern_ids':proposal.concern_ids,'unresolved_relationships':proposal.unresolved_relationships,
        'limitations':['Automatic proposal, not a reviewed clinical relationship.',
            'Use only the supplied complete evidence; retain population, qualifications and uncertainty.',
            'Return a mandatory-review draft or the existing source-abstention envelope.']}
    request = json.dumps(header,sort_keys=True)+'\n\n'+'\n\n'.join(sections)
    key = digest({'contract':generation_contract(), 'source':proposals.evidence_fingerprint,
        'pdf':source.evidence.pdf_sha256,'proposal_plan':proposals.plan_id,'proposal':eligibility.proposal_id,
        'selection':eligibility.model_dump(mode='json'),'reference':reference,'request':text_hash(request),
        'prompt':text_hash(prompt),'provider':provider_hash})
    return request, alias, key


def editorial_prompt() -> str:
    return EDITORIAL_PROMPT.read_text(encoding='utf-8')

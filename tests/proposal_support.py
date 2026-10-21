"""Fictional source fixtures only; no human-review attestation or PDF reads."""
from src.document_concepts import build_document_concept_plan
from src.source_map import build_source_map
from test_concept_planning import source

TRIAL = ('In a randomized trial, adults with condition delta received treatment alpha or placebo. '
         'The primary outcome was remission at 8 weeks. The secondary outcome was response at 4 weeks.')
PRIMARY = ('For adults with condition delta receiving treatment alpha or placebo, the primary outcome '
           'remission at 8 weeks was 40% versus 38%; the difference was not statistically significant.')
SECONDARY = ('For adults with condition delta receiving treatment alpha or placebo, the secondary outcome '
             'response at 4 weeks was 10 points versus 9 points.')
GUIDELINE = ('For adults with condition delta, the panel suggests treatment alpha only after shared discussion. '
             'The recommendation is conditional, with low certainty of evidence, excluding children.')
SAFETY = ('Adverse events occurred in 20% with treatment alpha and 10% with placebo. '
          'However, follow-up was limited to 2 weeks; delayed harms were not measured.')

CASES = {
    'two_arm_trial': ['Methods\n\n'+TRIAL+'\n\nResults\n\n'+PRIMARY+'\n\n'+SECONDARY],
    'negative_result': ['Results\n\n'+PRIMARY],
    'conditional_low_certainty': ['Recommendations\n\n'+GUIDELINE],
    'distinct_populations': ['Methods\n\nAdults with condition delta received treatment alpha.\n\n'
        'Children with condition epsilon received treatment beta.'],
    'adverse_events': ['Results\n\n'+SAFETY],
    'procedure': ['Methods\n\nProcedure selection required eligibility assessment before procedure lambda. '
        'Contraindications to procedure lambda included condition epsilon.'],
    'diagnostic': ['Methods\n\nThe diagnostic test was assessed in adults with condition delta. '
        'Diagnostic selection criteria were restricted to the study population.'],
    'explicit_cross_page': ['Methods\n\n'+TRIAL, 'Discussion\n\nA different population received treatment beta.',
        'Results\n\n'+PRIMARY+' Population definitions are described in Methods.'],
    'unproven_cross_page': ['Methods\n\n'+TRIAL, 'Results\n\nThese patients had a response at 8 weeks.'],
    'adjacent_qualification': ['Methods\n\n'+TRIAL+'\n\nResults\n\n'+PRIMARY+'\n\n'
        'However, delayed adverse events were not assessed; applicability outside the study center was uncertain.'],
    'table': ['Results\n\nPatients had a response as shown in Table 1 and Figure 2.\n\n'
        'Table 1. Treatment alpha 40% placebo 38%.'],
    'ambiguous_numbers': ['Results\n\nAdults received treatment alpha or placebo. '
        'The outcomes were remission and response. The recorded values were 40%, 38%, 10% and 9%.'],
    'duplicate': ['Methods\n\nAdults received treatment alpha.\n\nAdults received treatment alpha.'],
    'cited_study': ['Discussion\n\nA previous study reported remission in adults receiving treatment alpha.'],
    'references': ['Methods\n\nAdults received treatment alpha.\n\nReferences\n\n'
        'A previous study reported treatment alpha for children with condition epsilon.'],
    'empty': [''],
    'unclassified': ['Introduction\n\nThe report provides background material.'],
    'single_design': ['Methods\n\nThis was a randomized trial.'],
    'damaged_passage': ['Results\n\nAdults received treatment alpha; the outcome was \ufffd and cannot be read.'],
    'orphan_numbers': ['Results\n\n40% 38% at 8 weeks.'],
}


def fixture(pages, *, name='synthetic-proposals.pdf'):
    extraction = source(pages)
    extraction['file_name'] = name
    mapped = build_source_map(extraction)
    inventory = build_document_concept_plan(extraction, mapped)
    return extraction, mapped, inventory


def text_for(mapped, item):
    return mapped.evidence.buffers[item.buffer_id].text[item.char_start:item.char_end]

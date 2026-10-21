"""Page recovery decisions from extraction quality, not chunk eligibility."""
from __future__ import annotations

from dataclasses import dataclass
import re

from .evidence import EvidenceDocument
from .source_packages import DAMAGE, CLINICAL, _words


@dataclass(frozen=True)
class PageRecoveryDecision:
    page_number: int
    image_reading: bool
    reason: str


def recovery_decisions(doc: EvidenceDocument, native_pages: set[int], holds=()) -> list[PageRecoveryDecision]:
    decisions=[]
    held_by_page={}
    for hold in holds:held_by_page.setdefault(hold.page_number,[]).append(hold)
    for page in doc.pages:
        if page.page_number in native_pages:
            decisions.append(PageRecoveryDecision(page.page_number,False,'witnessed_native_packages'))
            continue
        text=doc.buffers[page.selected_buffer_id].text
        words=re.findall(r'\b\w+\b',text)
        has_visual=any(b.role in {'figure','visual_region'} for b in page.blocks)
        page_holds=held_by_page.get(page.page_number,[])
        reasons={h.reason for h in page_holds}
        unresolved_prose=any(h.reason=='native_extraction_support_unresolved' and h.source_span and
            _words(doc.buffers[h.source_span.buffer_id].text[h.source_span.char_start:h.source_span.char_end])>=20 and
            CLINICAL.search(doc.buffers[h.source_span.buffer_id].text[h.source_span.char_start:h.source_span.char_end]) for h in page_holds)
        if 'reference_list' in reasons and reasons <= {'reference_list','source_heading_or_label','nonclinical_boilerplate'}:
            image=False;reason='reference_list_retained_for_review'
        elif not words:
            image=has_visual
            reason='image_or_vector_text_without_native_text' if image else 'empty_page_no_visual_source'
        elif DAMAGE.search(text):
            image=True;reason='damaged_native_encoding'
        elif len(words)<40 and has_visual:
            image=True;reason='sparse_native_text_with_visual_source'
        elif unresolved_prose:
            image=True;reason='native_prose_corroboration_unresolved'
        elif 'insufficient_coherent_source_length' in reasons and len(words)>=90 and CLINICAL.search(text):
            image=True;reason='native_paragraph_order_unresolved'
        else:
            image=False;reason='readable_native_source_requires_packaging_review'
        decisions.append(PageRecoveryDecision(page.page_number,image,reason))
    return decisions

"""Narrow native corroboration for drafting, never clinical approval or coverage."""
from __future__ import annotations

import re
import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .evidence import EvidenceDocument, EvidenceSpan, EvidenceTrace, EvidenceWarning, span


class NativeLineSupport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    native_span: EvidenceSpan
    candidate_spans: list[EvidenceSpan]


class NativeSupportProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    version: Literal["native-local-support-v1", "native-block-lines-v2"] = "native-local-support-v1"
    canonical_span: EvidenceSpan
    page_number: int
    block_id: str
    bbox: tuple[float, float, float, float]
    native_block_span: EvidenceSpan
    candidate_spans: list[EvidenceSpan]
    scope_basis: Literal["literal_population_intervention", "explicit_trial_design"]
    line_support: list[NativeLineSupport] = Field(default_factory=list)
    original_view_spans: list[EvidenceSpan] = Field(default_factory=list)


def overlaps(a: EvidenceSpan, b: EvidenceSpan) -> bool:
    return a.buffer_id == b.buffer_id and a.char_start < b.char_end and b.char_start < a.char_end


def boxes_overlap(a, b) -> bool:
    # Touching boundaries are held too: geometry rounding is not independence.
    return max(a[0], b[0]) <= min(a[2], b[2]) and max(a[1], b[1]) <= min(a[3], b[3])


class NativeSupportIndex:
    def __init__(self, evidence: EvidenceDocument):
        self.doc = evidence
        self.normalized = {}

    def _normalized(self, buffer_id):
        if buffer_id not in self.normalized:
            raw = self.doc.buffers[buffer_id].text
            # Offsets retain every original character. Only whitespace collapses.
            pieces, starts, ends = [], [], []
            for m in re.finditer(r"\S+|\s+", raw):
                if m.group().isspace():
                    pieces.append(" "); starts.append(m.start()); ends.append(m.end())
                else:
                    pieces.append(m.group())
                    starts.extend(range(m.start(), m.end())); ends.extend(range(m.start()+1, m.end()+1))
            self.normalized[buffer_id] = ("".join(pieces), starts, ends)
        return self.normalized[buffer_id]

    def match(self, buffer_id: str, value: str):
        normalized, starts, ends = self._normalized(buffer_id)
        a = normalized.find(value)
        if a < 0 or normalized.find(value, a+1) >= 0:
            return None
        b = a + len(value)
        if (a and normalized[a-1].isalnum()) or (b < len(normalized) and normalized[b].isalnum()):
            return None
        return span(buffer_id, starts[a], ends[b-1]), normalized[:a], normalized[b:]

    def _line_support(self, page, block, location):
        active = [v for v in self.doc.native_layout_views if v.alternate_canonical_id == self.doc.canonical_buffer_id]
        routes = {r.route_id:r for r in self.doc.processing_routes}
        if not active or not any(block.block_id in routes[rid].block_ids for v in active for rid in v.route_ids):
            return []
        lines = [line for line in block.lines if overlaps(line.span, location)]
        if not lines:return []
        result = [];last_y = -float("inf");previous = {}
        for line in lines:
            if not all(math.isfinite(v) for v in line.bbox) or line.bbox[0]>=line.bbox[2] or line.bbox[1]>=line.bbox[3]:return []
            if line.direction != (1., 0.) or line.bbox[1] <= last_y or any(not (block.bbox[i]-.1 <= line.bbox[i] <= block.bbox[i+2]+.1) for i in (0,1)):
                return []
            if line.bbox[2] > block.bbox[2]+.1 or line.bbox[3] > block.bbox[3]+.1:return []
            last_y = line.bbox[1]
            value = " ".join(self.doc.buffers[line.span.buffer_id].text[line.span.char_start:line.span.char_end].split())
            if not value:return []
            candidates = {}
            for c in page.candidates:
                if c.extractor not in {"pypdf", "pymupdf"}:continue
                found = self.match(c.buffer_id, value)
                if found is not None:candidates.setdefault(c.extractor, found[0])
            if set(candidates) != {"pypdf", "pymupdf"}:return []
            for kind,s in candidates.items():
                if s.char_start < previous.get(kind, -1):return []
                previous[kind] = s.char_end
            result.append(NativeLineSupport(native_span=line.span, candidate_spans=[candidates[k] for k in sorted(candidates)]))
        # The native lines must account for all non-whitespace owned characters.
        raw = self.doc.buffers[location.buffer_id].text
        covered = set(i for line in lines for i in range(max(location.char_start,line.span.char_start), min(location.char_end,line.span.char_end)))
        if any(not raw[i].isspace() and i not in covered for i in range(location.char_start,location.char_end)):return []
        return result

    def prove(self, owned: EvidenceSpan, trace: EvidenceTrace) -> NativeSupportProof | None:
        doc = self.doc
        if doc.identity_quality != "pdf_bytes" or not trace.fragments or trace.unmapped_spans:
            return None
        if any(doc.buffers[s.buffer_id].text[s.char_start:s.char_end].strip() for s in trace.synthetic_spans):
            return None
        if any(f.precision != "exact" and doc.buffers[f.source.buffer_id].text[f.source.char_start:f.source.char_end].strip() for f in trace.fragments):
            return None
        pages = {doc.buffers[f.source.buffer_id].page_number for f in trace.fragments if doc.buffers[f.source.buffer_id].text[f.source.char_start:f.source.char_end].strip()}
        if len(pages) != 1 or None in pages:
            return None
        value = " ".join(doc.buffers[owned.buffer_id].text[owned.char_start:owned.char_end].split())
        if re.search(r"\(cid:\d+\)|/(?:H\d{4,}|C\d{2,})|[\x00-\x08\x0b\x0c\x0e-\x1f\ufffd\u00fe]", value, re.I):
            return None
        if not re.match(r"[A-Z]", value) or not re.search(r"[.!?]$", value):
            return None
        # No missing referents, inferred arms, educational boilerplate, footnotes,
        # contrasts or visual relationships are licensed by native agreement.
        if re.search(r"\b(table|figure|fig\.|these|those|this|they|their|respectively|group|arm|former|latter|above|below|however|although|except|unless)\b|learning objective|\bfirst[, ]|[*†‡]|[\x00-\x08\ufffd]", value, re.I):
            return None
        if re.search(r"\b(?:adults?|patients?|children|participants?)\b.+\b(?:received|were treated with|were assigned to|were randomized to)\b", value, re.I):
            basis = "literal_population_intervention"
        elif re.search(r"\b(?:trial|study) was (?:carried out|conducted|performed) (?:at|in)\b", value, re.I):
            basis = "explicit_trial_design"
        else:
            return None
        page = next((p for p in doc.pages if p.page_number in pages), None)
        if page is None or page.rotation not in {None, 0}:
            return None
        blocks = []
        for block in page.blocks:
            if block.role != "text" or not block.span or not block.bbox or block.direction is None:
                continue
            if not all(math.isfinite(v) for v in block.bbox):continue
            if abs(block.direction[0]-1) > .01 or abs(block.direction[1]) > .01:
                continue
            match = self.match(block.span.buffer_id, value)
            if match is None:
                continue
            location, prefix, suffix = match
            if location.char_start < block.span.char_start or location.char_end > block.span.char_end:
                continue
            # Start at the native block boundary or a complete native sentence.
            if prefix.strip() and not re.search(r"[.!?]\s*$", prefix):
                continue
            if re.match(r"\s*(?:however|although|except|unless|only|but|provided|excluding|these|those|this|they|their)\b", suffix, re.I):
                continue
            blocks.append((block, location))
        if len(blocks) != 1:
            return None
        candidates = {}
        for candidate in page.candidates:
            if candidate.extractor not in {"pypdf", "pymupdf"}:
                continue
            match = self.match(candidate.buffer_id, value)
            if match is not None:
                candidates.setdefault(candidate.extractor, match[0])
        block, location = blocks[0]
        if any(b.block_id != block.block_id and b.span and b.bbox and boxes_overlap(block.bbox, b.bbox) for b in page.blocks):
            return None
        line_support = []
        if set(candidates) != {"pypdf", "pymupdf"}:
            line_support = self._line_support(page, block, location)
            if not line_support:return None
        candidate_spans = [s for line in line_support for s in line.candidate_spans] if line_support else [candidates[k] for k in sorted(candidates)]
        originals = []
        for view in doc.native_layout_views:
            found = self.match(view.original_canonical_id, value)
            if found:
                originals.append(found[0])
            elif line_support:
                matches = [self.match(view.original_canonical_id, " ".join(doc.buffers[line.native_span.buffer_id].text[line.native_span.char_start:line.native_span.char_end].split())) for line in line_support]
                if all(matches):originals.extend(m[0] for m in matches)
        return NativeSupportProof(canonical_span=owned, page_number=page.page_number, block_id=block.block_id,
            bbox=block.bbox, native_block_span=location, candidate_spans=candidate_spans, scope_basis=basis,
            version="native-block-lines-v2" if line_support else "native-local-support-v1", line_support=line_support, original_view_spans=originals)

    def warning_independent(self, proof: NativeSupportProof, warning: EvidenceWarning, warning_trace: EvidenceTrace) -> bool:
        pages = {warning.page_number} if warning.page_number else {
            self.doc.buffers[f.source.buffer_id].page_number for f in warning_trace.fragments}
        if pages and pages != {proof.page_number}:
            return False
        supported = proof.candidate_spans + [proof.native_block_span, proof.canonical_span] + proof.original_view_spans
        if warning.span and (any(overlaps(warning.span, s) for s in supported) or
                any(overlaps(f.source, s) for f in warning_trace.fragments for s in supported)):
            return False
        if not pages:
            # An exact canonical location outside the self-contained statement
            # is different from an unlocalized missing clinical dependency.
            return bool(warning.code == "text_transformation_unreviewed" and warning.span and
                warning.span.buffer_id in {proof.canonical_span.buffer_id, *(s.buffer_id for s in proof.original_view_spans)} and
                warning.span.char_end > warning.span.char_start)
        if warning.code in {"reading_order_unverified", "extractor_disagreement"}:
            return True  # Locally witnessed single-block order in both candidates.
        if warning.code == "visual_region_unclassified":
            return warning.bbox is not None and not boxes_overlap(proof.bbox, warning.bbox)
        if warning.code == "existing_extraction_blocker" and re.fullmatch(
                rf"Page {proof.page_number}: sparse text with visual content requires OCR/layout review\.", warning.message):
            page = next(p for p in self.doc.pages if p.page_number == proof.page_number)
            regions = [b for b in page.blocks if b.role in {"visual_region", "figure"}]
            return bool(regions) and all(b.bbox and not boxes_overlap(proof.bbox, b.bbox) for b in regions)
        if warning.code in {"text_transformation_unreviewed", "encoding_sign_unit_unresolved"}:
            return bool(warning.span and warning_trace.fragments and not warning_trace.unmapped_spans)
        if warning.code == "figure_evidence_unresolved":
            # Never infer a clinical figure/table relationship from a drawing.
            # A fully explicit text statement also needs a separate positive
            # native caption inventory, all spatially outside its text block.
            page = next(p for p in self.doc.pages if p.page_number == proof.page_number)
            regions = [b for b in page.blocks if b.role in {"figure", "figure_caption"}]
            return bool(regions) and all(b.bbox and not boxes_overlap(proof.bbox, b.bbox) for b in regions)
        return False

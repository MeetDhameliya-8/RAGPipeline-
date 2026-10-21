"""Conservative lexical numeric evidence; never reconstruct damaged clinical facts."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import re


TOKEN_RE = re.compile(r"(?<![\d.])(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?|\.\d+)%?")
UNIT_RE = re.compile(r"\s*(micrograms?|milligrams?|mg|[µμu]g|kg|g|ml|mL|mmol/L|mg/dL|mm|cm|weeks?|days?|months?|years?)\b", re.I)


@dataclass(frozen=True)
class NumericEvidence:
    value: Decimal
    lexical: str
    start: int
    end: int
    operator: str
    unit: str
    ambiguous: bool
    possible_integer: Decimal | None = None


def numeric_evidence(text: str) -> list[NumericEvidence]:
    values = []
    for match in TOKEN_RE.finditer(text):
        lexical = match.group()
        prefix = text[max(0, match.start() - 8):match.start()]
        operator_match = re.search(r"([+−\-<>≤≥±])\s*$", prefix)
        operator = operator_match.group(1).replace("−", "-") if operator_match else ""
        unit_match = UNIT_RE.match(text, match.end())
        unit = "%" if lexical.endswith("%") else (unit_match.group(1).casefold() if unit_match else "")
        unit = {"microgram": "ug", "micrograms": "ug", "µg": "ug", "μg": "ug",
                "milligram": "mg", "milligrams": "mg"}.get(unit, unit)
        ambiguous = bool(prefix and prefix[-1].isalpha()) or bool(re.search(r"/(?:H\d{4,}|C\d{2,})$", prefix))
        value = Decimal(lexical.rstrip("%").replace(",", ""))
        # A printed superscript may have become a decimal suffix. Keep this
        # only as an ambiguous alternative, never as a supported number.
        integer, dot, fraction = lexical.rstrip("%").partition(".")
        possible_integer = Decimal(integer.replace(",", "")) if dot and "," in integer and len(fraction) <= 3 else None
        values.append(NumericEvidence(value, lexical, match.start(), match.end(), operator, unit, ambiguous, possible_integer))
    return values


def local_numeric_findings(claim: str, passages: list[str], full_source: str) -> list[tuple[str, str, str]]:
    """Return ERROR for unsupported/conflicting evidence and WARNING for unknown relations."""
    findings: list[tuple[str, str, str]] = []
    source_tokens = numeric_evidence(full_source)
    local_tokens = [item for passage in passages for item in numeric_evidence(passage)]
    aligned_tokens = [item for passage in passages if _quantity_context(passage) == _quantity_context(claim)
                      for item in numeric_evidence(passage)]
    exact_claim = re.sub(r"\s+", " ", claim).strip().casefold() in re.sub(r"\s+", " ", full_source).casefold()
    for token in numeric_evidence(claim):
        matches = [item for item in local_tokens if item.value == token.value]
        artifacts = [item for item in local_tokens if item.possible_integer == token.value]
        if token.ambiguous or artifacts or any(item.ambiguous for item in matches):
            findings.append(("WARNING", "EVIDENCE_QUALITY", "Numeric glyph, sign or citation-marker meaning is unresolved in local evidence."))
        elif not matches:
            if any(item.value == token.value for item in source_tokens):
                findings.append(("WARNING", "NUMERIC_ASSOCIATION", "Numeric value occurs outside matching local evidence; endpoint, arm or timeframe is unresolved."))
            else:
                findings.append(("ERROR", "SOURCE_FIDELITY", f"Number not found in source evidence: {token.lexical}."))
        else:
            compatible = [item for item in matches
                          if (not token.operator or not item.operator or token.operator == item.operator)
                          and (not token.unit or not item.unit or token.unit == item.unit)]
            aligned_matches = [item for item in aligned_tokens if item.value == token.value]
            if not compatible and aligned_matches:
                findings.append(("ERROR", "NUMERIC_RELATION", "Numeric value has a conflicting explicit sign, inequality or unit in local evidence."))
            elif not compatible:
                findings.append(("WARNING", "NUMERIC_ASSOCIATION", "Different lexical units/signs occur in loosely matching evidence; the quantity context is unresolved."))
            elif not exact_claim:
                findings.append(("WARNING", "NUMERIC_ASSOCIATION", "Numeric presence does not verify endpoint, treatment-arm, comparator or timeframe association; review is required."))
            elif any(item.operator != token.operator or item.unit != token.unit for item in compatible):
                findings.append(("WARNING", "NUMERIC_ASSOCIATION", "Numeric operator or unit association requires review."))
    return list(dict.fromkeys(findings))


def _quantity_context(text: str) -> tuple[str, ...]:
    """Compare explicit propositions without asserting a semantic paraphrase."""
    spans = []
    for token in numeric_evidence(text):
        unit = UNIT_RE.match(text, token.end)
        spans.append((token.start, unit.end() if unit else token.end))
    for start, end in reversed(spans):
        text = text[:start] + " " + text[end:]
    return tuple(re.findall(r"[a-z]+", text.casefold()))

# P1-C1 minimal design, recorded before implementation

2026-10-16. Scope: offline evidence eligibility and mocked drafting only. Stop at the P1-C1 gate.

Reproduction: all 30 saved P1-B1 plans contain 6,627 blocked units and zero eligible units. In `build_clinical_plan`, a warning anywhere on a required page becomes `uncertain_dependency`; any such decision adds `unresolved_evidence_warning`. Context closure propagates that block. Native geometry and alternative candidates are retained but are not used to establish local support. Exact canonical offsets alone are inadequate: some exact native passages interleave columns.

An actual previously blocked study-design passage was checked against the rendered PDF page and both native candidates. Its named trial and study-site statement is complete in the left text column; an uninterpreted figure occupies the right column. Its eligibility is currently blocked solely by page warnings. Medical source text and rendered pages remain in local audit storage, outside documentation.

Small correction: add a reproducible native-support certificate for a narrowly self-contained passage. Require verified PDF identity, usable exact non-whitespace provenance, a complete scoped statement, unique whitespace-only agreement in both pypdf and PyMuPDF candidates, and one horizontal native text block. Preserve exact candidate/block spans. Do not repair hyphens, signs, units or missing words. Clinical table/figure references, referents, group ambiguity, damaged material and unresolved context remain blocking.

Only specifically explained, localizable page warnings may be independent of a certified passage. Visual regions must be spatially disjoint; unlocated critical/document warnings cannot be waived. Page reading-order disagreement can be locally overcome by a single block and cross-extractor sequence agreement. Located source corruption must be disjoint from the retained native support. Record independent warning IDs separately; never resolve/delete them or clear document coverage. Recompute certificates and warning decisions when validating persisted plans.

Keep existing generation transport, raw schema, citation binding, numerical/claim validation, five fields, word-count checks and human approval. A short actual passage may reach generation yet still fail existing draft word-count checks; do not pad or call it clinically validated. Synthetic coherent passages exercise the complete mocked validation path separately.

First regressions: unrelated same-page visual/order uncertainty permits certified text; overlapping visuals, missing geometry/corroboration, interleaving, local corruption, unresolved table/group/qualifier dependencies and unlocalized critical warnings do not. Verify certificates, storage compatibility, partial coverage, real PDF layouts and mocked generation with the actual validator. Audit all 30 PDFs against preserved original hashes and saved P1-B1 plans; no historical artifacts are overwritten.

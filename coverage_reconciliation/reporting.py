"""Explicit export to a NEW directory; no source, DB or checkpoint writes."""
from pathlib import Path
from .models import CoverageReport,VerificationError
from clinical_proposals.source_adapter import digest


def _checked(report: CoverageReport) -> CoverageReport:
    report=CoverageReport.model_validate(report.model_dump())
    if report.reconciliation_id!='coverage:'+digest(report.model_dump(mode='json',exclude={'reconciliation_id'})):
        raise VerificationError('REPORT_IDENTITY_INVALID')
    return report


def human_summary(report: CoverageReport) -> str:
    report=_checked(report)
    counts=report.counts
    show=lambda key:'unknown' if counts.get(key) is None else str(counts[key])
    return ('Coverage reconciliation '+report.reconciliation_id+'\n'
        'Scope: automatic draft traceability; clinical completeness is NOT established.\n'
        f"Source regions: {show('source_regions')}; proposals: {show('proposals')}.\n"
        f"Known generated candidates: {show('known_generated_candidates')}; total: {show('generated_candidates')}.\n"
        f"Unique reviewable drafts: {show('unique_reviewable_drafts')}; verified local review rows: {show('verified_stored_review_drafts')}.\n"
        f"Held/rejected/deferred units: {show('held_units')}/{show('rejected_units')}/{show('deferred_units')}.\n"
        f"Unknown generation/storage units: {show('unknown_generation_units')}/{show('unknown_storage_units')}.\n"
        f"Technical accounting complete: {report.technical_reconciliation_complete}; clinical approvals: 0.\n"
        f"Review tasks: {len(report.review_tasks)}; verification issue occurrences: {len(report.verification_issues)+sum(len(p.verification_issues) for p in report.proposals)}.\n"
        'Stored drafts require clinical review. No production ingestion is authorized.\n')


def write_report(report: CoverageReport,directory: Path) -> tuple[Path,Path]:
    report=_checked(report)
    directory=Path(directory).resolve()
    # mkdir without exist_ok refuses to overwrite an existing artifact folder.
    try:
        directory.mkdir(parents=True)
    except FileExistsError:
        raise VerificationError('EXPORT_DIRECTORY_EXISTS') from None
    machine=directory/'coverage.json'; summary=directory/'summary.txt'
    machine.write_text(report.model_dump_json(indent=2)+'\n',encoding='utf-8')
    summary.write_text(human_summary(report),encoding='utf-8')
    return machine,summary

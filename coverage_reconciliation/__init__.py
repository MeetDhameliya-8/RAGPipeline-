"""Opt-in read-only technical coverage reconciliation, never clinical approval."""
from .engine import reconcile_coverage
from .models import CoverageReport,GenerationContext,ReconciliationLimits,StageObservation
from .verification import capture_generation_context
from .reporting import human_summary,write_report

__all__=['reconcile_coverage','CoverageReport','GenerationContext','ReconciliationLimits',
    'StageObservation','capture_generation_context','human_summary','write_report']

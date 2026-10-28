"""Tests for report reconciliation: degradation evidence and the degradation rule."""

import pytest

from resilient_triage.graph.reconciliation import DegradationEvidence, reconcile
from resilient_triage.schemas.incident import (
    DegradationStatus,
    IncidentTriageReport,
    SeverityLevel,
)


def _report(
    status: DegradationStatus = DegradationStatus.HEALTHY,
    tripped: list[str] | None = None,
) -> IncidentTriageReport:
    return IncidentTriageReport(
        summary="Payment API elevated errors",
        severity=SeverityLevel.HIGH,
        root_cause_analysis="Connection pool exhaustion",
        degradation_status=status,
        circuit_breakers_tripped=tripped or [],
    )


def _evidence(
    tripped: set[str] | None = None,
    fell_back: bool = False,
    unavailable: set[str] | None = None,
) -> DegradationEvidence:
    return DegradationEvidence(
        tripped_breakers=frozenset(tripped or set()),
        unavailable_sources=frozenset(unavailable or set()),
        synthesis_fell_back=fell_back,
    )


def test_synthesis_fallback_is_severely_degraded():
    """A synthesis fallback with no tripped breakers is SEVERELY_DEGRADED."""
    result = reconcile(_report(), _evidence(fell_back=True))
    assert result.degradation_status == DegradationStatus.SEVERELY_DEGRADED


@pytest.mark.parametrize(
    ("tripped", "fell_back", "expected"),
    [
        (set(), False, DegradationStatus.PARTIALLY_DEGRADED),
        ({"statuspage"}, False, DegradationStatus.PARTIALLY_DEGRADED),
        (set(), True, DegradationStatus.SEVERELY_DEGRADED),
        ({"statuspage", "redis"}, False, DegradationStatus.SEVERELY_DEGRADED),
    ],
    ids=["alone", "plus-one-breaker", "plus-synthesis-fallback", "plus-two-breakers"],
)
def test_unavailable_source_degrades_combined_by_maximum(
    tripped: set[str], fell_back: bool, expected: DegradationStatus
):
    """An unavailable source gives at least PARTIALLY_DEGRADED, combined with the other rules."""
    evidence = _evidence(tripped=tripped, fell_back=fell_back, unavailable={"chaos"})
    result = reconcile(_report(), evidence)
    assert result.degradation_status == expected
    assert result.circuit_breakers_tripped == sorted(tripped)


def test_maximum_of_rules_is_taken():
    """Synthesis fallback with one tripped breaker stays SEVERELY_DEGRADED, not PARTIALLY."""
    result = reconcile(_report(), _evidence(tripped={"chaos"}, fell_back=True))
    assert result.degradation_status == DegradationStatus.SEVERELY_DEGRADED
    assert result.circuit_breakers_tripped == ["chaos"]


def test_llm_healthy_is_raised_by_evidence():
    """A report the LLM marked HEALTHY is raised when a breaker tripped."""
    result = reconcile(_report(DegradationStatus.HEALTHY), _evidence(tripped={"statuspage"}))
    assert result.degradation_status == DegradationStatus.PARTIALLY_DEGRADED


def test_llm_degradation_claims_without_evidence_are_discarded():
    """The LLM's degradation status and breaker names are replaced by the evidence."""
    report = _report(DegradationStatus.SEVERELY_DEGRADED, tripped=["redis"])
    result = reconcile(report, _evidence())
    assert result.degradation_status == DegradationStatus.HEALTHY
    assert result.circuit_breakers_tripped == []


def test_unknown_is_never_produced():
    """An LLM-supplied UNKNOWN status is replaced by a computed status."""
    result = reconcile(_report(DegradationStatus.UNKNOWN), _evidence())
    assert result.degradation_status == DegradationStatus.HEALTHY


def test_tripped_breakers_are_sorted():
    """circuit_breakers_tripped is the sorted evidence set."""
    result = reconcile(_report(), _evidence(tripped={"statuspage", "chaos"}))
    assert result.circuit_breakers_tripped == ["chaos", "statuspage"]
    assert result.degradation_status == DegradationStatus.SEVERELY_DEGRADED


def test_input_report_is_not_mutated():
    """reconcile returns a new report and leaves its input untouched."""
    report = _report(DegradationStatus.HEALTHY, tripped=["redis"])
    before = report.model_dump()
    result = reconcile(report, _evidence(tripped={"chaos"}, fell_back=True))
    assert result is not report
    assert report.model_dump() == before


def test_other_fields_are_untouched():
    """reconcile changes only the degradation fields."""
    report = _report()
    result = reconcile(report, _evidence(tripped={"chaos"}))
    unchanged = {"circuit_breakers_tripped", "degradation_status"}
    assert result.model_dump(exclude=unchanged) == report.model_dump(exclude=unchanged)

"""Tests for Pydantic schemas and validation constraints."""

from datetime import datetime, timezone
import pytest
from pydantic import ValidationError

from resilient_triage.schemas.api import TriageRequest, TriageResponse
from resilient_triage.schemas.incident import (
    ActionRecommendation,
    DegradationStatus,
    IncidentTriageReport,
    PriorityLevel,
    ServiceImpact,
    SeverityLevel,
    TelemetrySignal,
)
from resilient_triage.schemas.state import TriageState
from resilient_triage.schemas.telemetry import (
    ChaosConfig,
    ChaosTelemetryResponse,
    StatuspageComponent,
    StatuspageIncident,
    StatuspageSummary,
)


def test_valid_incident_triage_report():
    """Verify that a well-formed dictionary parses strictly into IncidentTriageReport."""
    data = {
        "incident_id": "test-uuid-1234",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "summary": "GitHub Actions is failing across all workflow runs.",
        "severity": "CRITICAL",
        "root_cause_analysis": "Upstream webhook processing failure detected in external statuspage.",
        "affected_services": [
            {
                "service_name": "Actions Runner",
                "impact_level": "Major Outage",
                "details": "Queued jobs not picking up runners.",
            }
        ],
        "telemetry_signals": [
            {
                "source": "statuspage",
                "status": "major_outage",
                "latency_ms": 420.5,
                "error_rate": 0.85,
                "raw_snippet": "GitHub Actions service is reporting major outage.",
            }
        ],
        "recommended_actions": [
            {
                "priority": "P0",
                "action": "Pause scheduled CI jobs and notify on-call engineering.",
                "target_system": "CI Pipeline",
            }
        ],
        "degradation_status": "HEALTHY",
        "circuit_breakers_tripped": [],
        "confidence_score": 0.95,
    }

    report = IncidentTriageReport.model_validate(data)
    assert report.incident_id == "test-uuid-1234"
    assert report.severity == SeverityLevel.CRITICAL
    assert report.degradation_status == DegradationStatus.HEALTHY
    assert len(report.affected_services) == 1
    assert report.affected_services[0].service_name == "Actions Runner"
    assert report.confidence_score == 0.95


def test_invalid_severity_enum_raises_validation_error():
    """Verify that bad enum values (e.g. from LLM hallucination) trigger ValidationError."""
    data = {
        "summary": "Outage detected",
        "severity": "CATASTROPHIC",  # Bad enum
        "root_cause_analysis": "Unknown root cause",
        "confidence_score": 0.8,
    }

    with pytest.raises(ValidationError) as exc_info:
        IncidentTriageReport.model_validate(data)

    errors = exc_info.value.errors()
    assert any(err["loc"] == ("severity",) for err in errors)


def test_confidence_score_bounds():
    """Verify confidence score must be between 0.0 and 1.0."""
    data = {
        "summary": "Outage detected",
        "severity": "HIGH",
        "root_cause_analysis": "Database timeout",
        "confidence_score": 1.5,  # Exceeds max 1.0
    }

    with pytest.raises(ValidationError) as exc_info:
        IncidentTriageReport.model_validate(data)

    errors = exc_info.value.errors()
    assert any(err["loc"] == ("confidence_score",) for err in errors)


def test_extra_field_forbidden():
    """Verify unexpected fields are rejected strictly."""
    data = {
        "summary": "Outage detected",
        "severity": "LOW",
        "root_cause_analysis": "Minor blip",
        "confidence_score": 0.5,
        "hallucinated_field": "This should fail validation",
    }

    with pytest.raises(ValidationError) as exc_info:
        IncidentTriageReport.model_validate(data)

    errors = exc_info.value.errors()
    assert any("hallucinated_field" in str(err) for err in errors)


def test_statuspage_summary_parsing():
    """Verify Statuspage JSON structure parsing."""
    raw_statuspage = {
        "page_name": "GitHub",
        "page_url": "https://www.githubstatus.com",
        "indicator": "major",
        "description": "Major Service Outage",
        "components": [
            {
                "id": "c1",
                "name": "Git Operations",
                "status": "operational",
                "description": "Normal",
            },
            {
                "id": "c2",
                "name": "Actions",
                "status": "major_outage",
            },
        ],
        "incidents": [
            {
                "id": "inc1",
                "name": "Incident with Actions",
                "status": "investigating",
                "impact": "major",
            }
        ],
    }

    summary = StatuspageSummary.model_validate(raw_statuspage)
    assert summary.page_name == "GitHub"
    assert summary.indicator == "major"
    assert len(summary.components) == 2
    assert summary.components[1].status == "major_outage"
    assert len(summary.incidents) == 1


def test_chaos_config_defaults_and_validation():
    """Verify ChaosConfig defaults and boundary constraints."""
    config = ChaosConfig()
    assert not config.active
    assert not config.inject_503
    assert config.failure_rate == 0.0
    assert config.latency_ms == 0

    with pytest.raises(ValidationError):
        ChaosConfig(failure_rate=1.5)  # > 1.0 invalid


def test_triage_request_response_schemas():
    """Verify API request and response serialization."""
    req = TriageRequest(query="Is GitHub Actions down?", force_refresh=True)
    assert req.query == "Is GitHub Actions down?"
    assert req.force_refresh is True

    report = IncidentTriageReport(
        summary="Actions is degraded",
        severity=SeverityLevel.HIGH,
        root_cause_analysis="API rate limits exceeded",
        confidence_score=0.9,
    )
    resp = TriageResponse(
        report=report,
        cached=True,
        cache_similarity=0.94,
        execution_time_ms=12.4,
    )
    assert resp.cached is True
    assert resp.cache_similarity == 0.94
    assert resp.execution_time_ms < 20.0


def test_triage_state_initialization():
    """Verify LangGraph TriageState dictionary typing."""
    state: TriageState = {
        "query": "Investigating high webhook error rate",
        "messages": [],
        "statuspage_telemetry": None,
        "chaos_telemetry": None,
        "circuit_breaker_status": {"statuspage": "CLOSED", "chaos": "CLOSED"},
        "is_degraded": False,
        "repair_attempts": 0,
        "validation_errors": [],
        "final_report": None,
    }
    assert state["query"] == "Investigating high webhook error rate"
    assert state["circuit_breaker_status"]["statuspage"] == "CLOSED"
    assert state["repair_attempts"] == 0

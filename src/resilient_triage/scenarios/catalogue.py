"""Scenario catalogue: the 13 catalogued scenarios, by chapter, in display order (#93, #96).

All page copy for scenarios lives here: chapter titles, scenario titles, captions and setup
notes. Captions lock each scenario's teaching point; their final wording is a UI question.
Every scenario declares what its run must show, which the catalogue test checks by running it.
Scripted replies arrive after about 0.9 s and failures after about 0.18 s, as in the prototype.
"""

import json

from resilient_triage.llm import DEFAULT_REPORT_JSON, ScriptedReply
from resilient_triage.llm.failures import ProviderFailure, RateLimited, Unavailable
from resilient_triage.scenarios.model import (
    CacheSeed,
    Chapter,
    HeldSlots,
    Overrides,
    Scenario,
    Setup,
)
from resilient_triage.schemas.incident import IncidentTriageReport
from resilient_triage.schemas.telemetry import (
    ChaosConfig,
    StatuspageComponent,
    StatuspageSummary,
)

REPLY_DELAY_S = 0.9
FAILURE_DELAY_S = 0.18


def _reply(text: str) -> ScriptedReply:
    """A model reply, after the prototype's reply delay."""
    return ScriptedReply(text, delay_s=REPLY_DELAY_S)


def _fail(failure: ProviderFailure) -> ScriptedReply:
    """A provider failure, after the prototype's failure delay."""
    return ScriptedReply(failure, delay_s=FAILURE_DELAY_S)


_BASE_REPORT = json.loads(DEFAULT_REPORT_JSON)


def _report(**changes: object) -> str:
    """The default valid report as JSON, with `changes` applied to its top-level fields."""
    return json.dumps({**_BASE_REPORT, **changes})


VALID = _report()
LOW_CONFIDENCE = _report(confidence_score=0.45)
REFINED = _report(confidence_score=0.8)
NOT_JSON = "The Actions runners look overloaded; I would scale the pool up."
WRONG_TYPE = _report(confidence_score="very high")
# The model claims a remediative action needs no approval; only reconcile may raise it.
UNAPPROVED_REMEDIATION = _report(
    recommended_actions=[
        {**action, "requires_approval": False} for action in _BASE_REPORT["recommended_actions"]
    ]
)
PRIMARY_RATE_LIMITED = RateLimited(
    retry_after_s=20.0, scope="minute", detail="Primary model per-minute quota used up"
)
FALLBACK_RATE_LIMITED = RateLimited(
    retry_after_s=20.0, scope="minute", detail="Fallback model per-minute quota used up"
)
PROVIDER_DOWN = Unavailable(detail="Provider answered HTTP 503")

GITHUB_DEGRADED = StatuspageSummary(
    page_name="GitHub",
    page_url="https://www.githubstatus.com",
    indicator="major",
    description="Partial System Outage",
    components=[StatuspageComponent(id="actions", name="Actions", status="major_outage")],
)
SEED_REPORT = IncidentTriageReport.model_validate_json(DEFAULT_REPORT_JSON)
CACHED_QUERY = "GitHub Actions jobs stuck in the queued state"
SENTINEL_SLOW = ChaosConfig(active=True, latency_ms=5000)
SENTINEL_DOWN = ChaosConfig(active=True, inject_503=True)

CHAPTERS: tuple[Chapter, ...] = (
    Chapter(id="fast-paths", title="Fast paths"),
    Chapter(id="model-misbehaves", title="When the model misbehaves"),
    Chapter(id="dependencies-fail", title="When dependencies fail"),
    Chapter(id="finale", title="Everything at once"),
)

SCENARIOS: tuple[Scenario, ...] = (
    # Chapter: fast-paths
    Scenario(
        id="healthy-run",
        chapter="fast-paths",
        title="Healthy run",
        query="GitHub Actions workflows failing with 503 errors",
        scope=None,
        script=(_reply(VALID),),
        statuspage=GITHUB_DEGRADED,
        caption="Every dependency answers: one model call, one valid report, cached for next time.",
        expected_outcome="analyzed",
    ),
    Scenario(
        id="cache-hit",
        chapter="fast-paths",
        title="Cache hit",
        query=CACHED_QUERY,
        scope=None,
        script=(),
        statuspage=GITHUB_DEGRADED,
        caption="A question already answered is served from the semantic cache, with no run.",
        expected_outcome="cache_hit",
        setup=Setup(seeds=(CacheSeed(query=CACHED_QUERY, report=SEED_REPORT),)),
        setup_notes=("An answer to this question is already in the cache.",),
    ),
    Scenario(
        id="model-failover",
        chapter="fast-paths",
        title="Model failover",
        query="GitHub webhook deliveries delayed",
        scope=None,
        script=(_fail(PRIMARY_RATE_LIMITED), _reply(VALID)),
        statuspage=GITHUB_DEGRADED,
        caption="The primary model is rate-limited, so the gateway fails over to the fallback.",
        expected_outcome="analyzed",
        setup_notes=("The primary model is out of quota.",),
    ),
    # Chapter: model-misbehaves
    Scenario(
        id="self-repair",
        chapter="model-misbehaves",
        title="Self-repair",
        query="GitHub Actions runners failing to pick up jobs",
        scope=None,
        script=(_reply(NOT_JSON), _reply(VALID)),
        statuspage=GITHUB_DEGRADED,
        caption="An invalid reply is sent back with its errors, and the model repairs it.",
        expected_outcome="analyzed",
        setup_notes=("The model's first reply is not valid JSON.",),
    ),
    Scenario(
        id="refine",
        chapter="model-misbehaves",
        title="Refine",
        query="GitHub repo pushes intermittently rejected",
        scope=None,
        script=(_reply(LOW_CONFIDENCE), _reply(REFINED)),
        statuspage=GITHUB_DEGRADED,
        caption="A valid but low-confidence report earns one reflection round to firm it up.",
        expected_outcome="analyzed",
        setup_notes=("The model's first report has a confidence of 0.45.",),
    ),
    Scenario(
        id="refine-then-provider-failure",
        chapter="model-misbehaves",
        title="Refine, then the provider fails",
        query="GitHub Actions artifact uploads timing out",
        scope=None,
        script=(
            _reply(LOW_CONFIDENCE),
            _fail(PROVIDER_DOWN),
            _fail(PROVIDER_DOWN),
            _fail(PROVIDER_DOWN),
        ),
        statuspage=GITHUB_DEGRADED,
        caption="When the refine round cannot reach a model, the valid pre-refine report is kept.",
        expected_outcome="analyzed",
        setup_notes=(
            "The model's first report has a confidence of 0.45.",
            "After that, both models are unavailable.",
        ),
    ),
    Scenario(
        id="self-repair-exhausted",
        chapter="model-misbehaves",
        title="Self-repair exhausted",
        query="GitHub webhook payloads failing validation",
        scope=None,
        script=(_reply(NOT_JSON), _reply(WRONG_TYPE)),
        statuspage=GITHUB_DEGRADED,
        caption="When repair fails too, a safe low-confidence fallback is returned, uncached.",
        expected_outcome="analyzed",
        setup_notes=("Both of the model's replies are invalid.",),
    ),
    Scenario(
        id="reconciliation",
        chapter="model-misbehaves",
        title="Reconciliation",
        query="GitHub Actions runner pool exhausted",
        scope=None,
        script=(_reply(UNAPPROVED_REMEDIATION),),
        statuspage=GITHUB_DEGRADED,
        caption="The model marks a remediation as safe to run; reconcile requires approval anyway.",
        expected_outcome="analyzed",
        setup_notes=("The model's report claims a remediative action needs no approval.",),
    ),
    # Chapter: dependencies-fail
    Scenario(
        id="unavailable-source",
        chapter="dependencies-fail",
        title="Unavailable source",
        query="GitHub Actions status unknown, workflows hanging",
        scope=None,
        script=(_reply(VALID),),
        statuspage=None,
        caption="Statuspage fails after its retries; the run goes on and is partially degraded.",
        expected_outcome="analyzed",
        setup_notes=("Statuspage fails every read.",),
    ),
    Scenario(
        id="tripped-breaker",
        chapter="dependencies-fail",
        title="Tripped breaker",
        query="GitHub Actions deployments failing",
        scope=None,
        script=(_reply(VALID),),
        statuspage=None,
        caption="An open breaker refuses the read at once; compensation fills the blind spot.",
        expected_outcome="analyzed",
        setup=Setup(tripped_sources=("statuspage",)),
        setup_notes=("Statuspage failed three reads in a row, so its breaker is open.",),
    ),
    Scenario(
        id="run-deadline",
        chapter="dependencies-fail",
        title="Run deadline",
        query="Internal auth service responding slowly",
        scope=None,
        script=(_reply(VALID),),
        statuspage=GITHUB_DEGRADED,
        caption="A run that cannot finish in time is cut off and answered with a fallback report.",
        expected_outcome="analyzed",
        cluster_sentinel=SENTINEL_SLOW,
        overrides=Overrides(run_deadline_s=3.0),
        setup_notes=(
            "Cluster Sentinel takes 5 s to answer.",
            "This run's deadline is 3 s.",
        ),
    ),
    Scenario(
        id="shed-request",
        chapter="dependencies-fail",
        title="Shed request",
        query="GitHub Actions queue backing up",
        scope=None,
        script=(_reply(VALID),),
        statuspage=GITHUB_DEGRADED,
        caption="With every run slot busy, the request waits briefly, then is shed, not queued.",
        expected_outcome="shed",
        overrides=Overrides(held_slots=HeldSlots(capacity=2, queue_timeout_s=1.0)),
        setup_notes=("Both run slots are busy, and a request waits at most 1 s for one.",),
    ),
    # Chapter: finale
    Scenario(
        id="everything-down",
        chapter="finale",
        title="Everything down",
        query="GitHub Actions and the internal auth service are both down",
        scope=None,
        script=(_fail(PRIMARY_RATE_LIMITED), _fail(FALLBACK_RATE_LIMITED)),
        statuspage=None,
        caption="Redis, both sources and both models down: still a valid, degraded answer.",
        expected_outcome="analyzed",
        cluster_sentinel=SENTINEL_DOWN,
        setup=Setup(tripped_sources=("statuspage", "chaos")),
        setup_notes=(
            "Redis is unreachable, so the cache runs in memory.",
            "Statuspage and Cluster Sentinel each failed three reads: both breakers are open.",
            "Both models are out of quota.",
        ),
    ),
)

_BY_ID = {scenario.id: scenario for scenario in SCENARIOS}


def get_scenario(scenario_id: str) -> Scenario | None:
    """The catalogued scenario with this id, or None."""
    return _BY_ID.get(scenario_id)

"""Scenario factory: builds a fully private set of collaborators for each scenario run.

Every run gets its own scripted `LLMGateway`, `TelemetryProbe` (canned or failing Statuspage,
a private `ChaosManager` for Cluster Sentinel), `CircuitBreakerRegistry`, in-memory
`SemanticCacheManager` and `TriageGateway` (ADR-009). The run never reads or mutates the shared
`AppRuntime`; only the scenario slot pool and the process-wide embedder are shared, and both are
injected. The graph, routers, reconciliation, cache and breaker code all run for real.
"""

from dataclasses import replace
from typing import Any

from resilient_triage.cache.embeddings import BaseEmbeddingProvider
from resilient_triage.cache.semantic_cache import SemanticCacheManager
from resilient_triage.cache.unreachable_store import UnreachableRemoteStore
from resilient_triage.config import Settings
from resilient_triage.graph.builder import triage_graph
from resilient_triage.graph.deps import TriageDeps
from resilient_triage.llm import LLMGateway, ScriptedAdapter, build_llm_gateway
from resilient_triage.resilience.circuit_breaker import CircuitBreakerRegistry
from resilient_triage.scenarios.model import Scenario
from resilient_triage.schemas.state import TriageState
from resilient_triage.telemetry.canned import CannedStatuspageSource, FailingStatuspageSource
from resilient_triage.telemetry.chaos import ChaosManager, ClusterSentinelSource
from resilient_triage.telemetry.probe import TelemetryProbe, TelemetrySource
from resilient_triage.trace.events import ScenarioSetup
from resilient_triage.trace.recorder import NullTrace, TraceRecorder, record_event
from resilient_triage.triage.gateway import TriageGateway, TriageOutcome
from resilient_triage.triage.slots import RunSlotPool


class ScenarioRun:
    """One scenario's private collaborators, ready to run once."""

    def __init__(
        self,
        scenario: Scenario,
        *,
        llm: LLMGateway,
        telemetry: TelemetryProbe,
        breakers: CircuitBreakerRegistry,
        chaos: ChaosManager,
        cache: SemanticCacheManager,
        slots: RunSlotPool,
        settings: Settings,
    ) -> None:
        self.scenario = scenario
        self.llm = llm
        self.telemetry = telemetry
        self.breakers = breakers
        self.chaos = chaos
        self.cache = cache
        self.slots = slots
        self._held = 0  # slots this run's setup holds, given back when the run ends
        self.deps = TriageDeps(llm=llm, telemetry=telemetry, history=cache)
        self.gateway = TriageGateway(
            cache, self._run_graph, settings, slots=slots, scenario_id=scenario.id
        )

    async def triage(self, *, trace: TraceRecorder | None = None) -> TriageOutcome:
        """Run the scenario's request through its private gateway, recording on `trace`."""
        recorder: TraceRecorder = trace if trace is not None else NullTrace()
        try:
            await self._setup(recorder)
            return await self.gateway.triage(
                self.scenario.query, self.scenario.scope, trace=recorder
            )
        finally:
            self._release_held()

    async def _setup(self, trace: TraceRecorder) -> None:
        """Do the scenario setup, recording it on `trace` before the request's trace_started."""
        setup = self.scenario.setup
        if setup.seeds:
            for seed in setup.seeds:
                await self.cache.store(seed.query, seed.report, seed.scope)
            record_event(trace, lambda: ScenarioSetup(step="seed_cache", count=len(setup.seeds)))
        for source in setup.tripped_sources:
            await self._trip(source, trace)
        if self.scenario.overrides.held_slots is not None:
            await self._hold_every_slot(trace)

    async def _hold_every_slot(self, trace: TraceRecorder) -> None:
        """Take every slot of this run's private pool, so its request queues and is shed."""
        while self.slots.free and await self.slots.acquire():
            self._held += 1
        held = self._held
        record_event(trace, lambda: ScenarioSetup(step="hold_run_slots", count=held))

    def _release_held(self) -> None:
        """Give back every slot the setup held, so none leaks, even on cancellation."""
        while self._held:
            self._held -= 1
            self.slots.release()

    async def _trip(self, source: str, trace: TraceRecorder) -> None:
        """Fail real reads of `source` until its private breaker opens (fail_max of them)."""
        breaker = self.breakers.get(source)
        if breaker is None:
            raise ValueError(f"No breaker for scenario source {source!r}")
        failed = 0
        while not breaker.info().is_open:
            if failed >= breaker.info().fail_max:
                raise RuntimeError(f"Scenario source {source!r} did not fail in setup")
            await self.telemetry.collect(
                frozenset({source}),
                self.scenario.query,
                self.scenario.scope,
                trace=trace,
                phase="setup",
            )
            failed += 1
        record_event(trace, lambda: ScenarioSetup(step="fail_source", count=failed, source=source))

    async def _run_graph(
        self, run_input: TriageState, /, *, trace: TraceRecorder
    ) -> dict[str, Any]:
        """Run the triage graph on this run's private deps, recording on `trace`."""
        return await triage_graph.ainvoke(run_input, context=replace(self.deps, trace=trace))


class ScenarioFactory:
    """Turns a scenario definition into a `ScenarioRun` with fresh, private collaborators."""

    def __init__(
        self, *, settings: Settings, slots: RunSlotPool, embedder: BaseEmbeddingProvider
    ) -> None:
        self._settings = settings
        self._slots = slots
        self._embedder = embedder

    def build(self, scenario: Scenario) -> ScenarioRun:
        """Build a run's private collaborators; performs no I/O."""
        settings = self._settings
        deadline_s = scenario.overrides.run_deadline_s
        if deadline_s is not None:  # a copy for this run only
            settings = settings.model_copy(update={"triage_run_deadline_s": deadline_s})
        held = scenario.overrides.held_slots
        slots = (
            RunSlotPool(capacity=held.capacity, queue_timeout_s=held.queue_timeout_s)
            if held is not None
            else self._slots
        )
        breakers = CircuitBreakerRegistry()
        chaos = ChaosManager()
        if scenario.cluster_sentinel is not None:
            chaos.update_config(scenario.cluster_sentinel)
        statuspage: TelemetrySource = (
            CannedStatuspageSource(scenario.statuspage)
            if scenario.statuspage is not None
            else FailingStatuspageSource()
        )
        telemetry = TelemetryProbe(
            [statuspage, ClusterSentinelSource(chaos)],
            breakers=breakers,
            fail_max=settings.circuit_breaker_fail_max,
            reset_timeout=settings.circuit_breaker_reset_timeout,
            source_deadline_s=settings.telemetry_source_deadline_s,
        )
        cache = SemanticCacheManager(
            embedding_provider=self._embedder, remote_store_factory=UnreachableRemoteStore
        )
        return ScenarioRun(
            scenario,
            llm=build_llm_gateway(settings, ScriptedAdapter(scenario.script)),
            telemetry=telemetry,
            breakers=breakers,
            chaos=chaos,
            cache=cache,
            slots=slots,
            settings=settings,
        )

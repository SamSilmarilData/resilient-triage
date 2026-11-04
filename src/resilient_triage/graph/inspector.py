"""The node inspector's content: what the page shows when a graph node is selected (#121).

*Purpose* is the node module's docstring and *file* its module path, so both come from the
code. `NODE_NOTES` is the small typed registry for what code can't say about itself: the
`Settings` fields that tune each node and the ADRs that explain it. `tests/test_node_inspector.py`
fails on a graph node with no entry, a stale entry, an unknown setting or a missing ADR file.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType


@dataclass(frozen=True)
class NodeNotes:
    """What the registry adds to a node: its tuning `Settings` fields and its ADR file stems."""

    config_fields: tuple[str, ...] = ()
    adrs: tuple[str, ...] = ()


_LLM_FIELDS = (
    "groq_model",
    "groq_fallback_model",
    "llm_request_timeout_s",
    "llm_temperature",
    "llm_max_tokens",
    "llm_default_cooldown_s",
    "llm_max_cooldown_s",
)

NODE_NOTES: Mapping[str, NodeNotes] = MappingProxyType(
    {
        "analyze_query": NodeNotes(adrs=("0001-langgraph-cyclic-state-machine",)),
        "collect_telemetry": NodeNotes(
            config_fields=(
                "statuspage_url",
                "telemetry_source_deadline_s",
                "circuit_breaker_fail_max",
                "circuit_breaker_reset_timeout",
                "retry_max_attempts",
                "retry_min_wait_seconds",
                "retry_max_wait_seconds",
            ),
            adrs=("0003-pybreaker-and-tenacity-for-resilience",),
        ),
        "compensate_blindspots": NodeNotes(
            adrs=(
                "0002-dual-engine-semantic-caching",
                "0003-pybreaker-and-tenacity-for-resilience",
            ),
        ),
        "degraded_fallback": NodeNotes(adrs=("0003-pybreaker-and-tenacity-for-resilience",)),
        "synthesize_telemetry": NodeNotes(adrs=("0001-langgraph-cyclic-state-machine",)),
        "generate_report": NodeNotes(
            config_fields=_LLM_FIELDS,
            adrs=(
                "0005-free-tier-groq-quota-monitoring-and-failover",
                "0006-project-owned-llm-gateway",
            ),
        ),
        "validate_report": NodeNotes(
            config_fields=("max_self_repair_retries",),
            adrs=("0004-strict-pydantic-contracts-and-self-repair",),
        ),
        "self_repair": NodeNotes(
            config_fields=("max_self_repair_retries",),
            adrs=(
                "0001-langgraph-cyclic-state-machine",
                "0004-strict-pydantic-contracts-and-self-repair",
            ),
        ),
        "refine_reflection": NodeNotes(
            adrs=(
                "0001-langgraph-cyclic-state-machine",
                "0004-strict-pydantic-contracts-and-self-repair",
            ),
        ),
        "fallback_report": NodeNotes(
            adrs=(
                "0004-strict-pydantic-contracts-and-self-repair",
                "0006-project-owned-llm-gateway",
            ),
        ),
        "finalize_report": NodeNotes(
            adrs=("0004-strict-pydantic-contracts-and-self-repair", "0008-execution-trace"),
        ),
    }
)

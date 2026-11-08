"""The node inspector's content: what the page shows when a graph node is selected (#121).

*Purpose* is the node module's docstring and *file* its module path, so both come from the
code. `NODE_NOTES` is the small typed registry for what code can't say about itself: the
`Settings` fields that tune each node and the ADRs that explain it. `tests/test_node_inspector.py`
fails on a graph node with no entry, a stale entry, an unknown setting or a missing ADR file.
"""

import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

import resilient_triage
from resilient_triage.config import Settings
from resilient_triage.graph.builder import NODE_FUNCTIONS
from resilient_triage.schemas.api import ConfigFieldView, GraphNodes, NodeInspectorView

ADR_PATH = "docs/decisions/{}.md"

# The ADRs the registry cites, by file stem.
ADR_LANGGRAPH = "0001-langgraph-cyclic-state-machine"
ADR_SEMANTIC_CACHE = "0002-dual-engine-semantic-caching"
ADR_RESILIENCE = "0003-pybreaker-and-tenacity-for-resilience"
ADR_SELF_REPAIR = "0004-strict-pydantic-contracts-and-self-repair"
ADR_QUOTA_FAILOVER = "0005-free-tier-groq-quota-monitoring-and-failover"
ADR_LLM_GATEWAY = "0006-project-owned-llm-gateway"
ADR_EXECUTION_TRACE = "0008-execution-trace"

# The directory holding the package, both in a checkout and installed from the wheel, and
# where that directory sits in the repository (pyproject.toml `packages.find.where`). Served
# file paths are repository-relative; tests/test_node_inspector.py checks each one exists.
_PACKAGE_PARENT = Path(resilient_triage.__file__).resolve().parent.parent
_PACKAGE_PARENT_IN_REPO = Path("src")


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
        "analyze_query": NodeNotes(adrs=(ADR_LANGGRAPH,)),
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
            adrs=(ADR_RESILIENCE,),
        ),
        "compensate_blindspots": NodeNotes(adrs=(ADR_SEMANTIC_CACHE, ADR_RESILIENCE)),
        "degraded_fallback": NodeNotes(adrs=(ADR_RESILIENCE,)),
        "synthesize_telemetry": NodeNotes(adrs=(ADR_LANGGRAPH,)),
        "generate_report": NodeNotes(
            config_fields=_LLM_FIELDS, adrs=(ADR_QUOTA_FAILOVER, ADR_LLM_GATEWAY)
        ),
        "validate_report": NodeNotes(
            config_fields=("max_self_repair_retries",), adrs=(ADR_SELF_REPAIR,)
        ),
        "self_repair": NodeNotes(
            config_fields=("max_self_repair_retries",), adrs=(ADR_LANGGRAPH, ADR_SELF_REPAIR)
        ),
        "refine_reflection": NodeNotes(adrs=(ADR_LANGGRAPH, ADR_SELF_REPAIR)),
        "fallback_report": NodeNotes(adrs=(ADR_SELF_REPAIR, ADR_LLM_GATEWAY)),
        "finalize_report": NodeNotes(adrs=(ADR_SELF_REPAIR, ADR_EXECUTION_TRACE)),
    }
)


def _purpose(name: str, node: Callable[..., Any]) -> str:
    """The first line of a node module's docstring, without its `Node: <name> - ` prefix."""
    module = inspect.getmodule(node)
    doc = ((module.__doc__ if module else None) or "").strip().splitlines()
    return doc[0].removeprefix(f"Node: {name} - ") if doc else ""


def _repo_path(node: Callable[..., Any]) -> str:
    """The repository-relative path of the file a node function is defined in."""
    in_package = Path(inspect.getfile(node)).resolve().relative_to(_PACKAGE_PARENT)
    return (_PACKAGE_PARENT_IN_REPO / in_package).as_posix()


def graph_nodes() -> GraphNodes:
    """Inspector content for every graph node: purpose and file from code, the rest from notes."""
    fields = Settings.model_fields
    return GraphNodes(
        nodes=[
            NodeInspectorView(
                name=name,
                purpose=_purpose(name, node),
                file=_repo_path(node),
                config_fields=[
                    ConfigFieldView(name=field, description=fields[field].description or "")
                    for field in NODE_NOTES[name].config_fields
                ],
                adrs=[ADR_PATH.format(adr) for adr in NODE_NOTES[name].adrs],
            )
            for name, node in NODE_FUNCTIONS.items()
        ]
    )

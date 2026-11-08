"""The node inspector's content covers the real graph, with nothing stale (#121, #101).

The inspector's *purpose* is each node module's docstring and its *file* the module's path; a
small typed registry (`graph/inspector.py`) holds only each node's `config_fields` and `adrs`.
`registry_problems` is the guard against content drifting from code: it is run on the real
registry (must be clean) and on broken ones (must name each problem), so the guard itself is
tested.
"""

from collections.abc import Collection, Mapping

import httpx
import pytest
from fakes import NODE_ORDER
from repo_scripts import REPO_ROOT

from resilient_triage.config import Settings
from resilient_triage.graph.builder import triage_graph
from resilient_triage.graph.inspector import NODE_NOTES, NodeNotes

ADR_DIR = REPO_ROOT / "docs" / "decisions"
TERMINALS = {"__start__", "__end__"}  # LangGraph's own entry/exit: no module, not selectable
SECRETS = {"groq_api_key", "admin_api_key"}


def registry_problems(notes: Mapping[str, NodeNotes], graph_nodes: Collection[str]) -> list[str]:
    """Every way the registry disagrees with the graph, Settings or the ADR folder."""
    problems = [f"missing: {n}" for n in graph_nodes if n not in TERMINALS and n not in notes]
    problems += [f"stale: {n}" for n in notes if n not in graph_nodes or n in TERMINALS]
    for name, entry in notes.items():
        for field in entry.config_fields:
            if field not in Settings.model_fields:
                problems.append(f"unknown setting: {name}.{field}")
            elif field in SECRETS:
                problems.append(f"secret setting: {name}.{field}")
        for adr in entry.adrs:
            if not (ADR_DIR / f"{adr}.md").is_file():
                problems.append(f"missing ADR: {name}.{adr}")
    return problems


def test_the_registry_has_one_clean_entry_per_graph_node() -> None:
    assert registry_problems(NODE_NOTES, list(triage_graph.get_graph().nodes)) == []


GRAPH = ["__start__", "analyze_query", "generate_report", "__end__"]
GOOD = NodeNotes(config_fields=("groq_model",), adrs=("0006-project-owned-llm-gateway",))


@pytest.mark.parametrize(
    ("notes", "problem"),
    [
        ({"analyze_query": NodeNotes()}, "missing: generate_report"),
        (
            {"analyze_query": NodeNotes(), "generate_report": GOOD, "retired_node": NodeNotes()},
            "stale: retired_node",
        ),
        (
            {"analyze_query": NodeNotes(), "generate_report": GOOD, "__end__": NodeNotes()},
            "stale: __end__",
        ),
        (
            {"analyze_query": NodeNotes(config_fields=("groq_modle",)), "generate_report": GOOD},
            "unknown setting: analyze_query.groq_modle",
        ),
        (
            {"analyze_query": NodeNotes(config_fields=("groq_api_key",)), "generate_report": GOOD},
            "secret setting: analyze_query.groq_api_key",
        ),
        (
            {"analyze_query": NodeNotes(adrs=("0099-not-written",)), "generate_report": GOOD},
            "missing ADR: analyze_query.0099-not-written",
        ),
    ],
)
def test_the_guard_names_each_kind_of_drift(notes: dict[str, NodeNotes], problem: str) -> None:
    assert registry_problems(notes, GRAPH) == [problem]


async def test_get_graph_nodes_serves_purpose_file_config_and_adrs(
    anonymous_client: httpx.AsyncClient,
) -> None:
    resp = await anonymous_client.get("/graph/nodes")

    assert resp.status_code == 200
    nodes = {n["name"]: n for n in resp.json()["nodes"]}
    assert list(nodes) == NODE_ORDER
    assert nodes["analyze_query"] == {
        "name": "analyze_query",
        "purpose": "Cleans up the question and picks which telemetry sources to read.",
        "file": "src/resilient_triage/graph/nodes/analyze_query.py",
        "config_fields": [],
        "adrs": ["docs/decisions/0001-langgraph-cyclic-state-machine.md"],
    }
    # Two node names differ from their module's: the file comes from the code, not the name.
    assert nodes["synthesize_telemetry"]["file"] == "src/resilient_triage/graph/nodes/synthesize.py"
    assert nodes["finalize_report"]["file"] == "src/resilient_triage/graph/nodes/finalize.py"
    assert nodes["generate_report"]["config_fields"][0] == {
        "name": "groq_model",
        "description": "Groq model asked first (for example qwen/qwen3.8-27b)",
    }


async def test_every_served_file_and_adr_exists_and_every_purpose_is_a_sentence(
    anonymous_client: httpx.AsyncClient,
) -> None:
    for node in (await anonymous_client.get("/graph/nodes")).json()["nodes"]:
        assert (REPO_ROOT / node["file"]).is_file(), node["file"]
        assert all((REPO_ROOT / adr).is_file() for adr in node["adrs"]), node["adrs"]
        purpose = node["purpose"]
        assert purpose[0].isupper() and purpose.endswith("."), (node["name"], purpose)
        assert not purpose.startswith("Node:"), purpose


# Copy the page shows visitors (#165): sentence case, "the model" rather than "LLM", and no code
# jargon. Proper nouns are the only capitalised words after the first, which also catches
# breaker state names (OPEN, HALF_OPEN) and class names.
PROPER_NOUNS = {"Groq", "Statuspage", "JSON"}
JARGON = {"llm", "tenacity", "pydantic", "max", "min", "e.g."}


def copy_problems(text: str) -> list[str]:
    """Every way a visitor-facing sentence breaks the page's voice."""
    words = text.rstrip(".").replace("(", " ").replace(")", " ").replace(",", " ").split()
    problems = [] if text[0].isupper() else ["not capitalised"]
    problems += [f"capitalised: {w}" for w in words[1:] if w[0].isupper() and w not in PROPER_NOUNS]
    problems += [f"jargon: {w}" for w in words if w.lower() in JARGON]
    return problems


@pytest.mark.parametrize(
    ("text", "problems"),
    [
        ("Asks the model for a draft report as JSON.", []),
        ("Uses Groq (e.g. qwen/qwen3.8-27b)", ["jargon: e.g."]),
        ("Invokes LLM to produce candidate report JSON.", ["capitalised: LLM", "jargon: LLM"]),
        ("Picks the run's Targeted sources.", ["capitalised: Targeted"]),
        ("tenacity min wait", ["not capitalised", "jargon: tenacity", "jargon: min"]),
    ],
)
def test_the_voice_check_names_each_problem(text: str, problems: list[str]) -> None:
    assert copy_problems(text) == problems


async def test_purposes_and_config_descriptions_are_in_the_pages_voice(
    anonymous_client: httpx.AsyncClient,
) -> None:
    nodes = (await anonymous_client.get("/graph/nodes")).json()["nodes"]
    texts = {n["purpose"] for n in nodes}
    texts |= {f["description"] for n in nodes for f in n["config_fields"]}

    assert {text: copy_problems(text) for text in texts if copy_problems(text)} == {}

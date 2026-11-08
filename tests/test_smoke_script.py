"""Tests for the live smoke test script's fixed inputs."""

import re

from fakes import make_triage_deps
from langgraph.runtime import Runtime
from repo_scripts import load_script

from resilient_triage.graph.nodes.analyze_query import analyze_query_node
from resilient_triage.telemetry.chaos import ClusterSentinelSource
from resilient_triage.telemetry.statuspage import StatuspageSource


def test_outage_query_targets_cluster_sentinel():
    """Smoke step 7 reaches Cluster Sentinel under substring and whole-word matching (#46)."""
    query = load_script("smoke_test").OUTAGE_QUERY

    state = analyze_query_node({"query": query}, Runtime(context=make_triage_deps()))
    assert state["targeted_sources"] == ["chaos"]
    # Whole-word matching (#46) must still find an internal keyword and no external one.
    lowered = query.lower()
    assert any(re.search(rf"\b{re.escape(kw)}\b", lowered) for kw in ClusterSentinelSource.keywords)
    assert not [kw for kw in StatuspageSource.keywords if kw in lowered]

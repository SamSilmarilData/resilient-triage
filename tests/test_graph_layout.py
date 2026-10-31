"""The page's hand-placed graph layout covers the real topology, both ways (#116, ADR 0010).

`static/graph-layout.json` is one top-to-bottom layout used at every width (Deferred UI
questions, #101): node centres and widths, and each edge as an orthogonal polyline, with
conditional edges keyed by their router label as in the execution trace (ADR 0008).
"""

import itertools
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from resilient_triage.graph.builder import route_labels, triage_graph

LAYOUT_FILE = (
    Path(__file__).resolve().parents[1]
    / "src"
    / "resilient_triage"
    / "api"
    / "static"
    / "graph-layout.json"
)
# At 375 px with 16 px gutters the graph gets 343 px; node labels must stay at least this big.
PHONE_GRAPH_WIDTH_PX = 343
MIN_PHONE_LABEL_PX = 11
LABEL_PADDING = 8  # each side of a node's name inside its box

Box = tuple[float, float, float, float]  # left, top, right, bottom
Edge = tuple[str, str, str | None]  # source, target, router label (None when unconditional)


@pytest.fixture(scope="module")
def layout() -> dict[str, Any]:
    return json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))


def _graph_edges() -> set[Edge]:
    labels = route_labels(triage_graph)
    return {
        (e.source, e.target, labels[(e.source, e.target)] if e.conditional else None)
        for e in triage_graph.get_graph().edges
    }


def _layout_edges(layout: dict[str, Any]) -> list[Edge]:
    return [(e["source"], e["target"], e.get("route")) for e in layout["edges"]]


def _box(layout: dict[str, Any], name: str) -> Box:
    node = layout["nodes"][name]
    half_w = node["r"] if "r" in node else node["width"] / 2
    half_h = node["r"] if "r" in node else layout["node_height"] / 2
    return (node["x"] - half_w, node["y"] - half_h, node["x"] + half_w, node["y"] + half_h)


def _overlaps(a: Box, b: Box) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _inside_view(layout: dict[str, Any], x: float, y: float) -> bool:
    left, top, width, height = layout["view_box"]
    return left <= x <= left + width and top <= y <= top + height


def test_every_graph_node_is_placed_and_no_placed_node_is_stale(layout: dict[str, Any]):
    """Layout nodes equal get_graph() nodes, __start__ and __end__ included."""
    graph_nodes = set(triage_graph.get_graph().nodes)
    placed = set(layout["nodes"])

    assert graph_nodes - placed == set(), "nodes missing from graph-layout.json"
    assert placed - graph_nodes == set(), "stale nodes in graph-layout.json"


def test_every_graph_edge_is_routed_and_no_routed_edge_is_stale(layout: dict[str, Any]):
    """Layout edges equal get_graph() edges, each conditional one keyed by its router label."""
    graph_edges = _graph_edges()
    routed = _layout_edges(layout)

    assert len(routed) == len(set(routed)), "an edge is routed twice"
    assert graph_edges - set(routed) == set(), "edges missing from graph-layout.json"
    assert set(routed) - graph_edges == set(), "stale edges in graph-layout.json"


def test_layout_is_top_to_bottom(layout: dict[str, Any]):
    """The run starts at the top and ends at the bottom of the one layout."""
    ys = {name: node["y"] for name, node in layout["nodes"].items()}

    assert layout["direction"] == "top-to-bottom"
    assert min(ys, key=ys.__getitem__) == "__start__"
    assert max(ys, key=ys.__getitem__) == "__end__"


def test_nodes_fit_their_names_and_do_not_overlap(layout: dict[str, Any]):
    """Each box holds its name in the mono font, sits inside the view box and clears the others."""
    font = layout["font"]
    char_px = font["node_px"] * font["mono_advance_em"]
    for name, node in layout["nodes"].items():
        assert ("r" in node) != ("width" in node), f"{name}: a terminal has r, a node a width"
        if name in ("__start__", "__end__"):
            assert "r" in node, f"{name} is drawn as a terminal"
        else:
            assert node["width"] >= len(name) * char_px + 2 * LABEL_PADDING, f"{name} too narrow"
        left, top, right, bottom = _box(layout, name)
        assert _inside_view(layout, left, top) and _inside_view(layout, right, bottom), name

    for a, b in itertools.combinations(layout["nodes"], 2):
        assert not _overlaps(_box(layout, a), _box(layout, b)), f"{a} overlaps {b}"


def test_labels_stay_full_size_at_phone_width(layout: dict[str, Any]):
    """Scaled to a 375 px phone's content width, node labels are still at least 11 px."""
    width = layout["view_box"][2]

    assert layout["font"]["node_px"] * min(1, PHONE_GRAPH_WIDTH_PX / width) >= MIN_PHONE_LABEL_PX


def _on_border(point: list[float], box: Box) -> bool:
    x, y = point
    left, top, right, bottom = box
    on_side = x in (left, right) and top <= y <= bottom
    return on_side or (y in (top, bottom) and left <= x <= right)


def _crosses(start: list[float], end: list[float], box: Box) -> bool:
    """Whether an axis-aligned segment passes through a box's interior."""
    seg: Box = (
        min(start[0], end[0]),
        min(start[1], end[1]),
        max(start[0], end[0]),
        max(start[1], end[1]),
    )
    left, top, right, bottom = box
    return seg[0] < right and left < seg[2] and seg[1] < bottom and top < seg[3]


def test_edges_are_orthogonal_paths_between_their_nodes(layout: dict[str, Any]):
    """Each edge leaves its source's border, enters its target's, turns only at right angles,
    stays in the view box and passes through no other node."""
    for edge in layout["edges"]:
        name = f"{edge['source']} to {edge['target']}"
        points = edge["points"]
        assert len(points) >= 2, name
        assert _on_border(points[0], _box(layout, edge["source"])), f"{name}: bad start"
        assert _on_border(points[-1], _box(layout, edge["target"])), f"{name}: bad end"
        for start, end in itertools.pairwise(points):
            assert (start[0] == end[0]) != (start[1] == end[1]), f"{name}: {start}, {end}"
            assert _inside_view(layout, *start) and _inside_view(layout, *end), name
            for other in layout["nodes"].keys() - {edge["source"], edge["target"]}:
                assert not _crosses(start, end, _box(layout, other)), f"{name} crosses {other}"


def _label_box(layout: dict[str, Any], edge: dict[str, Any]) -> Box:
    """The box a start-anchored router label covers, its y being the text baseline."""
    font = layout["font"]
    x, y = edge["label"]
    width = len(edge["route"]) * font["label_px"] * font["mono_advance_em"]
    return (x, y - font["label_px"], x + width, y + 2)


def test_conditional_edges_carry_a_clear_router_label(layout: dict[str, Any]):
    """Only conditional edges are labelled, and each label clears every node and other label."""
    labelled = [edge for edge in layout["edges"] if "route" in edge]
    for edge in layout["edges"]:
        assert ("label" in edge) == ("route" in edge), f"{edge['source']} to {edge['target']}"

    boxes = {f"{e['source']} to {e['target']}": _label_box(layout, e) for e in labelled}
    for name, box in boxes.items():
        assert _inside_view(layout, box[0], box[1]) and _inside_view(layout, box[2], box[3]), name
        for node in layout["nodes"]:
            assert not _overlaps(box, _box(layout, node)), f"label of {name} overlaps {node}"
    for (a, box_a), (b, box_b) in itertools.combinations(boxes.items(), 2):
        assert not _overlaps(box_a, box_b), f"labels of {a} and {b} overlap"


async def test_layout_is_served_to_the_page(client: httpx.AsyncClient):
    """The page fetches the layout as JSON from /static, revalidated like unversioned files."""
    response = await client.get("/static/graph-layout.json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.json() == json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))

"""The page's hand-placed graph layout covers the real topology, both ways (#116, ADR 0010).

`static/graph-layout.json` is one top-to-bottom layout used at every width (Deferred UI
questions, #101): node centres and widths, and each edge as an orthogonal polyline, with
conditional edges keyed by their router label as in the execution trace (ADR 0008).
"""

import itertools
import json
from typing import Any

import httpx
import pytest
from repo_scripts import STATIC_DIR

from resilient_triage.graph.builder import route_labels, triage_graph

LAYOUT_FILE = STATIC_DIR / "graph-layout.json"
# At 375 px with 16 px gutters the graph gets 343 px; labels must render at their nominal size
# there (scale >= 1), and no label is nominally smaller than this.
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
    """Scaled to a 375 px phone's content width, node and router labels keep their nominal px."""
    font = layout["font"]
    scale = min(1, PHONE_GRAPH_WIDTH_PX / layout["view_box"][2])

    for key in ("node_px", "label_px"):
        assert font[key] >= MIN_PHONE_LABEL_PX, f"{key} is below {MIN_PHONE_LABEL_PX} px"
        assert font[key] * scale >= font[key], f"{key} renders at {font[key] * scale:.1f} px"


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
    """Each edge runs from its source's border to its target's through no other node."""
    # Every segment is horizontal or vertical (right-angle turns only) and stays in the view box.
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


# Parallel runs of two edges closer than this read as one line (#162): the loop-back edges
# (refine, repair, fallback) used to run 8 and 12 units apart along the graph's right side.
MIN_LANE_GAP = 16

Segment = tuple[list[float], list[float]]


def _segments(edge: dict[str, Any]) -> list[Segment]:
    return list(itertools.pairwise(edge["points"]))


def _edge_name(edge: dict[str, Any]) -> str:
    return f"{edge['source']} to {edge['target']} ({edge.get('route', '-')})"


def _parallel_gap(a: Segment, b: Segment) -> float | None:
    """How far apart two parallel segments run where their spans overlap; None otherwise."""
    axis = 0 if a[0][0] == a[1][0] else 1  # 0: vertical (constant x), 1: horizontal
    if (b[0][0] == b[1][0]) != (axis == 0):
        return None
    along = 1 - axis
    a_lo, a_hi = sorted((a[0][along], a[1][along]))
    b_lo, b_hi = sorted((b[0][along], b[1][along]))
    if min(a_hi, b_hi) - max(a_lo, b_lo) <= 0:
        return None
    return abs(a[0][axis] - b[0][axis])


def test_no_two_edges_run_along_each_other(layout: dict[str, Any]):
    """Where two edges run side by side they keep a lane apart, so each branch can be followed."""
    for a, b in itertools.combinations(layout["edges"], 2):
        for seg_a in _segments(a):
            for seg_b in _segments(b):
                gap = _parallel_gap(seg_a, seg_b)
                assert gap is None or gap >= MIN_LANE_GAP, (
                    f"{_edge_name(a)} runs {gap} units from {_edge_name(b)}"
                )


def _box_to_segment(box: Box, segment: Segment) -> float:
    """The shortest distance between a box and an axis-aligned segment."""
    (x1, y1), (x2, y2) = segment
    dx = max(box[0] - max(x1, x2), min(x1, x2) - box[2], 0)
    dy = max(box[1] - max(y1, y2), min(y1, y2) - box[3], 0)
    return float((dx**2 + dy**2) ** 0.5)


def _box_to_edge(box: Box, edge: dict[str, Any]) -> float:
    return min(_box_to_segment(box, segment) for segment in _segments(edge))


def test_each_router_label_sits_nearest_its_own_edge(layout: dict[str, Any]):
    """A router label is closer to the edge it names than to any other edge."""
    for edge in (e for e in layout["edges"] if "route" in e):
        box = _label_box(layout, edge)
        own = _box_to_edge(box, edge)
        for other in layout["edges"]:
            if other is edge:
                continue
            assert own < _box_to_edge(box, other), (
                f"label {edge['route']!r} of {_edge_name(edge)} is as near {_edge_name(other)}"
            )


async def test_layout_is_served_to_the_page(client: httpx.AsyncClient):
    """The page fetches the layout as JSON from /static, revalidated like unversioned files."""
    response = await client.get("/static/graph-layout.json")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.headers["cache-control"] == "no-cache"
    assert response.json() == json.loads(LAYOUT_FILE.read_text(encoding="utf-8"))

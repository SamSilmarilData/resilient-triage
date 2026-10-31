"""Scenario routes (ADR-009): the public catalogue, scenario streams, and the free-play limits."""

import httpx

DISPLAY_ORDER = {
    "fast-paths": ["healthy-run", "cache-hit", "model-failover"],
    "model-misbehaves": [
        "self-repair",
        "refine",
        "refine-then-provider-failure",
        "self-repair-exhausted",
        "reconciliation",
    ],
    "dependencies-fail": ["unavailable-source", "tripped-breaker", "run-deadline", "shed-request"],
    "finale": ["everything-down"],
}


async def test_get_scenarios_lists_the_public_view_by_chapter_in_display_order(
    client: httpx.AsyncClient,
) -> None:
    resp = await client.get("/scenarios")

    assert resp.status_code == 200
    chapters = resp.json()["chapters"]
    assert [c["id"] for c in chapters] == list(DISPLAY_ORDER)
    assert {c["id"]: [s["id"] for s in c["scenarios"]] for c in chapters} == DISPLAY_ORDER
    assert chapters[0]["title"] == "Fast paths"
    healthy = chapters[0]["scenarios"][0]
    assert set(healthy) == {"id", "chapter", "title", "caption", "setup_notes"}
    assert healthy["chapter"] == "fast-paths"

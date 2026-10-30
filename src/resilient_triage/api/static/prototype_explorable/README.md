# PROTOTYPE: explorable page (issue #96). Throwaway, never merged.

Three structurally different layouts of the explorable page, built on **real recorded traces**:

- **A · Essay**: an explorable explanation you read top to bottom, with the graph and waterfall sticky beside the prose.
- **B · Studio**: a three-pane workbench with the scenario rail, graph over waterfall, and an inspector.
- **C · Replay**: one stage with a narrated caption and a scrubber, then a key-facts card and the report.

Run from the repo root:

```sh
python3 src/resilient_triage/api/static/prototype_explorable/serve.py   # http://127.0.0.1:8765/?variant=A
PYTHONPATH=src .venv/bin/python src/resilient_triage/api/static/prototype_explorable/record_traces.py  # re-record
```

Switch layouts with `?variant=A|B|C`, the floating bar, or the ← / → keys. Pick a scenario with `#<id>`, for
example `#everything-down`.

`record_traces.py` runs all 13 catalogue scenarios through the real `TriageGateway`, graph, `LLMGateway`,
`TelemetryProbe`, breakers, reconciliation and in-memory cache, with a `ScriptedAdapter` and a canned
Statuspage. `TraceRecorder` doesn't exist yet, so the recorder wraps instance methods and reads `astream_events`.
The events follow ADR 0008's kinds and its no-free-text rule. There are two additions, both flagged as proposals:
`scenario_setup`, and `confidence` on `validate_report`'s `node_finished`.

The vendored files under `vendor/` are the approved versions (Preact 10.29.8, htm 3.1.1, @preact/signals 2.11.4,
signals-core 1.14.5), with source-map comments stripped. No manifest, no fonts (system stacks stand in).

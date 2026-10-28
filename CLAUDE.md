# resilient-triage

Fault-tolerant incident triage gateway: FastAPI → semantic cache → LangGraph state machine
(Groq LLM) → strict Pydantic report. Core promise: **never 500, never stall** when Redis,
Statuspage, or the LLM is down — degrade instead.

## Tech Stack
- Python 3.12, FastAPI + uvicorn, Pydantic v2 / pydantic-settings
- LangGraph (cyclic graph, `BoundedMemorySaver` checkpointer), langchain-core
- Resilience: pybreaker (circuit breakers), tenacity (retry + jitter), httpx (async)
- Cache: Redis 8 RediSearch HNSW, with in-memory NumPy fallback; FastEmbed `BAAI/bge-small-en-v1.5` (384-d)
- LLM: Groq (`qwen/qwen3.8-27b`) via direct async HTTP, JSON response mode

## Commands (run inside `.venv`)
- Setup: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[test,lint]"`, then
  `git config core.hooksPath .githooks` (pre-commit runs ruff)
- Check: `./scripts/check.sh` — ruff, ruff format, mypy, pytest, as CI runs them; extra args go
  to pytest. Redis is optional: tests fall back to the in-memory cache.
- Run: `./scripts/run_local.sh` (UI :8000, docs /docs, /health)
- Docker: `docker compose up`; smoke test: `python scripts/smoke_test.py`
- CI (`.github/workflows/ci.yml`) runs exactly: ruff check, ruff format --check, mypy src/, pytest, docker build + /health

## Project Map (`src/resilient_triage/`)
- `api/app.py` — routes: `POST /triage`, `/health`, `/metrics`, `/resilience/status`, `/llm/quota`,
  `/chaos/*`. Mutating admin routes (`/chaos/inject`, `/resilience/reset`, `/cache/clear`) use `verify_admin_key` (`X-Admin-Key`).
- `graph/builder.py` — the topology and routers. Flow:
  `analyze_query → collect_telemetry →(route_after_telemetry)→ [compensate_blindspots → degraded_fallback →] synthesize_telemetry → generate_report → validate_report →(route_after_validation)→ refine | repair | fallback | finalize`.
  `self_repair` and `refine_reflection` loop back to `generate_report`.
- `graph/nodes/` — one node per file, `<name>_node(state, runtime: Runtime[TriageDeps])` when it needs
  collaborators, else `<name>_node(state)`; returns a partial state update `dict[str, Any]` and reads
  collaborators only from `runtime.context`.
- `runtime.py` — `AppRuntime` (cache, breakers, chaos, quota, `TriageDeps`), built by `build_runtime()`; one per app.
- `graph/llm.py` — Groq client, `MockTriageChatModel`, `build_triage_llm()`, quota tracker / 429 failover.
- `cache/semantic_cache.py` — `SemanticCacheManager` (Redis ↔ in-memory failover). `cache/embeddings.py` — providers.
- `resilience/` — `CircuitBreakerRegistry`, retry decorators. `telemetry/` — Statuspage probe, `ChaosManager`.
- `schemas/` — `incident.py` (report contract), `state.py` (`TriageState`), `api.py`, `telemetry.py`.
- `config.py` — single `settings` object; every tunable is a `Field(..., description=...)` with bounds.

Deeper docs (load only the one relevant to the task): `docs/architecture.md`, `docs/resilience.md`,
`docs/schemas.md`, `docs/api.md`, `docs/deployment.md`, ADRs in `docs/decisions/`.

## Conventions
Code conventions live in `CODING_STANDARDS.md`; read it before adding a module, node, config
field or external call. Commits: Conventional Commits with scope (`fix(resilience): …`).

## Testing
- `tests/conftest.py`'s only autouse fixture shortens retry waits. The `runtime` fixture builds a fresh `AppRuntime`
  (mock LLM, canned Statuspage, emptied cache) per test. Stateful modules are members of `AppRuntime` (`runtime.py`).
  Don't add module-level singletons; tests get a fresh runtime per test.
- Use the `client` fixture (httpx `ASGITransport` + app lifespan) for API tests; never hit real Groq/Statuspage in tests.
- `asyncio_mode = "auto"` — async tests need no marker.

## Boundaries
- Never commit `.env` or API keys (`GROQ_API_KEY`, `ADMIN_API_KEY`).
- Don't add dependencies without asking; keep the Docker image non-root.
- `requires_approval=True` on non-diagnostic actions is written only by `reconcile`
  (`graph/reconciliation.py`); keep that invariant intact.
- Ask before changing the `IncidentTriageReport` schema or the cache key/index format (`triage_vector_idx`, `triage:doc:*`) — both are contracts.
- Don't skip, delete, or loosen tests/assertions to get green.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `SamSilmarilData/resilient-triage` (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` (created lazily) + ADRs in `docs/decisions/`. See `docs/agents/domain.md`.

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
- Setup: `python3.12 -m venv .venv && .venv/bin/pip install -e ".[test,lint]"`
- Test: `pytest -q` (124 tests, ~4s; Redis optional — falls back to in-memory)
- Lint: `ruff check . && ruff format --check .`
- Types: `mypy src/`
- Run: `./scripts/run_local.sh` (UI :8000, docs /docs, /health)
- Docker: `docker compose up`; smoke test: `python scripts/smoke_test.py`
- CI (`.github/workflows/ci.yml`) runs exactly: ruff check, ruff format --check, mypy src/, pytest, docker build + /health

Baseline (2026-10-29): all four checks green.

## Project Map (`src/resilient_triage/`)
- `api/app.py` — routes: `POST /triage`, `/health`, `/metrics`, `/resilience/status`, `/llm/quota`,
  `/chaos/*`. Mutating admin routes (`/chaos/inject`, `/resilience/reset`, `/cache/clear`) use `verify_admin_key` (`X-Admin-Key`).
- `graph/builder.py` — the topology and routers. Flow:
  `analyze_query → collect_telemetry →(route_after_telemetry)→ [compensate_blindspots → degraded_fallback →] synthesize_telemetry → generate_report → validate_report →(route_after_validation)→ refine | repair | fallback | finalize`.
  `self_repair` and `refine_reflection` loop back to `generate_report`.
- `graph/nodes/` — one node per file, `<name>_node(state: TriageState) -> dict[str, Any]` returning a partial state update.
- `graph/llm.py` — Groq client, `MockTriageChatModel`, `set_triage_llm()`, quota tracker / 429 failover.
- `cache/semantic_cache.py` — `SemanticCacheManager` (Redis ↔ in-memory failover). `cache/embeddings.py` — providers.
- `resilience/` — `circuit_breaker_registry`, retry decorators. `telemetry/` — Statuspage probe, `chaos_manager`.
- `schemas/` — `incident.py` (report contract), `state.py` (`TriageState`), `api.py`, `telemetry.py`.
- `config.py` — single `settings` object; every tunable is a `Field(..., description=...)` with bounds.

Deeper docs (load only the one relevant to the task): `docs/architecture.md`, `docs/resilience.md`,
`docs/schemas.md`, `docs/api.md`, `docs/deployment.md`, ADRs in `docs/decisions/`.

## Conventions
- Enums inherit `(str, Enum)` (this is why ruff `UP042` is ignored — don't "fix" it to `StrEnum`).
- `logger = logging.getLogger(__name__)` per module; %-style log args, not f-strings.
- Module docstring on every file (`"""Node: x - does y."""` for nodes); function docstrings one line.
- New config goes in `config.py` as a bounded `Field` **and** in `.env.example`.
- External calls (HTTP, Redis, LLM) must be guarded: breaker and/or retry, and a degraded path that still yields a valid `IncidentTriageReport`.
- Every non-diagnostic action must end with `requires_approval=True` — enforced in `reconcile` (`graph/reconciliation.py`), the only code that writes it; don't weaken it.
- Line length 100 (ruff); full type hints on new code.
- Commits: Conventional Commits with scope — `feat(graph): …`, `fix(resilience): …`, `test(suite): …`. Update `CHANGELOG.md` for user-visible changes.

## Testing
- `tests/conftest.py` has an autouse fixture that resets **all** singletons (breakers, chaos, quota,
  Statuspage cache, semantic cache) and installs `MockTriageChatModel`. Any new module-level singleton must be reset there too.
- Use the `client` fixture (httpx `ASGITransport` + app lifespan) for API tests; never hit real Groq/Statuspage in tests.
- `asyncio_mode = "auto"` — async tests need no marker.

## Boundaries
- Never commit `.env` or API keys (`GROQ_API_KEY`, `ADMIN_API_KEY`).
- Don't add dependencies without asking; keep the Docker image non-root.
- Ask before changing the `IncidentTriageReport` schema or the cache key/index format (`triage_vector_idx`, `triage:doc:*`) — both are contracts.
- Don't skip, delete, or loosen tests/assertions to get green.

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `SamSilmarilData/resilient-triage` (via `gh`). See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: root `GLOSSARY.md` (created lazily) + ADRs in `docs/decisions/`. See `docs/agents/domain.md`.

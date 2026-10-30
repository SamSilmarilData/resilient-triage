# Coding standards

Judgement-call rules for code in this repo, read at review time. Mechanical rules live in
tooling instead: `./scripts/check.sh` enforces line length 100 (ruff `E501`; `graph/prompts.py`
is exempt because prompt text goes to the LLM verbatim), formatting, and full type hints in
`src/` (mypy `disallow_untyped_defs`).

## Modules and naming
- Module docstring on every file. Graph nodes use `"""Node: <name> - <what it does>."""`.
- Enums inherit `(str, Enum)`, matching the existing schemas (ruff `UP042` is ignored for this).

## Docstrings
- Function and method docstrings are one line.
- Public API routes (`api/app.py`) and registry methods that already carry a Google-style
  `Args:`/`Returns:` block keep it and keep it accurate; new functions use one line.

## Logging
- `logger = logging.getLogger(__name__)` per module.
- %-style log arguments (`logger.warning("x failed: %s", exc)`), so formatting is lazy.

## Types in tests
- Test files follow the surrounding file: annotate helpers where the file already does.

## Configuration
- New config goes in `config.py` as a bounded `Field(..., description=...)` **and** in
  `.env.example`.

## Resilience
- Every external call (HTTP, Redis, LLM) is guarded by a breaker and/or retry, and has a
  degraded path that still yields a valid `IncidentTriageReport`.

## Frontend
The page is build-free and vendored (ADR-010). No JS lint or type-check runs in CI, so these
rules stand in for one:
- JS logic lives in the one reducer module (the trace-to-view reducer). Components only render
  its output and wire events.
- Every JS file starts with `// @ts-check` and uses JSDoc types.
- New JS logic needs a browser test (`@pytest.mark.browser`, run by `./scripts/check.sh --browser`).
- No third-party origins: the page loads scripts, styles, fonts and data from its own origin only.
- No new vendored package, runtime or test-only, without approval.

## Changelog
- User-visible changes get a `CHANGELOG.md` entry under `[Unreleased]`.

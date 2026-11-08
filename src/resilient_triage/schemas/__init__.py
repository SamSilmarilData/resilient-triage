"""Pydantic schemas: the report contract, run state, API and telemetry models.

Import from the submodules (`schemas.incident`, `schemas.state`, `schemas.api`,
`schemas.telemetry`). This package re-exports nothing: `schemas.api` carries a
`trace.summary.TraceSummary`, and `trace` imports `schemas.incident`, so any eager import here
would close an import cycle (`trace.summary` -> `schemas` -> ... -> `schemas.api` ->
`trace.summary`).
"""

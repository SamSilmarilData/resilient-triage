"""Execution trace (ADR-008): trace events, recorders and the trace summary.

Import from the submodules (`trace.events`, `trace.recorder`, `trace.summary`). This package
re-exports nothing, so `schemas.api` can import `trace.summary` while `trace.events` imports
`schemas.api`, without an import cycle.
"""

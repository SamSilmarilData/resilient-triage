#!/usr/bin/env bash
# Run the checks CI's test job runs: ruff, ruff format, mypy and pytest.
# Extra arguments go to pytest, e.g. `./scripts/check.sh tests/test_api.py`.
# `--browser` runs only the browser tier (CI's browser job) instead: it needs the `browser`
# extra and `playwright install chromium`, and fails, never skips, when either is missing.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/venv-bin.sh
source scripts/venv-bin.sh

if [[ "${1:-}" == "--browser" ]]; then
  shift
  exec "${bin}pytest" -q -m browser tests/browser "$@"
fi

"${bin}ruff" check .
"${bin}ruff" format --check .
"${bin}mypy" src/
"${bin}pytest" -q "$@"

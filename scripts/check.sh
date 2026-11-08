#!/usr/bin/env bash
# Run the checks CI's test job runs: ruff, ruff format, mypy and pytest.
# Extra arguments go to pytest, e.g. `./scripts/check.sh tests/test_api.py`.
# `--browser` runs only the browser tier (CI's browser job) instead: it needs the `browser`
# extra and `playwright install chromium`, and fails, never skips, when either is missing.
# `--package` runs only scripts/check_package.sh: CI's docker job's checks on the installed
# wheel, without Docker (the image's OS layer and non-root user stay CI-only).
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/venv-bin.sh
source scripts/venv-bin.sh

if [[ "${1:-}" == "--browser" ]]; then
  shift
  exec "${bin}pytest" -q -m browser tests/browser "$@"
fi

if [[ "${1:-}" == "--package" ]]; then
  exec scripts/check_package.sh
fi

"${bin}ruff" check .
"${bin}ruff" format --check .
"${bin}mypy" src/
"${bin}pytest" -q "$@"

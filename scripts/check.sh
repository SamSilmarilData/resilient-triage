#!/usr/bin/env bash
# Run the checks CI's test job runs: ruff, ruff format, mypy and pytest.
# Extra arguments go to pytest, e.g. `./scripts/check.sh tests/test_api.py`.
set -euo pipefail
cd "$(dirname "$0")/.."
# shellcheck source=scripts/venv-bin.sh
source scripts/venv-bin.sh

"${bin}ruff" check .
"${bin}ruff" format --check .
"${bin}mypy" src/
"${bin}pytest" -q "$@"

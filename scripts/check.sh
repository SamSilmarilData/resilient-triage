#!/usr/bin/env bash
# Run the checks CI's test job runs: ruff, ruff format, mypy and pytest.
# Extra arguments go to pytest, e.g. `./scripts/check.sh tests/test_api.py`.
set -euo pipefail
cd "$(dirname "$0")/.."
bin=""
[[ -x .venv/bin/python ]] && bin=".venv/bin/"

"${bin}ruff" check .
"${bin}ruff" format --check .
"${bin}mypy" src/
"${bin}pytest" -q "$@"

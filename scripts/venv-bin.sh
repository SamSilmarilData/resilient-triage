# Sourced by scripts/check.sh and .githooks/pre-commit, from the checkout's root.
# Sets $bin to this checkout's .venv/bin/, else the main checkout's (so a git worktree
# without its own venv still finds ruff/mypy/pytest), else "" to use PATH. Exports
# PYTHONPATH so any Python process, subprocesses included, imports this checkout's src/.
bin=""
common="$(git rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
for venv in .venv "${common:+$common/../.venv}"; do
  if [[ -n "$venv" && -x "$venv/bin/python" ]]; then
    bin="$venv/bin/"
    break
  fi
done
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"

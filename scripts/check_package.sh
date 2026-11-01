#!/usr/bin/env bash
# Check the installed package the way CI's docker job checks the image, without Docker:
# build the wheel, install it (non-editable) into a fresh venv outside the repo, verify its
# vendored static files, boot it with uvicorn on a free loopback port and run the smoke test.
#
# It runs offline-capable: no Redis (REDIS_URL points at a closed loopback port, so the
# in-memory cache serves) and no Groq key (the scripted LLM answers), from a cwd with no .env.
# Building the wheel and installing its dependencies still use pip's index or cache, and the
# app's first start may download the FastEmbed model if it isn't cached yet.
#
# Not covered here: the image's OS layer (python:3.12-slim, apt packages, embedded Redis via
# docker-entrypoint.sh) and its non-root user. Only CI's docker job checks those.
set -euo pipefail
repo="$(cd "$(dirname "$0")/.." && pwd)"
cd "$repo"
# shellcheck source=scripts/venv-bin.sh
source scripts/venv-bin.sh
# The installed package must stand alone: never let this checkout's src/ shadow it.
unset PYTHONPATH
python="${bin}python"
[[ -x "$python" ]] || python="$(command -v python3.12 || command -v python3)"

work="$(mktemp -d "${TMPDIR:-/tmp}/check-package.XXXXXX")"
server_pid=""
cleanup() {
  if [[ -n "$server_pid" ]] && kill -0 "$server_pid" 2>/dev/null; then
    kill "$server_pid" 2>/dev/null || true
    wait "$server_pid" 2>/dev/null || true
  fi
  rm -rf "$work"
}
trap cleanup EXIT

step() { printf '\n==> %s\n' "$1"; }

step "Build the wheel"
"$python" -m pip wheel --quiet --no-deps --wheel-dir "$work/dist" "$repo"
wheel="$(ls "$work"/dist/resilient_triage-*.whl)"
echo "$(basename "$wheel")"

step "Install it into a fresh venv (non-editable, outside the repo)"
"$python" -m venv "$work/venv"
venv="$work/venv/bin"
"$venv/python" -m pip install --quiet --disable-pip-version-check "$wheel"

# From here on, run from an empty dir: src/ isn't importable and no .env is read.
mkdir "$work/run"
cd "$work/run"

step "Verify the installed vendored static files"
installed='import resilient_triage as p, sys; sys.exit(not p.__file__.startswith(sys.prefix))'
"$venv/python" -c "$installed" || { echo "resilient_triage isn't the venv's installed copy" >&2; exit 1; }
"$venv/python" "$repo/scripts/vendor.py" verify --installed

step "Boot the installed app on a free loopback port"
free_port() {
  "$venv/python" -c 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
}
port="$(free_port)"
url="http://127.0.0.1:$port"
admin_key="$("$venv/python" -c 'import secrets; print(secrets.token_urlsafe(32))')"
env -u GROQ_API_KEY \
  ADMIN_API_KEY="$admin_key" \
  REDIS_URL="redis://127.0.0.1:$(free_port)/0" \
  "$venv/uvicorn" resilient_triage.api.app:app --host 127.0.0.1 --port "$port" \
  >"$work/server.log" 2>&1 &
server_pid=$!
# Startup may download the embedding model, so poll /health instead of a fixed sleep.
deadline=$((SECONDS + 120))
until "$venv/python" -c 'import sys, urllib.request; urllib.request.urlopen(sys.argv[1], timeout=5)' \
  "$url/health" 2>/dev/null; do
  if ! kill -0 "$server_pid" 2>/dev/null || ((SECONDS >= deadline)); then
    echo "The app did not become healthy at $url; its log:" >&2
    cat "$work/server.log" >&2
    exit 1
  fi
  sleep 1
done
echo "healthy at $url"

step "Run the smoke test against it"
if ! "$venv/python" "$repo/scripts/smoke_test.py" --url "$url" --admin-key "$admin_key"; then
  echo "The server's log:" >&2
  cat "$work/server.log" >&2
  exit 1
fi

cat <<'EOF'

Package check passed: the wheel installs, carries every vendored file, boots and passes
the smoke test (in-memory cache, scripted LLM).
Not covered: the Docker image's OS layer and its non-root user. CI's docker job checks those.
EOF

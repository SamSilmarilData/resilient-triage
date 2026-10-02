#!/usr/bin/env bash
# ==============================================================================
# Native macOS Local Runner for Resilient Triage (Zero Docker Desktop Needed)
# ==============================================================================

set -e

DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )/.." && pwd )"
cd "$DIR"

# Check for virtualenv
if [ ! -d ".venv" ]; then
    echo "Creating Python 3.12 virtual environment..."
    python3.12 -m venv .venv
    source .venv/bin/activate
    pip install -e ".[test]"
else
    source .venv/bin/activate
fi

# Auto-detect and launch Redis if needed
REDIS_STATUS="⚠️  Disconnected (In-Memory Fallback Active)"
if command -v redis-server >/dev/null 2>&1; then
    if ! .venv/bin/python -c "import redis; r=redis.from_url('redis://127.0.0.1:6379/0'); r.ping()" >/dev/null 2>&1; then
        echo "Starting local Redis daemon via Homebrew..."
        brew services start redis >/dev/null 2>&1 || redis-server --daemonize yes >/dev/null 2>&1 || true
        sleep 0.5
    fi
    if .venv/bin/python -c "import redis; r=redis.from_url('redis://127.0.0.1:6379/0'); r.ping()" >/dev/null 2>&1; then
        REDIS_STATUS="🟢 Connected (Native Redis 8 / RediSearch on :6379)"
    fi
fi

# Print banner and links
echo "========================================================================"
echo "🛡️  Starting Resilient Triage Incident Gateway (Native macOS)"
echo "========================================================================"
echo "• SRE Command Center UI : http://127.0.0.1:8000"
echo "• Interactive API Docs  : http://127.0.0.1:8000/docs"
echo "• Health Endpoint       : http://127.0.0.1:8000/health"
echo "• Redis Cache Engine    : $REDIS_STATUS"
echo "========================================================================"
echo "Press Ctrl+C to stop."
echo ""

exec uvicorn resilient_triage.api.app:app --host 127.0.0.1 --port 8000 --reload

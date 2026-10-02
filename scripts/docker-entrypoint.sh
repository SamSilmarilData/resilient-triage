#!/bin/sh
set -e

# ==============================================================================
# Dual-Mode Docker Entrypoint for Resilient Triage
# 
# Mode 1 (Self-Contained): If REDIS_URL is unset or points to localhost/127.0.0.1,
#                          boot embedded Redis daemon inside the container.
# Mode 2 (External DB):    If REDIS_URL points to an external host, connect directly.
# ==============================================================================

if [ -z "$REDIS_URL" ] || echo "$REDIS_URL" | grep -qE "127\.0\.0\.1|localhost"; then
    echo "🛡️ Starting embedded Redis cache daemon (maxmemory 128mb, allkeys-lru)..."
    redis-server \
        --daemonize yes \
        --port 6379 \
        --maxmemory 128mb \
        --maxmemory-policy allkeys-lru \
        --save "" \
        --appendonly no \
        --loglevel warning

    # Wait up to 2 seconds for redis-server to respond
    for i in $(seq 1 10); do
        if redis-cli ping > /dev/null 2>&1; then
            echo "✅ Embedded Redis is ready (PONG)."
            break
        fi
        sleep 0.2
    done
    export REDIS_URL="redis://127.0.0.1:6379/0"
else
    echo "📡 Using external Redis connection: $REDIS_URL"
fi

# Exec uvicorn as PID 1 to gracefully handle termination signals (SIGTERM/SIGINT)
exec uvicorn resilient_triage.api.app:app --host 0.0.0.0 --port "${PORT:-8000}"

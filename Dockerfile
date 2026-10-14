FROM python:3.12-slim AS base

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    build-essential \
    redis-server \
    redis-tools \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml README.md ./
COPY src/ src/
COPY scripts/ scripts/

RUN chmod +x scripts/*.sh && pip install --upgrade pip && pip install .

RUN groupadd -g 10001 appuser && \
    useradd -u 10001 -g appuser -m -s /bin/bash appuser && \
    mkdir -p /var/run/redis /var/lib/redis /var/log/redis && \
    chown -R appuser:appuser /app /var/run/redis /var/lib/redis /var/log/redis

USER appuser

EXPOSE 8000 6379

HEALTHCHECK --interval=15s --timeout=5s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:${PORT:-8000}/health || exit 1

ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]

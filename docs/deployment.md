# Deployment & Free Cloud Hosting Guide

This guide details how to deploy and host `resilient-triage` completely for free ($0.00 infrastructure cost) with **real SOTA AI generation** and **live Redis semantic caching**.

---

## Architecture Highlights for Zero-Cost Hosting

1. **Embedded Live Redis Container**: The [`Dockerfile`](../Dockerfile) packages `redis-server` alongside FastAPI. When deployed to single-container platforms (Hugging Face Spaces, Render Free, Railway), [`scripts/docker-entrypoint.sh`](../scripts/docker-entrypoint.sh) automatically boots embedded Redis with `maxmemory 128mb` and `allkeys-lru` eviction.
2. **External Cloud Redis Override**: If `REDIS_URL` is set to an external cloud database (such as Upstash `rediss://...`), the entrypoint script automatically skips internal Redis and connects directly.
3. **Graceful Vector Fallback**: If Redis is unreachable or crashes, `SemanticCacheManager` seamlessly falls back to sub-millisecond in-memory vectorized search with zero dropped requests.
4. **CPU-Optimized Embeddings**: FastEmbed uses ONNX runtime (`bge-small-en-v1.5`), generating 384-dim embeddings in ~3.5ms on standard CPU with ~150MB RAM.
5. **Ultra-Fast SOTA Generation**: Groq LPUs provide 14,400 free daily requests with sub-2s turnaround times for flagship 27B models.

---

## Option 1: Deploy to Hugging Face Spaces (Recommended - 100% Free Forever)

Hugging Face Spaces offers a **Free Docker Space** with **2 vCPUs and 16 GB of RAM**, custom public HTTPS URLs, and zero sleep timeouts.

### Step 1: Create a Space on Hugging Face
1. Go to [huggingface.co/spaces](https://huggingface.co/spaces) and click **Create new Space**.
2. Set Space Name: `resilient-triage`.
3. Select **Space SDK**: **Docker** (Blank).
4. Set Hardware: **CPU Basic • 2 vCPU • 16 GB RAM • Free**.
5. Select Visibility: **Public**.

### Step 2: Push Repository to the Space
Clone your Space repo and push `resilient-triage` to it:
```bash
git remote add space https://huggingface.co/spaces/<YOUR_USERNAME>/resilient-triage
git push space main
```

### Step 3: Add the Groq API Key Secret
1. In your Hugging Face Space, click **Settings** -> **Variables and secrets**.
2. Under **New secret**, add:
   - Key: `GROQ_API_KEY`
   - Value: `gsk_...` (your Groq key)
   - Optionally, a second secret `ADMIN_API_KEY` (a long random string) to enable the admin routes for yourself. Without it they answer `403` (see [Admin key](#admin-key)).
3. Your Space will build and deploy automatically!
4. **Embedded Redis boots automatically**: The container initializes `redis-server` on port 6379, and `GET /health` reports `"redis_connected": true`!
5. Anyone visiting `https://<YOUR_USERNAME>-resilient-triage.hf.space` gets the full SRE Command Center UI with live SOTA 27B inference in ~1.5s, live Redis vector caching, and zero logins or keys required!

---

## Option 2: Deploy to Render.com (Free Web Service)

Render allows 1-click deployment connected to your GitHub repository.

### Step 1: Create a New Web Service
1. Go to [dashboard.render.com](https://dashboard.render.com) and click **New +** -> **Web Service**.
2. Connect your GitHub repository: `SamSilmarilData/resilient-triage`.
3. Choose **Docker** as the Environment.
4. Select the **Free** instance type.

### Step 2: Configure Environment Variables
Under the **Environment Variables** tab, add:
- `GROQ_API_KEY`: `gsk_...`
- `ADMIN_API_KEY` (optional): a long random string that enables the admin routes. Unset, they answer `403` (see [Admin key](#admin-key)).
- `CACHE_SIMILARITY_THRESHOLD`: `0.90`
- `TRIAGE_RUN_DEADLINE_S` (optional, default `45.0`, above 0 and at most 120): the run deadline. A graph run that takes longer is cancelled and the request answered with an uncached `SEVERELY_DEGRADED` fallback report. Keep it below your platform's request timeout so the client gets that report rather than a proxy timeout. Slot wait (`TRIAGE_QUEUE_TIMEOUT_S`) is not included.
- `FREE_PLAY_RUNS_PER_MINUTE` (optional, default `20`, 1 to 10000): free-play graph runs admitted per sliding 60 s; a run over it gets a 503 with `Retry-After`. Cache hits and scenario runs are free. The budget is per process: with several workers or replicas, each allows this many. `SCENARIO_MAX_CONCURRENT_RUNS` (optional, default `4`, 1 to 100) bounds scenario runs in flight, separately from `TRIAGE_MAX_CONCURRENT_RUNS`.
- `TELEMETRY_SOURCE_DEADLINE_S` (optional, default `5.0`, 0.5 to 30): the source deadline. One Telemetry source read (Statuspage or Cluster Sentinel), retries included, that takes longer is cut off, counted as a breaker failure, and the source is reported unavailable for that run. Keep it well below `TRIAGE_RUN_DEADLINE_S`.

### Step 3: Deploy
Click **Create Web Service**. Render will build the container and provide a live public HTTPS URL (`https://resilient-triage.onrender.com`).
The embedded Redis daemon automatically starts in the background and is capped at 128MB to stay well within Render's 512MB RAM ceiling.

---

## Option 3: Multi-Container Docker Compose

For VPS, AWS EC2, or local container runtimes:

```bash
export ADMIN_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"  # or set it in .env
docker-compose up -d --build
```
- API Container: `http://localhost:8000`
- Redis Container: `redis/redis-stack-server:latest` on port `6379`
- Shared network connects API to `redis://redis:6379/0` automatically.

---

## Option 4: Native Local Execution on macOS (Zero Docker Desktop)

For local development or testing on a MacBook:

```bash
# 1. Install Redis via Homebrew (one-time)
brew install redis && brew services start redis

# 2. Create .env from the template; it sets ADMIN_API_KEY (change the value)
cp .env.example .env

# 3. Run the native startup script
./scripts/run_local.sh
```

- SRE Command Center UI: `http://127.0.0.1:8000`
- Interactive API Docs: `http://127.0.0.1:8000/docs`
- Health Endpoint: `http://127.0.0.1:8000/health`

---

## Admin key

The mutating admin routes (`POST /chaos/inject`, `/resilience/reset`, `/cache/clear`) fail closed: they answer `403` until `ADMIN_API_KEY` is set, and then need a matching `X-Admin-Key` header (`401` otherwise). Every other route, `/triage` included, needs no key.

- **Set it** in `.env` locally (`.env.example` has a placeholder), or as a secret / environment variable on the host. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
- **Use it** from `/docs`: click **Authorize**, paste the key, and Swagger UI sends it on the admin routes. Or send the header yourself: `curl -X POST <url>/resilience/reset -H "X-Admin-Key: $ADMIN_API_KEY"`.
- **Smoke test**: `python scripts/smoke_test.py` reads `ADMIN_API_KEY` (or `--admin-key`). Against a running server it must match that server's key; an instance it launches itself gets a random key if none is set.

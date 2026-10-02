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
- `CACHE_SIMILARITY_THRESHOLD`: `0.90`

### Step 3: Deploy
Click **Create Web Service**. Render will build the container and provide a live public HTTPS URL (`https://resilient-triage.onrender.com`).
The embedded Redis daemon automatically starts in the background and is capped at 128MB to stay well within Render's 512MB RAM ceiling.

---

## Option 3: Multi-Container Docker Compose

For VPS, AWS EC2, or local container runtimes:

```bash
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

# 2. Run the native startup script
./scripts/run_local.sh
```

- SRE Command Center UI: `http://127.0.0.1:8000`
- Interactive API Docs: `http://127.0.0.1:8000/docs`
- Health Endpoint: `http://127.0.0.1:8000/health`

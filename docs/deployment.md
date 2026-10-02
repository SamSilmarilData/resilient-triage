# Deployment & Free Cloud Hosting Guide

This guide details how to deploy and host `resilient-triage` completely for free ($0.00 infrastructure cost) with real SOTA AI generation.

---

## Architecture Highlights for Zero-Cost Hosting

1. **In-Memory Vector Search**: When Redis is not provisioned, `SemanticCacheManager` automatically runs an in-memory NumPy vector cache. You do not need to pay for or provision a cloud database.
2. **CPU-Optimized Embeddings**: FastEmbed uses ONNX runtime (`bge-small-en-v1.5`), generating 384-dim embeddings in ~3.5ms on standard CPU with ~150MB RAM.
3. **Ultra-Fast SOTA Generation**: Groq LPUs provide 14,400 free daily requests with sub-2s turnaround times for flagship 27B / 120B models.

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
4. Anyone visiting `https://<YOUR_USERNAME>-resilient-triage.hf.space` gets the full SRE Command Center UI with live SOTA 27B inference in ~1.5s with zero logins or keys required!

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

---

## Option 3: Native Local Execution on macOS (Zero Docker Desktop)

For local development or testing on a MacBook:

```bash
# 1. Clone repository
git clone https://github.com/SamSilmarilData/resilient-triage.git
cd resilient-triage

# 2. Run the native startup script
./scripts/run_local.sh
```

- SRE Command Center UI: `http://127.0.0.1:8000`
- Interactive API Docs: `http://127.0.0.1:8000/docs`
- Health Endpoint: `http://127.0.0.1:8000/health`

---

## Option 4: Local Open-Source Models via Ollama (Zero External APIs)

If you prefer to run completely offline without contacting any external cloud APIs:

```bash
# 1. Install Ollama on macOS
brew install ollama

# 2. Pull Qwen 2.5 or DeepSeek-R1
ollama run qwen2.5:1.5b

# 3. Start resilient-triage
./scripts/run_local.sh
```
`resilient-triage` will automatically detect your local Ollama instance on `http://localhost:11434` and run on your Mac's Apple Silicon GPU.

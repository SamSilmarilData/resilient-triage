# 🎬 SRE Command Center: Interactive Demo Guide & Presentation Script (v1.1.0)

This guide provides the complete narrative, technical depth, and step-by-step presentation script for showcasing `resilient-triage` to recruiters, engineering managers, teammates, and friends.

---

## 🏛️ Executive Pitch (The 30-Second Hook)

> *"During major infrastructure outages, monitoring tools, status pages, and telemetry APIs degrade or crash simultaneously. Naive AI bots that make synchronous HTTP calls crash on dead sockets or hit rate limits.*
> 
> *`resilient-triage` is an autonomous SRE incident triage gateway engineered to **never hang, never crash, and gracefully compensate** when upstream systems are on fire. It combines **LangGraph cyclic state machines**, **Groq LPUs**, **Redis 8 RediSearch HNSW vector caching**, and **Pybreaker circuit breakers** to deliver sub-2ms cache hits and guaranteed sub-1.3s fault-tolerant incident reports."*

---

## ⏱️ Step-by-Step Presentation Script (The 5 Acts)

### Act 1: The Cold Incident Triage (Live Groq LPU Generation) — ~30s
* **Goal**: Demonstrate autonomous telemetry synthesis and structured root-cause analysis on a live cache miss.
* **Action**:
  1. Open the demo dashboard at `/` (or your live Render Singapore URL).
  2. Click the quick preset **💥 Actions 503 Outage** (or press `Cmd+Enter`).
* **What Happens**:
  - The status machine queries the in-memory 10s Statuspage cache.
  - LangGraph compiles telemetry signals and dispatches to Groq LPU (`qwen/qwen3.8-27b`).
  - Output strictly validates against Pydantic schema in **~1.2 seconds**.
  - Telemetry Banner pulses violet: `🔍 X-Cache: MISS • Server: 1.27s (Groq LPU) • Net RTT: ~80ms`.
* **What to Say**:
  > *"Notice the violet badge: `X-Cache: MISS`. The agent synthesized status signals, inferred downstream pipeline impact, and produced prioritized remediative action items with 1-click copyable bash commands in 1.2 seconds on Groq LPUs."*

---

### Act 2: The Instant Semantic Cache Hit (<2ms Turnaround) — ~20s
* **Goal**: Demonstrate instant semantic offloading using Redis 8 RediSearch HNSW vector search.
* **Action**:
  1. Without refreshing, click **Run Triage** again (or ask a synonymous query like *"GitHub Actions workflows are timing out and failing with 503 errors"*).
* **What Happens**:
  - Dense 384-dimensional query vector is retrieved from the in-memory LRU cache in **0.001ms**.
  - Redis HNSW KNN index matches the previous incident report with $\ge 95\%$ cosine similarity.
  - Telemetry Banner turns emerald green: `⚡ X-Cache: HIT • Server: 1.16ms (Redis HNSW) • Net RTT: ~78ms (79ms total)`.
* **What to Say**:
  > *"When an incident strikes, hundreds of engineers flood Slack asking the exact same question. Look at the banner now: `X-Cache: HIT` in **1.16 milliseconds**. Redis HNSW vector caching intercepts redundant traffic instantly, offloading 100% of LLM compute costs."*

---

### Act 3: The Dual-Metric Telemetry Explainer — ~20s
* **Goal**: Demonstrate production-grade observability and explain WAN network distance vs server execution.
* **Action**:
  1. Point to the glowing banner and the bottom-right **Audit Log Ticker**: `POST /triage 200 (Srv: 1ms | Net: 78ms)`.
* **What to Say**:
  > *"Our cloud demo is deployed in Render's Singapore region. We deliberately instrumented **Dual-Metric Telemetry** to separate server execution from physical internet WAN transit.*
  > 
  > *While undersea fiber optic transit across the Indian Ocean adds ~75ms of network round-trip time, our backend Redis vector engine responds in single-digit milliseconds. Recruiters and SREs see complete transparency across the stack."*

---

### Act 4: Upstream Outage Chaos & Circuit Breaker Fail-Fast — ~40s
* **Goal**: Prove that the agent never hangs or drops requests when external dependencies crash.
* **Action**:
  1. In the **Chaos & Resilience Controller** (bottom-left panel), toggle **Inject 503 Failures** to ON.
  2. Click **⚡ Trip Breaker** (simulating 3 consecutive upstream 503 failures).
  3. Look at the top navbar: `chaos` circuit breaker flips to **`OPEN`** (red pulse).
  4. Now click **Run Triage** again with the circuit breaker OPEN.
* **What Happens**:
  - The request finishes in ~1.2s without hanging on dead sockets.
  - The report severity displays **`PARTIALLY_DEGRADED`**.
  - Under `Circuit Breakers Tripped`, it clearly records `["chaos"]`.
  - The LangGraph state machine automatically routed through `DegradedFallbackNode`, synthesizing available signals and annotating the blind spots.
* **What to Say**:
  > *"Look at that: zero timeouts, zero HTTP 500 errors, zero crashes.*
  > 
  > *If this were a naive LLM wrapper, it would have hung on a dead socket for 30 seconds. Instead, Pybreaker intercepted the failure, tripped the circuit open, and LangGraph gracefully routed through a compensatory fallback node, explicitly warning on-call responders of telemetry blind spots."*

---

### Act 5: Clean Recovery & Reset — ~20s
* **Goal**: Demonstrate zero-touch recovery to 100% system health.
* **Action**:
  1. Click **`🔄 Reset Circuits`** in the chaos controller.
  2. Point to the navbar returning to `HEALTHY` and `CLOSED`.
* **What to Say**:
  > *"When upstream systems recover, resetting the circuit immediately restores full state machine capabilities. The entire system is backed by **53 automated unit and integration tests** passing in 4 seconds.*
  > 
  > *Thanks for watching, and I'd love to answer any questions about the architecture!"*

---

## 💡 Quick Q&A Reference for Technical Evaluators

| Question | Answer |
| :--- | :--- |
| **"What model are you running?"** | `qwen/qwen3.8-27b` on Groq Language Processing Units for sub-1.3s inference, with automated fallback ladders to Ollama, Google Gemini, and OpenAI. |
| **"How does the semantic cache work?"** | `FastEmbed` ONNX models generate 384-dim dense vectors. Vectors are indexed via Redis 8 RediSearch HNSW Float32 Cosine index (`triage_vector_idx`). Repeat queries are accelerated by an in-memory 512-entry LRU cache. |
| **"What happens if Redis goes down?"** | `SemanticCacheManager` intercepts connection drops and instantaneously fails over to an in-memory NumPy vector matrix with 0 dropped requests and 0 HTTP 500 errors. Background probes auto-reconnect when Redis returns. |
| **"What if the LLM emits invalid JSON?"** | LangGraph executes an automated Pydantic self-repair loop: catching `ValidationError`, extracting the exact invalid field paths, and prompting the LLM with the traceback to iteratively self-correct within 2 retries. |

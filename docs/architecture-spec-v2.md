# Architecture Specification — V2

> **Status:** Design proposal. Not yet implemented.
> **Relationship to V1:** V2 **replaces** the current `api/` (SHA-256 CPU burn) with a ticker aggregator. The dashboard infrastructure, K8s patterns, and scripts are **extended**, not rebuilt.

## 1. Motivation & Narrative

V1 demonstrates one thing well: **HPA autoscaling on a CPU-bound HTTP workload**, visualized in real time. It's a clean, self-contained demo of one scaling pattern.

V2 expands the scope to **multi-workload autoscaling** — three distinct autoscaling patterns operating in parallel on a single system, each responding to a different pressure signal:

| Autoscaler | What scales | Signal | Behavior |
|-----------|-------------|--------|----------|
| **HPA** | API pods | CPU utilization | 1 → 10 (gradual) |
| **KEDA** | Queue workers | Redis queue depth | **0 → 5** (scale-to-zero) |
| **CronJob** | Pre-warmer pods | Wall clock (every 30s) | 0 → 1 (ephemeral, ~5s lifetime) |

### 1.1 Secondary theme: resilience through caching

The system simulates a **real third-party dependency** (rate-limited, sometimes slow or failing). A Redis cache sits in front, and a scheduled pre-warmer keeps hot symbols fresh. The result is a demo where:

- Healthy state → most reads are `FRESH_HIT`, ~zero queue pressure, workers scaled to zero
- Third-party degrades → reads shift to `STALE_HIT`, queue fills, KEDA scales workers up
- Pre-warmer paused → hit rate drops, `COLD_MISS` rises, system stress becomes visible

### 1.2 Non-goals

- Not a benchmark of Redis, Arq, or KEDA in isolation.
- Not a generic microservices tutorial.
- Not a production-ready reference architecture (explicitly: single Redis, no HA, no TLS, no auth).
- Not an exhaustive showcase of autoscaling metrics (no external metrics like Prometheus scalers, no VPA, no cluster autoscaler).

---

## 2. Domain

**Crypto ticker aggregator.** Consumers ask for current prices by symbol; the system returns cached data as quickly as possible and refreshes from an external price feed in the background.

### 2.1 Why this domain

- **Fluctuating data** — prices change constantly, so cache staleness is observable (the `age_ms` on every response is meaningful).
- **Natural hot/cold distribution** — BTC, ETH, SOL queried frequently; obscure symbols rarely. This creates dramatic cache hit rate variation when the load generator toggles between traffic profiles.
- **Credible rate limits** — external price feeds (CoinGecko, CoinMarketCap, etc.) have well-known rate limits, so the simulated third-party's constraints feel realistic.
- **Clear cache semantics** — prices have obvious freshness rules (seconds matter for active traders, minutes are fine for dashboards).

### 2.2 Universe

A fixed list of ~50 symbols is known to the system:
- **Top 10** (`BTC`, `ETH`, `SOL`, `XRP`, `DOGE`, `ADA`, `AVAX`, `DOT`, `MATIC`, `LINK`) — the pre-warmer refreshes these every 30s.
- **Next 40** (`LTC`, `ATOM`, `NEAR`, `UNI`, etc.) — queryable but not pre-warmed.

The "universe" is hardcoded in a ConfigMap.

---

## 3. High-Level Architecture

```
 ┌────────────────────────────────────────────────────────────────────────┐
 │                              HOST (macOS)                              │
 │                                                                        │
 │  ┌─────────────────────────────────────────────┐                       │
 │  │          Browser (localhost:3000)            │                       │
 │  │  Dashboard SPA — diagram + 3 charts + live   │                       │
 │  └────────────────────┬────────────────────────┘                       │
 │                       │ WebSocket                                       │
 │  ┌────────────────────▼────────────────────────┐                       │
 │  │       Dashboard Backend (FastAPI :3000)      │                       │
 │  │  ┌──────────┐  ┌──────────┐  ┌────────────┐ │                       │
 │  │  │load_gen  │  │k8s_mon   │  │redis_mon   │ │                       │
 │  │  │(httpx)   │  │(k8s api) │  │queue_mon   │ │                       │
 │  │  └────┬─────┘  └────┬─────┘  └─────┬──────┘ │                       │
 │  └───────┼─────────────┼──────────────┼────────┘                       │
 │          │HTTP         │K8s API       │Redis client                    │
 │          │             │              │                                 │
 │  ┌───────▼─────────────▼──────────────▼────────────────────────┐       │
 │  │                   Docker Desktop                             │       │
 │  │  ┌──────────────────────────────────────────────────────┐    │       │
 │  │  │         k3d Cluster "challenge-demo-v2"              │    │       │
 │  │  │                                                      │    │       │
 │  │  │  ┌─────────────┐   ┌─────────────────────────────┐   │    │       │
 │  │  │  │ traefik LB  │   │   metrics-server (5s res)   │   │    │       │
 │  │  │  │ :8080→:80   │   │                             │   │    │       │
 │  │  │  └──────┬──────┘   └─────────────────────────────┘   │    │       │
 │  │  │         │                                             │    │       │
 │  │  │         ▼                                             │    │       │
 │  │  │  ┌──────────────────┐                                 │    │       │
 │  │  │  │ Service: ticker  │──┐                              │    │       │
 │  │  │  │ ClusterIP :80→8000│  │                              │    │       │
 │  │  │  └──────────────────┘  │                              │    │       │
 │  │  │                        ▼                              │    │       │
 │  │  │    ┌──────────────────────────────────┐               │    │       │
 │  │  │    │   API pods (HPA, 1..10)          │               │    │       │
 │  │  │    │   ticker-api (FastAPI + httpx)   │               │    │       │
 │  │  │    └────┬─────────────────────┬───────┘               │    │       │
 │  │  │         │ redis read/write    │ enqueue refresh job   │    │       │
 │  │  │         ▼                     ▼                        │    │       │
 │  │  │    ┌─────────────────────────────────┐                │    │       │
 │  │  │    │  Redis (single instance)        │                │    │       │
 │  │  │    │  DB 0: cache    DB 1: arq queue │                │    │       │
 │  │  │    └────────────┬────────────────────┘                │    │       │
 │  │  │                 │                                     │    │       │
 │  │  │                 │ arq BLPOP                           │    │       │
 │  │  │                 ▼                                     │    │       │
 │  │  │    ┌──────────────────────────────────┐               │    │       │
 │  │  │    │   Worker pods (KEDA, 0..5)       │──────────┐    │    │       │
 │  │  │    │   arq worker (refresh_price)     │          │    │    │       │
 │  │  │    └──────────────────────────────────┘          │    │    │       │
 │  │  │                 ▲                                │    │    │       │
 │  │  │                 │ enqueue refresh job            │HTTP│    │       │
 │  │  │    ┌────────────┴─────────────────┐              │    │    │       │
 │  │  │    │  CronJob: prewarmer          │              │    │    │       │
 │  │  │    │  every 30s, enqueues top-10  │              │    │    │       │
 │  │  │    └──────────────────────────────┘              │    │    │       │
 │  │  │                                                  ▼    │    │       │
 │  │  │    ┌──────────────────────────────────────────────┐   │    │       │
 │  │  │    │  Third-party simulator (ticker-thirdparty)   │   │    │       │
 │  │  │    │  FastAPI + chaos middleware                  │   │    │       │
 │  │  │    │  Rate limits, latency, 429s, 500s            │   │    │       │
 │  │  │    └──────────────────────────────────────────────┘   │    │       │
 │  │  │                                                      │    │       │
 │  │  │  HPA: ticker-api-hpa (CPU 50%, 1..10)                │    │       │
 │  │  │  ScaledObject: ticker-worker-keda (queue len, 0..5)  │    │       │
 │  │  │  CronJob: ticker-prewarmer (*/30 sec, ephemeral)     │    │       │
 │  │  └──────────────────────────────────────────────────────┘    │       │
 │  └──────────────────────────────────────────────────────────────┘       │
 └────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Component Inventory

| Component | Runtime | Replicas | Autoscaler | New in V2? |
|-----------|---------|----------|-----------|------------|
| Browser dashboard SPA | Browser | 1 | — | Updated |
| Dashboard backend | Host process | 1 | — | Extended |
| Ticker API | K8s pods | 1..10 | HPA (CPU) | **Replaces V1 API** |
| Arq workers | K8s pods | 0..5 | KEDA (queue depth) | **New** |
| Pre-warmer | K8s CronJob | 0 or 1 ephemeral | CronJob schedule | **New** |
| Third-party simulator | K8s pod | 1 | none | **New** |
| Redis | K8s pod | 1 | none | **New** |
| Traefik LB | K8s (built-in) | 1 | — | Unchanged |
| metrics-server | K8s (built-in) | 1 | — | Unchanged (5s resolution) |
| KEDA | K8s (installed) | 1 operator | — | **New** |

---

## 4. Request Flow: Stale-While-Revalidate

### 4.1 Cache semantics

| Age of cached entry | Outcome | Action |
|---|---|---|
| `< 5s` (configurable: `FRESH_TTL`) | `FRESH_HIT` | Return cached immediately. Done. |
| `5s – 30s` (configurable: `STALE_TTL`) | `STALE_HIT` | Return cached immediately. Asynchronously enqueue a refresh job. |
| `> 30s` or missing | `COLD_MISS` | Enqueue high-priority refresh. Brief wait (500ms) for worker to populate. If timeout → `TIMEOUT`, return 503. |

The two TTL thresholds are controllable from the dashboard (Advanced panel).

### 4.2 API contract

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/health` | Liveness probe — checks Redis connectivity |
| `GET` | `/price/{symbol}` | Return current price for one symbol (flow above) |
| `GET` | `/prices?symbols=BTC,ETH,SOL` | Batch version — one call, map of results |
| `POST` | `/admin/prewarm/{symbol}` | Force-enqueue a refresh for `symbol` (used by pre-warmer and debug) |

**Response shape for `/price/{symbol}`:**

```json
HTTP/1.1 200 OK
X-Cache-Status: STALE_HIT
{
  "symbol": "BTC",
  "price": 67234.12,
  "age_ms": 8421,
  "status": "STALE_HIT"
}
```

On `COLD_MISS` that times out: `503 Service Unavailable` with `{"symbol": "...", "status": "TIMEOUT"}`.

### 4.3 Request sequence — FRESH_HIT (fast path)

```
Client → API /price/BTC
  → Redis DB 0: GET price:BTC → {price, ts}
  → age = now - ts = 3.2s (< 5s)
  → response: 200 {FRESH_HIT, age_ms: 3200}
```

### 4.4 Request sequence — STALE_HIT (background revalidation)

```
Client → API /price/BTC
  → Redis DB 0: GET price:BTC → {price, ts}
  → age = now - ts = 12s (stale but not cold)
  → fire & forget: Redis DB 1: arq enqueue "refresh_price(BTC)" (low priority, idempotent)
  → response: 200 {STALE_HIT, age_ms: 12000}

Meanwhile, in a worker pod:
  → BLPOP from arq queue
  → call third-party: GET http://ticker-thirdparty/v1/ticker/BTC (subject to rate limit)
  → on success: Redis DB 0: SET price:BTC {price: <new>, ts: <now>}
```

### 4.5 Request sequence — COLD_MISS (synchronous wait)

```
Client → API /price/UNKNOWN-SYMBOL
  → Redis DB 0: GET price:UNKNOWN → nil
  → Redis DB 1: arq enqueue_job "refresh_price(UNKNOWN)" high-priority, job_id = hash(UNKNOWN)
    (job_id dedup: if already enqueued, no-op — avoids stampede)
  → poll Redis DB 0: GET price:UNKNOWN with 500ms budget, 50ms poll interval
  → if populated: 200 {COLD_MISS, age_ms: <small>}
  → if timeout: 503 {TIMEOUT}
```

**Stampede protection** is handled by Arq's `job_id` deduplication: concurrent COLD_MISS requests for the same symbol all enqueue with the same `job_id` (derived from symbol name), resulting in a single worker execution.

---

## 5. Component Details

### 5.1 Ticker API (`api/`)

**Replaces** the V1 CPU-burn API. Same `api/` directory; `main.py` gutted.

**Responsibilities:**
- Serve `/price/*` endpoints
- Read from Redis DB 0
- Enqueue jobs into Redis DB 1 (Arq) on miss / stale
- Report cache outcome via `X-Cache-Status` header and response JSON

**Dependencies:**
- `fastapi==0.115.12`
- `uvicorn==0.34.2`
- `redis[hiredis]==5.2.1` — async client
- `arq==0.26.3` — queue producer

**Implementation notes:**
- Uses a single shared `redis.asyncio.Redis` pool (max 20 connections) created on startup.
- Uses `arq.create_pool` for enqueueing (separate Arq pool for DB 1).
- COLD_MISS polling uses `asyncio.wait_for` + `asyncio.sleep(0.05)` loop — no busy-waiting.

### 5.2 Third-party simulator (`third_party/`)

New directory. A small FastAPI app that mimics an external ticker API with configurable chaos.

**Endpoints:**

| Method | Path | Behavior |
|--------|------|----------|
| `GET` | `/health` | Always 200 |
| `GET` | `/v1/ticker/{symbol}` | Returns `{symbol, price, ts}` — subject to chaos middleware |
| `POST` | `/admin/chaos` | Sets the chaos profile (called by dashboard backend) |

**Chaos profiles:**

| Profile | Latency | Failure rate | Rate limit |
|---------|---------|--------------|-----------|
| `healthy` | 20–50ms | 0% | 100 RPS global, 10 RPS per symbol |
| `slow` | 500–2000ms | 0% | same |
| `flaky` | 20–50ms | 10% 500s | same |
| `rate-limited` | 20–50ms | 0% | 30 RPS global (aggressive 429s) |

**Middleware:**
- `RateLimiterMiddleware`: in-memory token buckets (per-symbol + global), returns 429 when exhausted.
- `ChaosMiddleware`: after passing rate limit, applies latency + probabilistic failures based on active profile.

Chaos profile is mutable at runtime via `POST /admin/chaos` with payload `{"profile": "flaky"}`. Stored in-memory; resets on pod restart (intentional — simplest possible stateful config).

**Data source:**
- Prices are generated deterministically: `price(symbol, t) = base[symbol] + sine(t) * volatility[symbol]` with 50 hardcoded symbols. No real API is called. The system is fully offline.

### 5.3 Arq workers (`worker/`)

New directory. Long-running Arq workers that consume the refresh queue.

**Job definition:**

```python
async def refresh_price(ctx, symbol: str) -> dict:
    # 1. Call third-party (via httpx client)
    # 2. Parse response
    # 3. Write to Redis DB 0 with current timestamp
    # 4. Return job summary for logging
```

**Worker settings:**
- `max_jobs: 10` per worker (concurrent in-flight jobs)
- `job_timeout: 10s`
- `keep_result: 30s` (for idempotency checks + debugging)
- Third-party httpx client: max 20 connections, 5s timeout

**Graceful shutdown:**
- On SIGTERM: stop accepting new jobs, await in-flight for up to 10s, exit.
- K8s termination grace period: 15s.

### 5.4 Pre-warmer (`prewarmer/`)

New directory. A small Python script, **not a long-running service**. Runs as a K8s CronJob pod every 30 seconds.

**Behavior:**
1. Read top-10 symbols list from env (populated by ConfigMap).
2. Open Arq pool to Redis DB 1.
3. Enqueue `refresh_price(symbol)` for each top-10 symbol with `job_id=hash(f"{symbol}:prewarm:{minute}")` (minute-bucketed id for idempotency across restarts).
4. Close pool, exit.

Typical lifecycle: ~5s total (pod startup ~3s, script ~2s).

**Why not just use Arq's built-in cron?** KEDA scales workers to zero. If the cron lived inside workers, the pre-warmer would prevent scale-to-zero. A separate CronJob is independent from worker scaling.

### 5.5 Redis

Single pod running `redis:7.4-alpine`. Two databases:
- **DB 0** — cache (`price:*` keys, no TTL — staleness is computed from embedded timestamp)
- **DB 1** — Arq queue (managed by Arq: `arq:queue`, `arq:result:*`, `arq:in-progress:*`)

**Eviction policy:** `allkeys-lru` with `maxmemory 64mb`. In practice never evicts (50 symbols × ~200 bytes = trivial), but configured for correctness.

**Persistence:** disabled (`--save ""`). Demo is ephemeral.

**Why single instance:** Simpler topology. Two databases in one instance is a well-understood pattern (same operational concerns, isolated keyspaces). A note in the doc should call out that in production, cache and queue would be separate instances.

### 5.6 Dashboard backend (extended)

Existing `dashboard/app.py` is extended with two new collectors:

**New module `dashboard/redis_monitor.py`:**
```python
class RedisMonitor:
    async def get_metrics(self) -> RedisSnapshot:
        # INFO stats → keyspace_hits, keyspace_misses, used_memory_bytes
        # DB 0 size: DBSIZE
        # Returns cache hit rate %, total keys, memory usage
```

**New module `dashboard/queue_monitor.py`:**
```python
class QueueMonitor:
    async def get_metrics(self) -> QueueSnapshot:
        # LLEN arq:queue  → pending jobs
        # Counts of arq:in-progress:* keys → active jobs
        # Recent arq:result:* timestamps → completion rate
```

**Existing `K8sMonitor` extended:**
- Label selector widened: `app in (ticker-api, ticker-worker)` to collect both deployments.
- Reads **two** HPAs/ScaledObjects: `ticker-api-hpa` and `ticker-worker-keda`.
- Also reads recent CronJob `Jobs` for the pre-warmer (last 10 entries) to show firing history.

**Cache outcome tracking (unchanged pattern from V1):**
- `load_generator.py` parses `X-Cache-Status` header from each response.
- `metrics.py` `MetricsStore` extended with an outcome counter (per-second buckets: `{FRESH_HIT: N, STALE_HIT: N, COLD_MISS: N, TIMEOUT: N}`).
- `metrics_loop` in `app.py` includes outcome breakdown in the WebSocket payload.

### 5.7 Dashboard frontend (extended)

**Layout** (based on V1's final split layout):

```
┌─────────────────────────────────────────────────────────────┐
│ Header (Abstract Load Testing) ········· ● Connected        │
├──────────────────────────────┬──────────────────────────────┤
│ Controls (centered above):   │                              │
│ [Start] [RPS slider] [URL]   │  Response Latency            │
│ [Advanced ▾]                 │  (3 series:                  │
│                              │   FRESH / STALE / COLD)      │
├──────────────────────────────┤                              │
│                              ├──────────────────────────────┤
│                              │                              │
│   Architecture diagram       │  Replica count               │
│   (expanded to include       │  (3 series:                  │
│    Redis, third-party,       │   API / Workers / Cron)      │
│    workers, cron)            │                              │
│                              ├──────────────────────────────┤
│                              │                              │
│                              │  Cache outcomes              │
│                              │  (stacked area:              │
│                              │   FRESH/STALE/COLD/TIMEOUT)  │
├──────────────────────────────┴──────────────────────────────┤
│ ● RUNNING  RPS 80  API 3/3  WKR 2/5  Q 14  HIT 87%  3P 22r/s│
└─────────────────────────────────────────────────────────────┘
```

**Advanced controls panel** (collapsible, appears below header):

```
Traffic profile: [hot-only | cold-only | zipfian]
Pre-warmer:      [⏸ Pause] (resume)
Chaos profile:   [healthy | slow | flaky | rate-limited]
Cache TTLs:      Fresh: [=====●=====] 5s   Stale: [===●=========] 30s
```

**Architecture diagram additions** (reorganized from V1 to fit new nodes):
- New node: **Redis** (below cluster label, cache and queue subsections shown)
- New node: **Third-party simulator** (right side, outside cluster visually — even though it's in the cluster, positioning communicates "external")
- New row of pods: **Workers** (between API pods and cluster bottom, animates 0 → 5)
- New icon: **CronJob** (small, on the side, pulses every 30s)
- New flow colors:
  - Green (request, V1): browser → API → cache
  - Cyan (**new**): API → queue (on miss) → worker → third-party → cache
  - Orange (scaling, V1): HPA/KEDA → pods
  - Violet (metrics, V1): pods → metrics-server → HPA/KEDA

### 5.8 Load generator (extended)

`load_generator.py` gets three new behaviors:

1. **Traffic profile selector** — `self.profile` ∈ `{"hot-only", "cold-only", "zipfian"}`.
2. **Symbol picker** — replaces the hardcoded URL with a per-request symbol selection:
   - `hot-only`: uniform random from top-10
   - `cold-only`: uniform random from positions 11..50
   - `zipfian`: Zipf distribution over all 50 symbols (alpha=1.5, favors top positions)
3. **Outcome aggregation** — each response's `X-Cache-Status` is fed to `MetricsStore.record_outcome()`.

The URL input in the dashboard changes from "Target URL" to "Base URL" (e.g. `http://localhost:8080`); the path is constructed as `{base}/price/{chosen_symbol}`.

---

## 6. Kubernetes Configuration

### 6.1 New / modified manifests

```
k8s/
├── deployment.yaml          # MODIFIED: ticker-api (was python-api)
├── service.yaml             # MODIFIED: ticker-api
├── hpa.yaml                 # MODIFIED: targets ticker-api
├── ingress.yaml             # MODIFIED: routes to ticker-api
├── redis.yaml               # NEW: Deployment + Service
├── thirdparty.yaml          # NEW: Deployment + Service
├── worker.yaml              # NEW: Arq worker Deployment
├── keda-scaled.yaml         # NEW: ScaledObject for workers
├── prewarmer.yaml           # NEW: CronJob
├── configmap.yaml           # NEW: symbol universe + top-10 list
└── secrets.yaml             # NEW (optional): Redis password (default empty)
```

### 6.2 Resource budgets

| Deployment | Replicas | CPU req / lim | Memory req / lim | QoS |
|-----------|----------|---------------|------------------|-----|
| `ticker-api` | 1..10 (HPA) | 100m / 500m | 128Mi / 256Mi | Burstable |
| `ticker-worker` | 0..5 (KEDA) | 200m / 500m | 128Mi / 256Mi | Burstable |
| `ticker-prewarmer` (CronJob) | ephemeral | 50m / 100m | 64Mi / 128Mi | Burstable |
| `ticker-thirdparty` | 1 | 100m / 300m | 128Mi / 256Mi | Burstable |
| `redis` | 1 | 50m / 200m | 64Mi / 128Mi | Burstable |

**Cluster totals at max scale:**
- CPU requested: 10×100 + 5×200 + 1×50 + 1×100 + 1×50 = 2.2 cores
- CPU burstable: 10×500 + 5×500 + 1×100 + 1×300 + 1×200 = 8.1 cores
- Memory requested: ~3 GiB
- Memory ceiling: ~4.5 GiB

Fits in the default k3d cluster (4 cores, ~6-8 GiB Docker Desktop).

### 6.3 HPA — `ticker-api`

Unchanged from V1:
- CPU 50%, 1..10 replicas
- Fast scale-up (0s stabilization, +2 pods / 30s)
- Moderate scale-down (30s, -50% / 30s)

### 6.4 KEDA ScaledObject — `ticker-worker`

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata:
  name: ticker-worker-keda
spec:
  scaleTargetRef:
    name: ticker-worker
  minReplicaCount: 0
  maxReplicaCount: 5
  pollingInterval: 5          # check queue every 5s
  cooldownPeriod: 30          # wait 30s of zero queue before scaling to 0
  triggers:
  - type: redis
    metadata:
      address: redis.default.svc.cluster.local:6379
      listName: arq:queue      # Arq's queue key
      listLength: "5"          # 5 pending jobs per worker triggers scale-up
      databaseIndex: "1"
```

**Scale-to-zero behavior:** This is the critical difference from HPA. When the queue is empty for 30s, KEDA removes all worker pods. New pods spin up when jobs arrive. Startup time ≈ 3-5s.

### 6.5 CronJob — `ticker-prewarmer`

```yaml
apiVersion: batch/v1
kind: CronJob
metadata:
  name: ticker-prewarmer
spec:
  schedule: "*/1 * * * *"       # Kubernetes minimum is 1 min;
                                 # workaround below for 30s
  concurrencyPolicy: Forbid      # skip if previous run still active
  successfulJobsHistoryLimit: 3
  failedJobsHistoryLimit: 1
  jobTemplate:
    spec:
      backoffLimit: 0
      template:
        spec:
          restartPolicy: Never
          containers:
          - name: prewarmer
            image: ticker-prewarmer:latest
            env:
            - name: REDIS_HOST
              value: redis.default.svc.cluster.local
            - name: TOP_SYMBOLS
              valueFrom:
                configMapKeyRef: {name: ticker-config, key: top-symbols}
```

**K8s CronJob minimum is 1 minute.** To achieve 30s cadence, the prewarmer script runs the enqueue logic twice internally: once on startup, then sleeps 30s, enqueues again, exits. This mimics 30s scheduling from within a 1-minute CronJob.

---

## 7. WebSocket Protocol (extended)

### 7.1 Client → Server messages (new actions)

| Action | Payload | Effect |
|--------|---------|--------|
| `start` | `{}` | V1 — start load |
| `pause` | `{}` | V1 — pause load |
| `set_rps` | `{"value": 100}` | V1 — set RPS |
| `set_base_url` | `{"value": "http://..."}` | **renamed** (was `set_url`) — base URL, symbols appended by load_gen |
| `set_traffic_profile` | `{"value": "hot-only"\|"cold-only"\|"zipfian"}` | **new** |
| `toggle_prewarmer` | `{"paused": true\|false}` | **new** — scales CronJob suspension |
| `set_chaos_profile` | `{"value": "healthy"\|"slow"\|"flaky"\|"rate-limited"}` | **new** — dashboard backend calls third-party `/admin/chaos` |
| `set_ttls` | `{"fresh_ms": 5000, "stale_ms": 30000}` | **new** — patched into ticker-api via ConfigMap + rolling restart (OR via admin endpoint — see §12) |

### 7.2 Server → Client broadcast (extended payload)

```json
{
  "type": "metrics",
  "timestamp": 1743580200.123,
  "response_times": {
    "avg_ms": {"FRESH_HIT": 4.1, "STALE_HIT": 5.2, "COLD_MISS": 320.5},
    "p90_ms": {"FRESH_HIT": 7.0, "STALE_HIT": 8.5, "COLD_MISS": 510.0}
  },
  "load": {
    "target_rps": 100,
    "actual_rps": 98,
    "is_running": true,
    "profile": "zipfian"
  },
  "cache": {
    "hit_rate_pct": 87.3,
    "total_keys": 42,
    "memory_mb": 1.2,
    "outcomes_last_second": {
      "FRESH_HIT": 62, "STALE_HIT": 28, "COLD_MISS": 8, "TIMEOUT": 0
    }
  },
  "queue": {
    "pending": 14,
    "in_progress": 3,
    "completed_last_second": 22
  },
  "cluster": {
    "api": {"replicas": 3, "hpa_desired": 3, "hpa_current_cpu": 48},
    "workers": {"replicas": 2, "keda_desired": 2, "queue_length": 14},
    "prewarmer": {"last_run": 1743580188, "status": "success"},
    "thirdparty": {"rps": 22, "avg_latency_ms": 48, "error_rate_pct": 0, "chaos_profile": "healthy"},
    "pods": [
      {"app": "ticker-api", "name": "...", "cpu_percent": 52, "memory_mb": 89, "status": "Running"},
      {"app": "ticker-worker", "name": "...", "cpu_percent": 18, "memory_mb": 72, "status": "Running"}
    ]
  }
}
```

---

## 8. Deployment Pipeline

### 8.1 `scripts/setup.sh` — extended

Additions on top of V1:

```bash
# ... (existing V1 steps: k3d create, wait for nodes, patch metrics-server)

# Install KEDA
kubectl apply -f https://github.com/kedacore/keda/releases/download/v2.16.0/keda-2.16.0.yaml

# Wait for KEDA operator
kubectl wait --for=condition=Available deployment/keda-operator \
  -n keda --timeout=120s
```

### 8.2 `scripts/deploy.sh` — rewritten

```bash
# Build images
docker build -t ticker-api:latest ./api
docker build -t ticker-thirdparty:latest ./third_party
docker build -t ticker-worker:latest ./worker
docker build -t ticker-prewarmer:latest ./prewarmer

# Import into k3d
k3d image import ticker-api:latest ticker-thirdparty:latest \
                  ticker-worker:latest ticker-prewarmer:latest \
                  -c challenge-demo

# Apply K8s manifests
kubectl apply -f ./k8s/configmap.yaml
kubectl apply -f ./k8s/redis.yaml
kubectl rollout status deployment/redis --timeout=60s

kubectl apply -f ./k8s/thirdparty.yaml
kubectl rollout status deployment/ticker-thirdparty --timeout=60s

kubectl apply -f ./k8s/deployment.yaml
kubectl apply -f ./k8s/service.yaml
kubectl apply -f ./k8s/hpa.yaml
kubectl apply -f ./k8s/ingress.yaml
kubectl rollout status deployment/ticker-api --timeout=120s

kubectl apply -f ./k8s/worker.yaml
kubectl apply -f ./k8s/keda-scaled.yaml

kubectl apply -f ./k8s/prewarmer.yaml
```

### 8.3 `scripts/teardown.sh`

Unchanged: `k3d cluster delete challenge-demo`.

### 8.4 Local development loop

Changed iteration story: the set of services to rebuild is now larger. A `Makefile` or per-service `scripts/deploy-X.sh` would help, but not strictly required.

---

## 9. File Map

```
code-challenge/
├── api/                            # MODIFIED: now ticker API
│   ├── Dockerfile
│   ├── requirements.txt            # +redis, +arq
│   └── main.py                     # /health, /price/{symbol}, /prices, /admin/prewarm
│
├── third_party/                    # NEW
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── main.py                     # /v1/ticker/{symbol}, /admin/chaos
│   ├── chaos.py                    # ChaosMiddleware + profiles
│   ├── rate_limit.py               # RateLimiterMiddleware
│   └── prices.py                   # deterministic price generator
│
├── worker/                         # NEW
│   ├── Dockerfile
│   ├── requirements.txt            # arq, httpx, redis
│   └── worker.py                   # Arq WorkerSettings + refresh_price job
│
├── prewarmer/                      # NEW
│   ├── Dockerfile
│   ├── requirements.txt            # arq, redis
│   └── main.py                     # one-shot enqueue script
│
├── dashboard/
│   ├── requirements.txt            # +redis
│   ├── app.py                      # extended: new actions, new payload shape
│   ├── load_generator.py           # extended: profiles, symbol picker, outcomes
│   ├── k8s_monitor.py              # extended: multi-app label selector, KEDA
│   ├── redis_monitor.py            # NEW
│   ├── queue_monitor.py            # NEW
│   ├── metrics.py                  # extended: outcome counters
│   └── static/
│       ├── index.html              # updated layout, new diagram nodes
│       └── style.css               # updated for new controls + chart series
│
├── k8s/
│   ├── deployment.yaml             # MODIFIED: ticker-api
│   ├── service.yaml                # MODIFIED
│   ├── hpa.yaml                    # MODIFIED
│   ├── ingress.yaml                # MODIFIED
│   ├── redis.yaml                  # NEW
│   ├── thirdparty.yaml             # NEW
│   ├── worker.yaml                 # NEW
│   ├── keda-scaled.yaml            # NEW
│   ├── prewarmer.yaml              # NEW
│   └── configmap.yaml              # NEW
│
├── scripts/
│   ├── setup.sh                    # extended: install KEDA
│   ├── deploy.sh                   # rewritten: multi-service build/apply
│   └── teardown.sh                 # unchanged
│
└── docs/
    ├── architecture-spec.md        # V1 (preserved)
    └── architecture-spec-v2.md     # this document
```

---

## 10. Migration from V1

V2 is a **breaking replacement**. The `api/` directory is gutted; old `/work` endpoint is deleted. There is no graceful transition because the demo is self-contained and its value is a coherent story, not continuity.

**What stays from V1:**
- k3d + K3s cluster topology
- Traefik ingress
- HPA on API pods (CPU 50%)
- Dashboard process structure (FastAPI + WebSocket + collectors)
- `load_generator.py` async firing model (extended, not rewritten)
- `MetricsStore` deque model (extended with outcomes)
- Scripts shape (`setup.sh`, `deploy.sh`, `teardown.sh`)

**What goes from V1:**
- `api/main.py` `/work` SHA-256 endpoint (replaced by ticker API)
- CPU chart in the dashboard (the three new charts replace the old three; CPU is visualized per-pod in the diagram)
- `Target URL` input (becomes `Base URL`)

**What's added:**
- Redis deployment
- Third-party simulator deployment
- Arq workers + KEDA ScaledObject
- Pre-warmer CronJob
- Two new dashboard collectors
- Three new controls (profile, chaos, TTLs) + one new toggle (pause pre-warmer)

---

## 11. Decision Log

| # | Decision | Choice | Alternatives | Rationale |
|---|----------|--------|--------------|-----------|
| 1 | Queue library | Arq | BullMQ, Celery, RQ, Dramatiq | Keeps stack Python-uniform. Async-native. Redis-backed. KEDA can scale it via Redis scaler. |
| 2 | Worker autoscaler | KEDA (Redis list length) | HPA with external metrics, manual scaling | KEDA is purpose-built for queue-based workloads and supports scale-to-zero — the dramatic visual that HPA can't deliver. |
| 3 | Pre-warmer mechanism | K8s CronJob | Arq cron inside workers, APScheduler in API | CronJob preserves the "three autoscalers" narrative (HPA + KEDA + CronJob). Separate pod visible in dashboard. |
| 4 | Redis topology | Single instance, two DBs | Two separate Redis instances | Simpler manifests, single point to monitor. Note: prod would separate. |
| 5 | Cache semantics | Stale-while-revalidate (5s/30s thresholds) | Read-through only, write-through, hard TTL | Four observable outcomes (FRESH/STALE/COLD/TIMEOUT), each visually distinct. Models real-world cache patterns. |
| 6 | Third-party simulation | In-cluster pod with chaos middleware | Mock library, toxiproxy | Runs in K8s → visible in dashboard as a pod. Simpler than toxiproxy, gives full control over failure modes. |
| 7 | Domain | Crypto tickers | Geocoding, weather, FX rates | Fluctuating data makes staleness observable. Clear hot/cold distribution. Credible rate limits. |
| 8 | Cache outcome tracking | `X-Cache-Status` header + load gen aggregation | Prometheus, API `/metrics` endpoint | Continues V1's pattern: load_gen is the source of truth for client-observed metrics. No new infra. |
| 9 | Pre-warmer cadence | 30s (simulated inside 1-min CronJob) | Every minute, every 10s | 30s is tight enough to see warming effect on hot symbols; K8s CronJob minimum is 1 min, worked around with internal sleep. |
| 10 | Migration strategy | Full replacement | Coexistence under `/v2/` prefix | Demo value is a coherent single story. Coexistence would dilute the narrative and inflate setup complexity. |

---

## 12. Open questions (to decide during implementation)

1. **TTL tuning at runtime** — changing `FRESH_TTL` / `STALE_TTL` mid-demo from the dashboard is valuable but requires either (a) patching a ConfigMap + rolling restart (slow), or (b) adding a `/admin/config` endpoint on ticker-api that stores TTLs in Redis and reads them per-request (fast, but adds a Redis hop to the fast path). **Preferred: (b).**

2. **Worker CPU request sizing** — set to 200m tentatively because workers do HTTP calls + JSON parsing. May need tuning if KEDA's queue-based scaling behaves poorly (e.g., pods idle while waiting on third-party). Measure in practice.

3. **CronJob minimum cadence workaround** — running the enqueue twice per pod (startup + 30s sleep + enqueue + exit) works but mixes concerns. Alternative: deploy *two* CronJobs offset by 30s (`*/1 * * * *` and `*/1 * * * * + 30s` — the latter isn't standard cron, so this needs a separate approach, e.g., a `initialDelaySeconds` pattern or wrapper). Revisit.

4. **KEDA + metrics-server interaction** — KEDA's Redis scaler polls Redis directly (not through metrics-server), so it's independent of the 5s metric-resolution we set for HPA. This is a feature, but worth documenting so it's not mysterious when KEDA reacts faster than HPA in the demo.

5. **Dashboard diagram layout** — the V1 SVG is already tight (960×680 viewBox). Fitting Redis, third-party, workers row, and CronJob icon will require a spatial redesign. Not a scope question, but an implementation one.

---

## 13. Future Work (explicitly out of V2 scope)

- **Prometheus + Grafana sidecar** for production-grade observability (alternative to bespoke dashboard).
- **Per-symbol cache heatmap** (50-cell grid showing staleness by symbol).
- **Multi-region third-party** with failover (requires second third-party pod + routing logic).
- **Explicit circuit breaker** on third-party calls (currently implicit via rate-limit backoff).
- **Request-level tracing** (OpenTelemetry spans across API → queue → worker → third-party).
- **Persistent cache** (Redis with AOF, or switch to Redis cluster).
- **Auth & TLS** (zero in V2; for production-style demos, add mTLS in cluster and JWT on API).
- **Coalesced COLD_MISS waiters** (currently only dedup enqueue via `job_id`; multiple HTTP clients still poll independently. Could use pub/sub to notify waiters when a job completes).

---

**Scope cerrado. Revisión antes de implementación.**

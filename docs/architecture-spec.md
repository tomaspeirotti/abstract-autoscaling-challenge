# Architecture Specification

## 1. System Overview

**Load Testing & Autoscaling Infrastructure** — a self-contained local platform that runs a Python API inside a Kubernetes cluster, generates configurable HTTP load in real time, and monitors autoscaling behavior through a live dashboard.

The system proves that the API handles variable traffic gracefully by leveraging Kubernetes Horizontal Pod Autoscaler (HPA) with real-time observability.

### 1.1 Design Goals

| Goal | How it's achieved |
|------|-------------------|
| Real autoscaling (not simulated) | k3d cluster running K3s with built-in metrics-server |
| Instant RPS adjustability | Custom async load generator with shared state variable |
| Zero build tooling for frontend | Vanilla JS + Chart.js from CDN |
| Single-command lifecycle | Bash scripts for setup, deploy, teardown |
| macOS Apple Silicon native | All images arm64, no QEMU emulation |
| Full local operation | No cloud dependencies, runs entirely on Docker Desktop |

---

## 2. High-Level Architecture

```
 ┌──────────────────────────────────────────────────────────────────┐
 │                         HOST (macOS)                             │
 │                                                                  │
 │  ┌──────────────────────────────────────────┐                    │
 │  │         Browser (localhost:3000)          │                    │
 │  │  ┌────────────────────────────────────┐   │                    │
 │  │  │   Dashboard SPA (Chart.js + WS)    │   │                    │
 │  │  │   - Response time charts           │   │                    │
 │  │  │   - Replica / CPU charts           │   │                    │
 │  │  │   - Pod status table               │   │                    │
 │  │  │   - RPS slider + Start/Pause       │   │                    │
 │  │  └──────────────┬─────────────────────┘   │                    │
 │  └─────────────────┼────────────────────────┘                    │
 │                    │ WebSocket (bidirectional)                    │
 │                    ▼                                              │
 │  ┌──────────────────────────────────────────┐                    │
 │  │    Dashboard Backend (FastAPI :3000)      │                    │
 │  │    ┌────────────────────────────────────┐ │                    │
 │  │    │  app.py                            │ │                    │
 │  │    │  - WebSocket hub (/ws)             │ │                    │
 │  │    │  - Static file server              │ │                    │
 │  │    │  - metrics_loop (1s broadcast)     │ │                    │
 │  │    └─────┬──────────────────┬───────────┘ │                    │
 │  │          │                  │             │                    │
 │  │    ┌─────▼──────┐    ┌─────▼──────────┐  │                    │
 │  │    │ load_gen    │    │ k8s_monitor    │  │                    │
 │  │    │ (httpx)     │    │ (python-k8s)   │  │                    │
 │  │    └─────┬───────┘    └─────┬──────────┘  │                    │
 │  │          │                  │             │                    │
 │  └──────────┼──────────────────┼─────────────┘                    │
 │             │ HTTP             │ K8s API (~/.kube/config)         │
 │             ▼                  ▼                                  │
 │  ┌───────────────────────────────────────────────────────────┐    │
 │  │              Docker Desktop                                │    │
 │  │  ┌─────────────────────────────────────────────────────┐   │    │
 │  │  │           k3d Cluster "challenge-demo"              │   │    │
 │  │  │                                                     │   │    │
 │  │  │  ┌──────────────┐  ┌────────────────────────────┐   │   │    │
 │  │  │  │  traefik LB   │  │     metrics-server         │   │   │    │
 │  │  │  │  :8080→:80    │  │     (built into K3s)       │   │   │    │
 │  │  │  └──────┬────────┘  └────────────────────────────┘   │   │    │
 │  │  │         │                                            │   │    │
 │  │  │         ▼                                            │   │    │
 │  │  │  ┌──────────────────────────────────┐                │   │    │
 │  │  │  │  Service: python-api (ClusterIP) │                │   │    │
 │  │  │  │  :80 → :8000                     │                │   │    │
 │  │  │  └──────────┬───────────────────────┘                │   │    │
 │  │  │             │ round-robin                            │   │    │
 │  │  │    ┌────────┼────────┐                               │   │    │
 │  │  │    ▼        ▼        ▼                               │   │    │
 │  │  │  Pod 1    Pod 2    Pod N                             │   │    │
 │  │  │  uvicorn  uvicorn  uvicorn                           │   │    │
 │  │  │  :8000    :8000    :8000                             │   │    │
 │  │  │                                                     │   │    │
 │  │  │  ┌──────────────────────────────────┐                │   │    │
 │  │  │  │  HPA: python-api-hpa             │                │   │    │
 │  │  │  │  CPU target: 50% → scale 1..10   │                │   │    │
 │  │  │  └──────────────────────────────────┘                │   │    │
 │  │  │                                                     │   │    │
 │  │  │  Nodes: 1 server + 2 agents                         │   │    │
 │  │  └─────────────────────────────────────────────────────┘   │    │
 │  └───────────────────────────────────────────────────────────┘    │
 └──────────────────────────────────────────────────────────────────┘
```

### 2.1 Component Boundaries

| Component | Runs on | Communicates via | Stateful? |
|-----------|---------|------------------|-----------|
| Dashboard Frontend | Browser | WebSocket to backend | Client-side only (Chart.js datasets) |
| Dashboard Backend | Host (Python process) | WS to browser, HTTP to K8s API, HTTP to API pods | In-memory deque (5-min window) |
| Load Generator | Host (asyncio task) | HTTP POST to `localhost:8080/work` | Shared `target_rps` + `is_running` vars |
| K8s Monitor | Host (asyncio task) | K8s Python client → API server | Stateless (polls every 1s) |
| Python API | K8s pods | Receives HTTP on :8000 | Stateless |
| HPA Controller | K8s control plane | Reads metrics-server, patches Deployment | K8s etcd |
| metrics-server | K8s (built into K3s) | Kubelet → pod resource usage | In-memory |

---

## 3. Component Details

### 3.1 Python API (`api/`)

A minimal FastAPI application that serves as the workload target. Designed to be replaceable with a real challenge API.

**Endpoints:**

| Method | Path | Purpose | Details |
|--------|------|---------|---------|
| `GET` | `/health` | Liveness/readiness probe | Returns `{"status": "ok"}` |
| `POST` | `/work` | CPU-intensive workload | Iterative SHA-256 hashing, configurable iterations |

**`POST /work` behavior:**
- Query parameter: `iterations` (default: 100,000; range: 1 to 10,000,000)
- Implementation: chains SHA-256 hashes `N` times starting from `b"seed"`
- Returns: `{"iterations": <int>, "elapsed_ms": <float>}`
- Purpose: generates measurable CPU load to trigger HPA scaling

**Runtime:**
- Base image: `python:3.12-slim` (arm64)
- Server: `uvicorn main:app --host 0.0.0.0 --port 8000`
- Dependencies: `fastapi==0.115.12`, `uvicorn==0.34.2`
- Docker HEALTHCHECK: `curl -f http://localhost:8000/health` every 10s

### 3.2 Dashboard Backend (`dashboard/`)

A FastAPI process running on the host that orchestrates load generation, K8s monitoring, and real-time metric broadcasting.

#### 3.2.1 Module: `app.py` — Orchestrator

**Responsibilities:**
- Serves static frontend files from `dashboard/static/`
- Manages WebSocket connections (multi-client support)
- Runs the `metrics_loop` background task (1-second interval)
- Routes incoming WebSocket commands to the appropriate handler

**Lifecycle:**
1. On `startup` event: spawns `metrics_loop` as an asyncio task
2. `metrics_loop` runs indefinitely:
   - Calls `metrics_store.compute_snapshot()` for response time stats
   - Calls `k8s_monitor.get_metrics()` for cluster state
   - Broadcasts combined payload to all connected WebSocket clients
3. WebSocket connections are tracked in a `set[WebSocket]`; disconnected clients are pruned on broadcast failure

**Environment variables:**
- `TARGET_URL` — default: `http://localhost:8080/work` (changeable at runtime via WebSocket)

#### 3.2.2 Module: `load_generator.py` — Traffic Generator

**Class: `LoadGenerator`**

| Attribute | Type | Default | Purpose |
|-----------|------|---------|---------|
| `target_url` | `str` | from env | HTTP endpoint to hit |
| `target_rps` | `float` | 10 | Requests per second target |
| `is_running` | `bool` | False | Start/pause state |
| `_semaphore` | `Semaphore(200)` | — | Max concurrent in-flight requests |
| `_client` | `httpx.AsyncClient` | — | Connection-pooled HTTP client (max 200 connections, 30s timeout) |

**Request firing model:**
```
_ticker() loop:
  while is_running:
    1. Compute interval = 1.0 / target_rps
    2. Acquire semaphore (block if 200 in-flight)
    3. Fire _send_request() as a detached asyncio.Task
    4. Sleep for remaining interval time
```

- Requests are non-blocking: `_ticker` doesn't wait for responses
- Each response records `(elapsed_ms, status_code)` via `MetricsStore.record_response()`
- On HTTP error: records `status_code=500` with the elapsed time
- On pause: cancels ticker, awaits all in-flight tasks, closes HTTP client

**RPS control:**
- `set_rps(value)` clamps to [1, 500] and updates `target_rps`
- Takes effect on the next ticker iteration (sub-second latency)

#### 3.2.3 Module: `k8s_monitor.py` — Cluster Observer

**Class: `K8sMonitor`**

Configuration (constructor params):
- `namespace`: `"default"`
- `deployment_name`: `"python-api"`
- `hpa_name`: `"python-api-hpa"`
- `cpu_request_millicores`: `250` (matches deployment.yaml)

**Data collection (runs in a thread via `asyncio.to_thread`):**

1. **Pod metrics** — `CustomObjectsApi.list_namespaced_custom_object(group="metrics.k8s.io")`:
   - Per-pod CPU usage (parsed from K8s format: `"25m"` → 0.025 cores)
   - Per-pod memory usage (parsed: `"65536Ki"` → 64 MB)
   - CPU is reported as percentage relative to the resource request (250m)

2. **Pod status** — `CoreV1Api.list_namespaced_pod(label_selector="app=python-api")`:
   - Pod name, phase (Running/Pending/Failed/Unknown)

3. **HPA status** — `AutoscalingV2Api.read_namespaced_horizontal_pod_autoscaler`:
   - `desired_replicas` — what HPA wants
   - `current_metrics[].resource.current.average_utilization` — current avg CPU %

**K8s value parsers:**
- CPU: handles `n` (nanocores), `u` (microcores), `m` (millicores), bare float (cores)
- Memory: handles `Ki`, `Mi`, `Gi`, `K`, `M`, `G`, raw bytes → converts to MB

**Output:** `ClusterSnapshot` dataclass:
```python
@dataclass
class ClusterSnapshot:
    replicas: int              # Running pods count
    hpa_desired: int           # HPA desired replicas
    hpa_current_cpu: int|None  # Current avg CPU utilization %
    pods: list[PodMetrics]     # Per-pod details
```

#### 3.2.4 Module: `metrics.py` — Aggregation Engine

**Class: `MetricsStore`**

Storage:
- `_records: deque[ResponseRecord]` — raw response entries (unbounded within window)
- `_snapshots: deque[MetricsSnapshot]` — per-second aggregates (`maxlen=300` = 5 minutes)

**`compute_snapshot()` algorithm (called every 1s by `metrics_loop`):**
1. Evict records older than `window_seconds` (300s)
2. Filter records from the last 1 second
3. If no records: emit zero-valued snapshot
4. Otherwise compute:
   - `avg_ms`: arithmetic mean of response times
   - `p90_ms`, `p99_ms`: linear interpolation percentile on sorted times
   - `actual_rps`: count of records in the last second
   - `error_count`: count where `status_code >= 400`

**Percentile implementation:**
```
k = (pct/100) * (len - 1)
result = data[floor(k)] * (ceil(k) - k) + data[ceil(k)] * (k - floor(k))
```

#### 3.2.5 Module: `cluster_config.py` — Live Config Editor

**Class: `ClusterConfigManager`**

Exposes three operations over the HPA and Deployment:
- `get_defaults()` — parses `k8s/hpa.yaml` and `k8s/deployment.yaml` with PyYAML; source of truth for the "initial" config.
- `get_current()` — reads live HPA + Deployment via `AutoscalingV2Api` and `AppsV1Api`.
- `apply(new_cfg)` — validates, patches HPA (`spec.minReplicas`, `spec.maxReplicas`, `spec.metrics[*].target.averageUtilization`), patches Deployment (`spec.template.spec.containers[0].resources.{requests,limits}.cpu`), updates `K8sMonitor.cpu_request_millicores`, and polls `read_namespaced_deployment_status` until the rolling restart completes (60s timeout).

Validation rules:
- Per-field ranges: min/max replicas, CPU/memory targets, CPU request/limit (see `_RANGES` in `cluster_config.py`).
- Cross-field: `min_replicas ≤ max_replicas`, `cpu_request ≤ cpu_limit`.

Server-side guard: `apply` and `reset` refuse to run while `load_generator.is_running` is true, returning `validation_error`. This mirrors the UI gating but cannot be bypassed from a custom WebSocket client.

### 3.3 Dashboard Frontend (`dashboard/static/`)

A single-page application with no build step.

**Dependencies (CDN):**
- Chart.js v4.4.7
- chartjs-adapter-date-fns v3.0.0
- Google Fonts: Inter (UI), JetBrains Mono (metrics)

#### 3.3.1 UI Layout

```
┌─────────────────────────────────────────────────────────────┐
│ [Load Testing Dashboard]                  [● Connected]     │ Header
├─────────────────────────────────────────────────────────────┤
│ [Start] ──── RPS [=====●=====] 10 ──── Target [URL input]  │ Controls
├─────────────────────────────────────────────────────────────┤
│ Avg Response │ P90       │ P99       │ Actual RPS │ Replicas│ Stats bar
│ 45.2 ms      │ 120.0 ms  │ 350.1 ms  │ 98         │ 3       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│           Response Times (avg/p90/p99) — 280px              │ Main chart
│                                                             │
├──────────────────────────┬──────────────────────────────────┤
│   Active Replicas — 220px│   CPU Utilization — 220px        │ Side charts
├──────────────────────────┴──────────────────────────────────┤
│ Pod Name              │ Status  │ CPU          │ Memory     │ Pod table
│ python-api-abc12      │ Running │ [████░░] 65% │ 89.1 MB   │
│ python-api-def34      │ Running │ [███░░░] 42% │ 72.3 MB   │
└─────────────────────────────────────────────────────────────┘
```

#### 3.3.2 Charts Configuration

| Chart | Type | Datasets | Y-axis | Height |
|-------|------|----------|--------|--------|
| Response Times | `line` (smooth, tension 0.3) | Avg (green), P90 (yellow), P99 (red) | ms | 280px |
| Active Replicas | `line` (stepped) | Running (blue, filled), Desired (indigo, dashed) | pods (step=1) | 220px |
| CPU Utilization | `line` (smooth, filled) | Avg CPU % (indigo) | % (suggestedMax=100) | 220px |

All charts:
- X-axis: time scale, `HH:mm:ss` format, max 8 ticks
- Max data points: 300 (5-minute rolling window)
- Animation: 300ms
- Point radius: 0 (lines only)
- Update mode: `'none'` (no animation per frame, prevents jank)

#### 3.3.3 WebSocket Client

```
connect():
  1. Open ws://{host}/ws
  2. onopen → green dot, "Connected"
  3. onclose → red dot, "Disconnected", retry in 3s
  4. onmessage → parse JSON → updateDashboard()

send(msg):
  if ws.readyState === OPEN → ws.send(JSON.stringify(msg))
```

**State synchronization:** The server sends `is_running` in every metrics payload. If the frontend's local state diverges (e.g., after reconnection), it syncs to the server's state and updates the button.

#### 3.3.4 Design System

```css
--bg:       #0f1117   /* page background */
--surface:  #1a1d27   /* cards, header, table */
--border:   #2a2d3e   /* subtle separators */
--text:     #e1e4ed   /* primary text */
--text-dim: #8b8fa3   /* labels, secondary */
--accent:   #6366f1   /* indigo — slider, RPS, CPU chart */
--green:    #22c55e   /* avg response, running status, start button */
--yellow:   #eab308   /* p90, pending status, pause button */
--red:      #ef4444   /* p99, disconnected, danger CPU */
--blue:     #3b82f6   /* replicas, blue stat card */
```

Responsive at 768px breakpoint: side charts stack vertically, URL input takes full width.

---

## 4. WebSocket Protocol

Single endpoint: `ws://{host}:{port}/ws`

### 4.1 Client → Server Messages

| Action | Payload | Effect |
|--------|---------|--------|
| `start` | `{"action": "start"}` | Starts load generator (creates httpx client + ticker task) |
| `pause` | `{"action": "pause"}` | Stops load generator (cancels ticker, drains in-flight, closes client) |
| `set_rps` | `{"action": "set_rps", "value": 100}` | Sets target RPS [1..500], clamped |
| `set_url` | `{"action": "set_url", "value": "http://..."}` | Changes target URL for load generator |
| `get_cluster_config` | `{"action": "get_cluster_config"}` | Returns `{type: "cluster_config", current, defaults}` |
| `apply_cluster_config` | `{"action": "apply_cluster_config", "value": {<ClusterConfig>}}` | Validates, patches, returns `cluster_config_result` + refreshed `cluster_config` |
| `reset_cluster_config` | `{"action": "reset_cluster_config"}` | Equivalent to `apply` with YAML defaults |

### 4.2 Server → Client Messages (every 1 second)

```json
{
  "type": "metrics",
  "timestamp": 1743580200.123,
  "response_times": {
    "avg_ms": 45.23,
    "p90_ms": 120.50,
    "p99_ms": 350.12
  },
  "load": {
    "target_rps": 100,
    "actual_rps": 98,
    "is_running": true
  },
  "cluster": {
    "replicas": 3,
    "hpa_desired": 4,
    "hpa_current_cpu": 65,
    "pods": [
      {"name": "python-api-abc12", "cpu_percent": 65.2, "memory_mb": 89.1, "status": "Running"},
      {"name": "python-api-def34", "cpu_percent": 42.0, "memory_mb": 72.3, "status": "Running"},
      {"name": "python-api-ghi56", "cpu_percent": 58.7, "memory_mb": 95.0, "status": "Pending"}
    ]
  }
}
```

**`cluster_config`** (on connect + after apply/reset):

```json
{
  "type": "cluster_config",
  "current": {"min_replicas": 1, "max_replicas": 10, "target_cpu_utilization": 50, "target_memory_utilization": 70, "cpu_request_millicores": 100, "cpu_limit_millicores": 500},
  "defaults": {"min_replicas": 1, "max_replicas": 10, "target_cpu_utilization": 50, "target_memory_utilization": 70, "cpu_request_millicores": 100, "cpu_limit_millicores": 500}
}
```

**`cluster_config_result`** (apply/reset response):

```json
{
  "type": "cluster_config_result",
  "status": "ok",
  "error": null,
  "restart_triggered": true
}
```

### 4.3 Connection Lifecycle

```
Client connects → server accepts, adds to connected_clients set
                → server broadcasts metrics every 1s
Client sends commands → server routes to load_generator methods
Client disconnects → server removes from set (via WebSocketDisconnect or broadcast failure)
Client auto-reconnects → 3-second retry on disconnect
```

---

## 5. Kubernetes Configuration

### 5.1 Cluster Topology

```
k3d cluster "challenge-demo"
├── k3d-challenge-demo-server-0     (K3s server node)
├── k3d-challenge-demo-agent-0      (K3s agent node)
├── k3d-challenge-demo-agent-1      (K3s agent node)
└── k3d-challenge-demo-serverlb     (traefik LB, host:8080→cluster:80)
```

### 5.2 Deployment Spec

```yaml
image: python-api:latest
imagePullPolicy: Never          # uses k3d-imported image
replicas: 1                     # initial, HPA manages actual
resources:
  requests:
    cpu: 250m                   # scheduling guarantee + HPA baseline
    memory: 128Mi
  limits:
    cpu: 500m                   # hard cap (throttled beyond this)
    memory: 256Mi               # OOMKilled beyond this
```

**Probes:**

| Probe | Endpoint | Initial Delay | Period | Failure Threshold |
|-------|----------|---------------|--------|-------------------|
| Readiness | `GET /health:8000` | 5s | 5s | 3 (default) |
| Liveness | `GET /health:8000` | 10s | 10s | 3 (default) |

### 5.3 Service

- Type: `ClusterIP` (internal only; external access via Ingress/traefik)
- Port mapping: `:80` → `:8000`
- Selector: `app: python-api`
- DNS: `python-api.default.svc.cluster.local`

### 5.4 Ingress

- Controller: traefik (built into k3d)
- Annotation: `traefik.ingress.kubernetes.io/router.entrypoints: web`
- Routes: `/ (Prefix)` → `python-api:80`
- Effect: requests to `localhost:8080` reach the API through traefik → Service → Pods

### 5.5 Pod Resource Allocation

Each API pod is assigned explicit CPU and memory budgets that control scheduling, scaling, and runtime behavior. These values are defined in `k8s/deployment.yaml` under `spec.template.spec.containers[0].resources`.

#### 5.5.1 Current Allocation

```yaml
resources:
  requests:
    cpu: 100m          # 0.1 cores — guaranteed minimum
    memory: 128Mi      # 128 MiB — guaranteed minimum
  limits:
    cpu: 500m          # 0.5 cores — hard ceiling
    memory: 256Mi      # 256 MiB — hard ceiling (OOMKill beyond)
```

#### 5.5.2 What Each Value Means

| Field | Value | Role |
|-------|-------|------|
| `requests.cpu` | `100m` | Scheduler reservation. Kube-scheduler only places the pod on a node with ≥100m of free CPU. Also the **denominator for HPA's CPU utilization calculation**. |
| `requests.memory` | `128Mi` | Scheduler reservation for memory. Also the denominator for HPA memory utilization. |
| `limits.cpu` | `500m` | CFS quota enforced by the kernel. A pod burning 100% CPU is throttled at 0.5 cores (50ms per 100ms period). Burstable beyond request, capped at limit. |
| `limits.memory` | `256Mi` | RSS ceiling. Exceeding this triggers OOMKill and pod restart. |

#### 5.5.3 QoS Class

Because `requests < limits` for both CPU and memory, pods receive the **Burstable** QoS class:

- **Guaranteed** (req == limit): highest priority, last to be evicted.
- **Burstable** (req < limit): moderate priority, evicted after BestEffort under node pressure.
- **BestEffort** (no requests/limits): first to be evicted.

Burstable is appropriate here: we want pods to handle bursts up to 500m CPU while only reserving 100m on the node, allowing higher pod density per node.

#### 5.5.4 Interaction with HPA

The HPA target is `CPU utilization = 50%`. Utilization is computed as:

```
cpuUtilization = (pod_cpu_usage / cpu_request) * 100
              = (pod_cpu_cores / 0.100) * 100
```

So **50% utilization = 50m of actual CPU consumption** (half of the 100m request). When the *average* across all pods exceeds 50m, HPA scales up.

Example at steady load:
- 1 pod consuming 80m CPU → utilization = 80%. HPA target exceeded → scales up.
- 2 pods consuming 80m each (160m total) → utilization = 80%. Still over → scales to 3.
- 3 pods at 50m each → utilization = 50%. Stable.

Because the request (100m) is well below the limit (500m), a pod can *burn* up to 500m while only counting against its 100m baseline for utilization math. This is intentional: it gives the HPA headroom to trigger scaling before pods get throttled at the limit.

#### 5.5.5 Cluster-Wide Capacity at Max Scale

With `maxReplicas: 10`:

| Resource | Per pod (request) | At 10 pods | Per pod (limit) | At 10 pods |
|----------|------------------|-----------|-----------------|-----------|
| CPU | 100m | 1.0 core reserved | 500m | 5.0 cores burstable |
| Memory | 128Mi | 1.25 GiB reserved | 256Mi | 2.5 GiB ceiling |

The k3d cluster (1 server + 2 agents, default `--cpus=4` on Docker Desktop) has ~4 cores total and typically 6–8 GiB of RAM available. Scaling to 10 pods fits comfortably under reserved capacity, with room for system workloads (kube-system, traefik, metrics-server).

#### 5.5.6 Why These Specific Values

| Value | Reasoning |
|-------|-----------|
| CPU request `100m` | Low enough that HPA's 50% target triggers at realistic traffic (~50m of actual load). Higher requests (e.g. 250m) make HPA reluctant to scale unless requests are very heavy. |
| CPU limit `500m` | Allows a pod to absorb short spikes without throttling. 5× headroom over the request is standard for burstable workloads. |
| Memory request `128Mi` | FastAPI + uvicorn baseline is ~60–80Mi; 128Mi covers steady state with margin. |
| Memory limit `256Mi` | 2× headroom over the request. SHA-256 workload doesn't allocate meaningfully, so memory is unlikely to be the scaling signal. |

> **Note:** `dashboard/k8s_monitor.py` must stay in sync with `cpu_request_millicores`. If you change `requests.cpu` in the deployment, also update the constructor default in `K8sMonitor.__init__`, otherwise the dashboard's `cpu_percent` display will be wrong.

---

### 5.6 HPA (Horizontal Pod Autoscaler)

```yaml
apiVersion: autoscaling/v2
minReplicas: 1
maxReplicas: 10
```

**Metrics targets:**

| Metric | Target | Effect |
|--------|--------|--------|
| CPU utilization | 50% average | Primary scale trigger |
| Memory utilization | 70% average | Secondary safety net |

**Scaling behavior:**

| Direction | Stabilization Window | Policy | Rate |
|-----------|---------------------|--------|------|
| Scale up | 0s (immediate) | Pods | +2 every 30s |
| Scale down | 30s | Percent | -50% every 30s |

**Scaling formula example (CPU):**
```
desiredReplicas = ceil(currentReplicas * (currentCPU% / targetCPU%))
Example: 2 pods at 80% CPU → ceil(2 * 80/50) = ceil(3.2) = 4 pods
```

---

## 6. Data Flow Sequences

### 6.1 Load Generation Cycle

```
User clicks Start
  → Frontend sends {"action": "start"} via WebSocket
  → app.py receives, calls load_generator.start()
  → LoadGenerator creates httpx.AsyncClient + starts _ticker task
  → _ticker fires POST /work at target_rps interval
  → Each response → MetricsStore.record_response(elapsed_ms, status_code)
  → metrics_loop (1s) calls MetricsStore.compute_snapshot()
  → Snapshot + K8s metrics → broadcast to all WS clients
  → Frontend updates stats, charts, pod table
```

### 6.2 Autoscaling Cycle

```
Load generator sends traffic at high RPS
  → API pods consume CPU processing SHA-256 iterations
  → metrics-server scrapes kubelet for pod CPU usage (every ~15s)
  → HPA controller reads metrics-server (every ~15s)
  → If avg CPU > 50% target:
      → HPA computes desiredReplicas
      → Patches Deployment.spec.replicas
      → New pods scheduled on agent nodes
      → Pods pass readiness probe (5s) → receive traffic
  → k8s_monitor.py reads new pod count + metrics
  → Dashboard shows new replicas appearing in chart + table

User clicks Pause
  → Load stops → CPU drops
  → HPA waits 30s stabilization window
  → Scales down by up to 50% every 30s
  → Eventually returns to 1 replica
```

### 6.3 Dashboard Reconnection

```
WebSocket disconnects (network issue / server restart)
  → Frontend: red dot, "Disconnected"
  → setTimeout(connect, 3000) — retry
  → On reconnect: green dot, "Connected"
  → Next metrics payload includes is_running state
  → Frontend syncs Start/Pause button to server state
```

---

## 7. Network Topology

```
Port Map:
  localhost:3000  →  Dashboard Backend (uvicorn, host process)
  localhost:8080  →  k3d load balancer → traefik → Ingress → Service → Pods
  localhost:9090  →  kubectl port-forward → Service → Pods (optional direct access)

Internal (within cluster):
  python-api.default.svc:80  →  Pod:8000 (round-robin)
  metrics.k8s.io API         →  metrics-server (pod resource data)
  K8s API server (:6443)     →  cluster state, HPA, deployments
```

---

## 8. Deployment Pipeline

### 8.1 `scripts/setup.sh` — Cluster Bootstrap

```
1. Validate prerequisites: docker, kubectl, k3d (exits with install instructions if missing)
2. Check Docker daemon is running
3. Delete existing "challenge-demo" cluster if present
4. k3d cluster create challenge-demo --agents 2 -p "8080:80@loadbalancer"
5. kubectl wait --for=condition=Ready nodes --all --timeout=120s
6. Poll kubectl top nodes (up to 60s) to verify metrics-server
7. Print node status
```

### 8.2 `scripts/deploy.sh` — Build & Deploy

```
1. Verify cluster exists
2. docker build -t python-api:latest ./api
3. k3d image import python-api:latest -c challenge-demo
4. kubectl apply -f ./k8s/ (deployment, service, hpa, ingress)
5. kubectl rollout status deployment/python-api --timeout=120s
6. Print pod status + HPA status
```

### 8.3 `scripts/teardown.sh` — Clean Destroy

```
1. If cluster exists: k3d cluster delete challenge-demo
2. Removes all Docker containers, networks, and volumes for the cluster
```

### 8.4 Dashboard Startup (manual)

```bash
cd dashboard
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --port 3000
```

---

## 9. Resource Budgets

### 9.1 Host Machine Requirements

| Resource | Minimum | Recommended |
|----------|---------|-------------|
| Docker Desktop RAM | 6 GB | 8 GB |
| Docker Desktop CPUs | 4 | 6 |
| Disk | ~2 GB (images + cluster) | — |
| Python | 3.12+ | 3.12+ |

### 9.2 Per-Pod Resource Budget

| Resource | Request | Limit | Implication |
|----------|---------|-------|-------------|
| CPU | 250m | 500m | Guaranteed 0.25 cores, can burst to 0.5 before throttling |
| Memory | 128 Mi | 256 Mi | OOMKilled if exceeds 256 Mi |

### 9.3 Scaling Capacity

- Min pods: 1 (idle state)
- Max pods: 10 (HPA limit)
- Max total CPU request at scale: 10 * 250m = 2.5 cores
- Max total memory request at scale: 10 * 128 Mi = 1.25 Gi

### 9.4 Load Generator Limits

| Parameter | Value |
|-----------|-------|
| Max RPS | 500 |
| Max concurrent requests | 200 (semaphore) |
| Max HTTP connections | 200 (httpx pool) |
| Request timeout | 30s |
| Metrics window | 300s (5 minutes) |
| Metrics granularity | 1-second snapshots |

---

## 10. Technology Stack Summary

| Layer | Technology | Version | Purpose |
|-------|-----------|---------|---------|
| Container orchestration | k3d / K3s | latest | Local Kubernetes cluster |
| Container runtime | Docker Desktop | >= v24 | Container execution |
| Ingress controller | Traefik | (K3s built-in) | External → Service routing |
| Metrics | metrics-server | (K3s built-in) | Pod CPU/memory metrics |
| API framework | FastAPI | 0.115.12 | Python web framework |
| ASGI server | Uvicorn | 0.34.2 | HTTP + WebSocket server |
| HTTP client | httpx | 0.28.1 | Async load generation |
| K8s client | kubernetes-python | 32.0.1 | K8s API access |
| WS support | websockets | 15.0.1 | WebSocket protocol for uvicorn |
| Frontend charting | Chart.js | 4.4.7 | Time-series visualization |
| Date adapter | chartjs-adapter-date-fns | 3.0.0 | Time axis formatting |
| Fonts | Inter, JetBrains Mono | (CDN) | UI typography |

---

## 11. File Map

```
code-challenge/
├── api/
│   ├── Dockerfile              # python:3.12-slim, uvicorn CMD, curl healthcheck
│   ├── requirements.txt        # fastapi, uvicorn
│   └── main.py                 # GET /health, POST /work (SHA-256 CPU burn)
│
├── dashboard/
│   ├── requirements.txt        # fastapi, uvicorn, httpx, kubernetes, websockets
│   ├── app.py                  # FastAPI: WS hub, metrics_loop, static serving
│   ├── load_generator.py       # LoadGenerator: async ticker, httpx client, semaphore
│   ├── k8s_monitor.py          # K8sMonitor: pod metrics, HPA status, CPU/mem parsing
│   ├── metrics.py              # MetricsStore: response aggregation, percentiles, deque
│   └── static/
│       ├── index.html          # SPA: charts, controls, pod table, WS client (335 lines)
│       └── style.css           # Dark theme, CSS vars, responsive grid (334 lines)
│
├── k8s/
│   ├── deployment.yaml         # 1 replica, 250m/500m CPU, readiness+liveness probes
│   ├── service.yaml            # ClusterIP :80→:8000
│   ├── hpa.yaml                # autoscaling/v2, CPU 50%, 1-10 replicas, fast scale
│   └── ingress.yaml            # traefik, / → python-api:80
│
├── scripts/
│   ├── setup.sh                # prereqs check, k3d cluster create, wait for metrics
│   ├── deploy.sh               # docker build, k3d import, kubectl apply, rollout wait
│   └── teardown.sh             # k3d cluster delete
│
├── docs/
│   └── superpowers/specs/
│       └── 2026-04-02-load-testing-infra-design.md   # Original design document
│
└── README.md                   # Quick start, usage, project structure
```

---

## 12. Decision Log

| # | Decision | Choice | Alternatives Considered | Rationale |
|---|----------|--------|------------------------|-----------|
| 1 | K8s distribution | k3d (K3s in Docker) | minikube, kind, Docker Compose | Real HPA + built-in metrics-server, fast setup (~30s), clean teardown, native arm64 |
| 2 | Load generator | Custom async Python | Locust, k6, wrk, hey | Only option allowing instant RPS changes via shared variable without restart; tight integration with metrics pipeline |
| 3 | Dashboard framework | FastAPI + vanilla JS | Grafana, React, Streamlit | No build step, single HTML file, zero external tooling, sufficient for real-time demo |
| 4 | Metrics storage | In-memory deque | Prometheus, InfluxDB, SQLite | No DB needed; 5-min rolling window in ~300 datapoints is trivial to hold in memory |
| 5 | Dashboard location | Host (outside cluster) | Inside cluster as a pod | Avoids RBAC/ServiceAccount complexity, direct file editing, simpler development |
| 6 | Frontend framework | None (vanilla JS) | React, Vue, Svelte | Single HTML file, zero build tooling, Chart.js handles all visualization |
| 7 | Real-time protocol | WebSocket (bidirectional) | SSE, polling | Bidirectional: sends metrics downstream AND receives commands upstream in one connection |
| 8 | K8s API access | `~/.kube/config` (host) | In-cluster ServiceAccount | Dashboard runs on host; kubeconfig is already set by k3d |
| 9 | Image distribution | `k3d image import` | Local registry, build inside cluster | Simpler, no registry setup, works with `imagePullPolicy: Never` |
| 10 | HPA scaling speed | Fast (0s up / 30s down stabilization) | K8s defaults (0s up / 300s down) | Demo context: need visible scaling within seconds, not production stability |

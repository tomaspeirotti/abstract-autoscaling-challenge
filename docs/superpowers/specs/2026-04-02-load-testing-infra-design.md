# Load Testing & Autoscaling Infrastructure Design

## Context

Preparing infrastructure for a code challenge. The challenge will likely require building a Python SaaS API. The goal is to have a ready-to-use environment that:
- Dockerizes the API and runs it in a local Kubernetes cluster
- Autoscales replicas based on real load (CPU-based HPA)
- Simulates traffic with adjustable RPS in real time
- Monitors container resources and response times in a custom dashboard

Everything must run locally on macOS (Apple Silicon), be free, and be easy to start/pause.

---

## Architecture Overview

```
                         ┌─────────────────────────────────────────┐
                         │         Dashboard (browser)              │
                         │  Chart.js graphs + RPS slider + controls │
                         └────────────────┬────────────────────────┘
                                          │ WebSocket (bidirectional)
                                          ▼
                         ┌─────────────────────────────────────────┐
                         │      Dashboard Backend (FastAPI)         │
                         │  - Serves UI (static files)             │
                         │  - Load Generator (asyncio task)         │
                         │  - K8s metrics collector (3s polling)    │
                         │  - WebSocket hub                        │
                         └───────┬────────────────────┬────────────┘
                                 │                    │
                    HTTP requests │                    │ K8s API (python-client)
                    to localhost  │                    │ via ~/.kube/config
                                 ▼                    ▼
                         ┌──────────────┐    ┌──────────────────┐
                         │  K8s Service  │    │  metrics-server   │
                         │  (ClusterIP)  │    │  (built into k3s) │
                         └──────┬───────┘    └──────────────────┘
                                │ round-robin
                    ┌───────────┼───────────┐
                    ▼           ▼           ▼
               ┌─────────┐ ┌─────────┐ ┌─────────┐
               │  Pod 1   │ │  Pod 2   │ │  Pod N   │   ◄── HPA scales this
               │ Python API│ │ Python API│ │ Python API│
               └─────────┘ └─────────┘ └─────────┘
```

The dashboard runs **outside the cluster** (on the host Mac) and connects to K8s via `~/.kube/config`. This avoids RBAC configuration and keeps the setup simple.

---

## Component 1: Kubernetes Cluster (k3d)

### Why k3d
- Runs K3s (lightweight K8s) inside Docker containers
- Single `brew install k3d` + one command to create a cluster (~30s)
- K3s ships with **metrics-server built-in** — no extra install
- Native arm64 support on Apple Silicon
- Clean teardown: `k3d cluster delete` removes everything

### Cluster Setup
```bash
k3d cluster create challenge-demo --agents 2 -p "8080:80@loadbalancer"
```
- 1 server node + 2 agent nodes
- Port 8080 on host mapped to K3s load balancer

### Prerequisites
- Docker Desktop for Mac: **6GB RAM, 4 CPUs minimum** in Settings > Resources
- All images must be arm64 (no QEMU emulation)

---

## Component 2: Kubernetes Manifests

### Deployment (`k8s/deployment.yaml`)
- Image: `python-api:latest` (built locally, imported via `k3d image import`)
- Container port: 8000
- Resource requests: 100m CPU, 128Mi memory (required for HPA)
- Resource limits: 500m CPU, 256Mi memory
- Readiness probe: HTTP GET `/health` every 5s

### Service (`k8s/service.yaml`)
- Type: ClusterIP
- Port 80 → targetPort 8000
- Selector: `app: python-api`
- Provides internal load balancing and DNS (`python-api.default.svc`)

### HPA (`k8s/hpa.yaml`)
- API version: `autoscaling/v2`
- Target: 50% average CPU utilization
- Min replicas: 1, Max replicas: 10
- Scale-up: immediate, up to +2 pods every 30s
- Scale-down: 60s stabilization window (faster than 5min default, for demo purposes)
- Also monitors memory at 70% threshold

### Exposing to Host
```bash
kubectl port-forward svc/python-api 9090:80
```
The load generator targets `http://localhost:9090`.

---

## Component 3: Python API (Placeholder)

A minimal FastAPI app that will be replaced with the actual challenge code.

### Endpoints
- `GET /health` — returns `{"status": "ok"}` (for readiness probe)
- `POST /work` — CPU-intensive endpoint for triggering autoscaling during testing (e.g., iterative hashing, fibonacci computation)

### Dockerfile
- Base: `python:3.12-slim` (arm64 native)
- Install dependencies via pip
- Run with `uvicorn main:app --host 0.0.0.0 --port 8000`
- HEALTHCHECK: `curl -f http://localhost:8000/health`

---

## Component 4: Dashboard Backend (FastAPI)

A single FastAPI process running on the host that orchestrates everything.

### Modules

**`app.py`** — Main FastAPI application:
- Serves static files (index.html, style.css)
- WebSocket endpoint at `/ws` (bidirectional)
- REST fallback endpoints for controls
- On startup: initializes load generator + K8s monitor as asyncio tasks

**`load_generator.py`** — Async HTTP load generator:
- Uses `httpx.AsyncClient` with connection pooling
- Shared state: `target_rps` (float), `is_running` (bool)
- For RPS > 50: spawns `ceil(target_rps / 50)` concurrent worker coroutines
- Records each response in a shared metrics buffer: `(timestamp, elapsed_ms, status_code)`
- RPS changes take effect on the next request cycle (instant)

**`k8s_monitor.py`** — Kubernetes metrics collector:
- Uses `kubernetes` Python client with `load_kube_config()`
- Polls every 3 seconds:
  - `CustomObjectsApi.list_namespaced_custom_object(group="metrics.k8s.io", ...)` → per-pod CPU/memory
  - `CoreV1Api.list_namespaced_pod(label_selector="app=python-api")` → pod count/status
  - `AutoscalingV2Api.read_namespaced_horizontal_pod_autoscaler(...)` → HPA status
- Parses K8s metric formats: "25m" → 0.025 cores, "65536Ki" → 64 MiB

**`metrics.py`** — Metrics aggregation:
- `collections.deque(maxlen=300)` — 5-minute rolling window of per-second aggregates
- Each entry: `{timestamp, avg_ms, p90_ms, p99_ms, actual_rps, replicas, pods: [{name, cpu, mem_mb}]}`
- Percentiles via `numpy.percentile()` over raw response times in the current second

### WebSocket Protocol

**Server → Client (every 1s):**
```json
{
  "type": "metrics",
  "timestamp": "2026-04-02T15:30:00Z",
  "response_times": {"avg_ms": 45, "p90_ms": 120, "p99_ms": 350},
  "load": {"target_rps": 100, "actual_rps": 98},
  "cluster": {
    "replicas": 3,
    "hpa_desired": 4,
    "pods": [
      {"name": "python-api-abc12", "cpu_percent": 65, "memory_mb": 89},
      {"name": "python-api-def34", "cpu_percent": 42, "memory_mb": 72},
      {"name": "python-api-ghi56", "cpu_percent": 58, "memory_mb": 95}
    ]
  }
}
```

**Client → Server:**
```json
{"action": "start"}
{"action": "pause"}
{"action": "set_rps", "value": 100}
```

---

## Component 5: Dashboard Frontend

A single-page application served as static files. No build step.

### Tech
- **Chart.js** (v4) + **chartjs-plugin-streaming** + **chartjs-adapter-date-fns** from CDN
- Vanilla JavaScript (no framework)
- WebSocket client for real-time data

### Layout
- **Header:** Title + status indicator (connected/disconnected)
- **Controls bar:** Start/Pause button + RPS slider (1-500) with numeric display
- **Chart 1 (main, wide):** Response times — 3 lines (avg=green, p90=yellow, p99=red), scrolling 5-min window
- **Chart 2 (half width):** Active replicas over time (step line chart)
- **Chart 3 (half width):** Average CPU utilization across pods over time
- **Pod table (bottom):** Live table showing each pod's name, CPU%, memory MB, status

### Styling
- Dark theme (CSS custom properties: `--bg`, `--surface`, `--text`, `--accent`)
- Card-based layout with subtle borders and rounded corners
- Responsive: works on any screen width
- Monospace font for metrics, sans-serif for labels

---

## File Structure

```
code-challenge/
├── api/
│   ├── Dockerfile
│   ├── requirements.txt          # fastapi, uvicorn
│   └── main.py                   # Placeholder API with /health and /work
├── dashboard/
│   ├── requirements.txt          # fastapi, uvicorn, httpx, numpy, kubernetes
│   ├── app.py                    # FastAPI server: WebSocket, static files, orchestration
│   ├── load_generator.py         # Async load gen with adjustable RPS
│   ├── k8s_monitor.py            # K8s metrics collector
│   ├── metrics.py                # Aggregation, percentiles, rolling window
│   └── static/
│       ├── index.html            # Dashboard SPA
│       └── style.css             # Dark theme
├── k8s/
│   ├── deployment.yaml           # API Deployment with resource requests
│   ├── service.yaml              # ClusterIP Service
│   └── hpa.yaml                  # HPA autoscaling rules
├── scripts/
│   ├── setup.sh                  # Install k3d + create cluster
│   ├── deploy.sh                 # Build image + import to k3d + apply manifests
│   └── teardown.sh               # Delete cluster
└── README.md
```

---

## Dependencies

### Host (macOS)
- Docker Desktop (>= v24)
- k3d (via Homebrew)
- kubectl (via Homebrew)
- Python 3.12+

### Dashboard Python packages
- `fastapi` + `uvicorn[standard]` — web server + WebSocket
- `httpx` — async HTTP client for load generation
- `numpy` — percentile calculations
- `kubernetes` — K8s API client
- `websockets` — WebSocket support for uvicorn

### Frontend (CDN, no install)
- Chart.js v4
- chartjs-plugin-streaming
- chartjs-adapter-date-fns

---

## Verification Plan

1. **Cluster setup:** `./scripts/setup.sh` → verify `kubectl get nodes` shows 3 nodes Ready
2. **Metrics available:** `kubectl top nodes` returns CPU/memory within 60s
3. **API deployed:** `./scripts/deploy.sh` → `kubectl get pods` shows 1 pod Running
4. **Port forward:** `kubectl port-forward svc/python-api 9090:80` → `curl localhost:9090/health` returns 200
5. **Dashboard starts:** `cd dashboard && uvicorn app:app --port 3000` → browser at localhost:3000 loads UI
6. **WebSocket connected:** dashboard shows "Connected" status
7. **Load generation works:** set RPS to 10, click Start → response time chart updates in real time
8. **Autoscaling up:** increase RPS to 200+ → within 30-60s, `kubectl get hpa` shows replicas increasing, dashboard shows new pods appearing
9. **Autoscaling down:** click Pause → within 60-90s, replicas decrease back toward 1
10. **Cleanup:** `./scripts/teardown.sh` → `docker ps` shows no k3d containers

---

## Decisions and Trade-offs

| Decision | Choice | Why |
|---|---|---|
| Orchestration | k3d (K3s in Docker) | Real HPA autoscaling, metrics-server included, fast setup |
| Load generator | Custom async Python | Only option that allows instant RPS changes via shared variable |
| Dashboard | FastAPI + Chart.js | Simple (no build step), pretty (dark theme + streaming charts), local |
| Metrics storage | In-memory deque | No DB needed for a demo; 5-min rolling window sufficient |
| Dashboard location | Outside cluster (host) | Avoids RBAC, simpler setup, direct file editing |
| Frontend framework | None (vanilla JS) | Single HTML file, zero build tooling |
| WebSocket vs SSE | WebSocket | Bidirectional: receives metrics AND sends commands |

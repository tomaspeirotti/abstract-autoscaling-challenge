# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Load testing and autoscaling infrastructure for comparing two equivalent APIs side-by-side: a **Python/FastAPI** API and a **Rust/axum** API, both implementing the same SHA-256 iterative algorithm on `POST /work`. Both run in a local k3d Kubernetes cluster with HPA. A FastAPI dashboard generates configurable HTTP load, visualizes response time / replica count / CPU per pod in real time via WebSocket, and lets the user edit cluster config (HPA + Deployment resources) live from the UI.

The Python stack is always deployed. The Rust stack is created on demand via a **dual-stack toggle** in the dashboard.

## Commands

### Full stack (cluster + both APIs + dashboard)
```bash
./start.sh                    # Creates k3d cluster, builds/deploys both APIs, starts dashboard on :3000
```

### Rebuild and redeploy both APIs (after editing api/ or api-rust/)
```bash
./scripts/deploy.sh           # Rebuilds python-api + rust-api images, re-imports, applies base manifests
```

### Teardown cluster
```bash
./scripts/teardown.sh
```

### Run dashboard independently
```bash
cd dashboard && source .venv/bin/activate
uvicorn app:app --port 3000
```

### Tests
```bash
cd dashboard && source .venv/bin/activate
pytest tests/                 # cluster_config unit tests
```

### Kubernetes inspection
```bash
kubectl get pods -l 'app in (python-api,rust-api)'
kubectl top pods -l 'app in (python-api,rust-api)'
kubectl get hpa
kubectl logs -l app=python-api --tail=50
kubectl logs -l app=rust-api --tail=50
```

## Architecture

### APIs — same contract, different runtime

Both expose `GET /health` (→ `{"status": "ok"}`) and `POST /work?iterations=N` (→ `N` chained SHA-256 hashes starting from `b"seed"`, returns `{iterations, elapsed_ms}`).

- **Python API (`api/`)** — FastAPI + uvicorn + `hashlib` (OpenSSL-backed). Container listens on :8000.
- **Rust API (`api-rust/`)** — axum + tokio multi-thread + `sha2` crate. Container listens on :8000. Dockerfile is multi-stage (cargo build → debian:stable-slim).

Both deploy as separate Deployments (`python-api`, `rust-api`) with matching HPA config (CPU 50% + memory 70%, min 1 / max 10) and matching resource requests (CPU 100m request / 500m limit by default — editable live via the UI).

### Dashboard (`dashboard/`)

- `app.py` — FastAPI backend with a single `/ws` WebSocket for bidirectional control + metrics streaming. Handles these WS actions: `start`, `pause`, `set_rps`, `get_cluster_config`, `apply_cluster_config`, `reset_cluster_config`.
- `cluster_config.py` — `ClusterConfigManager`: reads live HPA + Deployment state, validates changes, patches both stacks in dual mode, creates/deletes the rust stack on toggle, waits for rollouts when resources change. All K8s calls via `asyncio.to_thread`.
- `load_generator.py` — async httpx client firing at configurable RPS (1–500) with 200 max concurrent (semaphore). In dual mode, splits load between python and rust URLs.
- `k8s_monitor.py` — polls pod metrics + HPA state via the kubernetes Python client. Holds `cpu_request_millicores` for per-pod CPU % math; `ClusterConfigManager.apply` updates it when resources change.
- `metrics.py` — `MetricsStore` with rolling 300s window, per-second snapshots (avg, p90, p99), tagged by stack.
- `static/` — vanilla HTML/CSS + Chart.js. Top panel consolidates RPS/iterations controls and cluster-config editor with Spanish tooltips. HPA and Deployment fields split into side-by-side cards. Dual-stack toggle lives in the cluster-config panel.
- `tests/` — pytest suite for `cluster_config` validation + apply logic.
- `logs/dashboard.log` — rolling logs (also stdout).

Dashboard runs **outside** the cluster, connects via kubeconfig and hits the APIs through the ingress.

### Key data flow

Browser ←WebSocket→ `app.py` broadcasts a metrics payload every 1s. `LoadGenerator` records responses into `MetricsStore` tagged by stack. `K8sMonitor` polls pod/HPA state for both stacks. Config edits flow: UI → WS `apply_cluster_config` → `ClusterConfigManager.apply` → patch HPA + Deployment (both stacks if dual) → wait rollout if resources changed → rebroadcast current config.

### K8s manifests (`k8s/`)

- `deployment.yaml` / `service.yaml` / `hpa.yaml` — python-api. Applied by `deploy.sh`.
- `rust-deployment.yaml` / `rust-service.yaml` / `rust-hpa.yaml` — rust-api. Applied **only** by `ClusterConfigManager` when the dual-stack toggle is enabled from the UI. Deleted when toggled off.
- `ingress.yaml` — Traefik IngressRoute + two `stripPrefix` middlewares. `/py/*` → python-api, `/rs/*` → rust-api, `/` (no prefix) → python-api for backward compat. Strip middleware removes the prefix so each backend sees only `/health` / `/work`.
- Both HPAs: autoscaling/v2, aggressive scale-up (0s stabilization, +2 pods/30s), moderate scale-down (30s stabilization, -50%/30s).
- All Deployments use `imagePullPolicy: Never` (images imported via `k3d image import`).

## Dual-stack behavior

When `dual_stack_enabled` flips from false to true, `ClusterConfigManager._apply_rust_manifests_sync` loads the three rust YAMLs, overrides HPA replicas/targets and Deployment resources with the current live config (so both stacks start aligned for a fair comparison), and creates them via the K8s API. Toggling off deletes HPA → Deployment → Service in order. Subsequent config edits patch **both** stacks.

## Documentation

- `README.md` — user-facing overview + quick start.
- `docs/work-algorithm-comparison.md` — why `/work` performs differently in Python vs Rust despite the same algorithm (interpreter overhead, FFI, concurrency model, HTTP stack, SHA-NI in OpenSSL vs portable Rust).
- `docs/api-stacks-comparison.md` — broader backend stack comparison for SaaS.
- `docs/architecture-spec.md`, `docs/architecture-spec-v2.md` — challenge architecture specs.
- `docs/rust-aws-architecture.md` — productionized AWS architecture proposal for the Rust API.
- `docs/diagrams/` — referenced diagrams.

## Swapping an API

Replace `api/main.py` (+ `api/requirements.txt`) or `api-rust/src/main.rs` (+ `Cargo.toml`) with the real challenge code. Each must expose `GET /health` returning 200 and `POST /work` for the load generator to hit. Then run `./scripts/deploy.sh`.

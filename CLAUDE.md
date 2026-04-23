# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Is

Load testing and autoscaling infrastructure: a Python API runs inside a local k3d Kubernetes cluster with HPA autoscaling, while a FastAPI dashboard sends configurable HTTP load and visualizes response times, replica count, and CPU utilization in real time via WebSocket.

## Commands

### Full stack (cluster + API + dashboard)
```bash
./start.sh                    # Creates k3d cluster, builds/deploys API, starts dashboard on :3000
```

### Rebuild and redeploy API only (after editing api/)
```bash
./scripts/deploy.sh
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

### Kubernetes inspection
```bash
kubectl get pods -l app=python-api
kubectl top pods -l app=python-api
kubectl get hpa
kubectl logs -l app=python-api --tail=50
```

## Architecture

Two independent Python apps, no shared package:

### API (`api/`)
- FastAPI app served by uvicorn on port 8000 inside the container
- Placeholder CPU-intensive endpoint (`POST /work`) + health check (`GET /health`)
- Deployed to k3d cluster as `python-api` Deployment (1 replica, HPA scales 1-10)
- Exposed via Traefik Ingress on `localhost:8080`
- CPU request: 100m, limit: 500m — HPA targets 50% CPU utilization

### Dashboard (`dashboard/`)
- FastAPI backend (`app.py`) with a single WebSocket endpoint (`/ws`) for bidirectional control and metrics streaming
- `LoadGenerator`: async httpx client firing requests at configurable RPS (1-500), with 200 max concurrent via semaphore
- `K8sMonitor`: reads pod metrics, HPA status via kubernetes Python client (blocking calls run in `asyncio.to_thread`)
- `MetricsStore`: rolling 300s window of response records, computes per-second snapshots (avg, p90, p99)
- Static frontend at `dashboard/static/` (vanilla HTML/CSS + Chart.js)
- Runs outside the cluster, connects to k3d via kubeconfig and to the API via the Ingress

### Key data flow
Browser ←WebSocket→ `app.py` broadcasts metrics every 1s. `LoadGenerator` records responses into `MetricsStore`. `K8sMonitor` polls k8s API for pod/HPA state. Both feed into the broadcast payload.

### K8s manifests (`k8s/`)
- `deployment.yaml`: single-container pod, `imagePullPolicy: Never` (uses locally-imported image)
- `hpa.yaml`: autoscaling/v2 with aggressive scale-up (0s stabilization, +2 pods/30s) and moderate scale-down (30s stabilization, -50%/30s)
- `ingress.yaml`: Traefik ingress routing all traffic to the `python-api` ClusterIP service

## Swapping the API

Replace `api/main.py` and `api/requirements.txt` with the real challenge code. Must expose `GET /health` (returns 200). Then run `./scripts/deploy.sh`.

# Code Challenge - Load Testing & Autoscaling Infrastructure

Local infrastructure for load testing a Python API with Kubernetes autoscaling and a real-time monitoring dashboard.

## Architecture

```
Browser (Dashboard)  ←WebSocket→  Dashboard Backend (FastAPI)
                                       │              │
                                  HTTP requests   K8s API
                                       ▼              ▼
                                  K8s Service    metrics-server
                                       │
                              ┌────────┼────────┐
                              Pod 1   Pod 2   Pod N  ← HPA autoscales
```

## Prerequisites

- **Docker Desktop** (>= v24) — Settings > Resources: 6GB RAM, 4 CPUs minimum
- **k3d**: `brew install k3d`
- **kubectl**: `brew install kubectl`
- **Python 3.12+**

## Quick Start

```bash
./start.sh
```

This single command will:
1. Check prerequisites (Docker, k3d, kubectl, python3)
2. Create the k3d cluster (or reuse existing)
3. Build and deploy the API
4. Set up Python venv and install dependencies
5. Open a second terminal with `kubectl` pod monitoring
6. Start the dashboard and open http://localhost:3000

## Usage

1. Open the dashboard at `http://localhost:3000`
2. Set the target RPS with the slider
3. Click **Start** to begin sending load
4. Watch:
   - Response time charts (avg, p90, p99)
   - Replica count changing as HPA scales pods
   - CPU utilization per pod
5. Click **Pause** to stop load generation

## Swapping the Placeholder API

When you have the real challenge API:

1. Replace `api/main.py` with your code
2. Update `api/requirements.txt`
3. Ensure a `GET /health` endpoint exists (returns 200)
4. Run `./scripts/deploy.sh` to rebuild and redeploy
5. Update the target URL in the dashboard if needed

## Teardown

```bash
./scripts/teardown.sh
```

## Project Structure

```
api/                  Python API (placeholder)
dashboard/            Load testing dashboard
  app.py              FastAPI backend (WebSocket + REST)
  load_generator.py   Async HTTP load generator
  k8s_monitor.py      Kubernetes metrics collector
  metrics.py          Response time aggregation
  static/             Frontend (Chart.js dashboard)
k8s/                  Kubernetes manifests
  deployment.yaml     API Deployment with resource requests
  service.yaml        ClusterIP Service
  hpa.yaml            HPA autoscaling rules
scripts/              Setup, deploy, and teardown scripts
```

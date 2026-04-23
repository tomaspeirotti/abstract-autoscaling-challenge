# Code Challenge — Load Testing & Autoscaling Infrastructure

Infra local para load-testear dos APIs equivalentes (Python y Rust) corriendo en Kubernetes con HPA, lado a lado, y comparar su comportamiento bajo carga desde un dashboard en tiempo real.

## Qué incluye

- **Dos APIs** con el mismo contrato (`POST /work`, `GET /health`) y el mismo algoritmo (SHA-256 iterativo), una en **Python/FastAPI** y otra en **Rust/axum**.
- **Cluster local k3d** con HPA, metrics-server e ingress Traefik que rutea `/py/*` → python-api y `/rs/*` → rust-api.
- **Dashboard FastAPI + WebSocket** que dispara carga configurable, grafica latencias (avg/p90/p99), cuenta réplicas y CPU por pod, y permite **editar la configuración del cluster desde la UI** (réplicas, target CPU/memoria, CPU request/limit, modo dual-stack).
- **Modo dual-stack**: toggle que despliega/remueve el stack de Rust en caliente; la carga se reparte entre ambas APIs y el dashboard las compara en paralelo.
- **Documentación técnica** bajo [docs/](docs/) (ver [Documentos](#documentos)).

## Arquitectura

```
Browser (Dashboard UI)  ←WebSocket→  Dashboard backend (FastAPI)
                                          │           │         │
                                    HTTP requests  K8s API   Config patches
                                          ▼           ▼         ▼
                                      Ingress    metrics-    HPA + Deployment
                                     (Traefik)    server     (python + rust)
                                     /py/*  /rs/*
                                       │      │
                                  ┌────┴──┐ ┌─┴────┐
                                  python-  rust-
                                    api     api      ← HPA scales 1..N cada stack
```

## Prerrequisitos

- **Docker Desktop** (≥ v24) — recomendado: 6 GB RAM, 4 CPUs
- **k3d**: `brew install k3d`
- **kubectl**: `brew install kubectl`
- **Python 3.12+**

(Para compilar Rust localmente no hace falta toolchain: la imagen se construye dentro del Dockerfile multi-stage.)

## Quick start

```bash
./start.sh
```

El script:
1. Chequea prerrequisitos.
2. Crea el cluster k3d (o reusa el existente).
3. Buildea **ambas** imágenes (python-api y rust-api) y las importa al cluster.
4. Aplica los manifests del stack de Python + ingress. El stack de Rust se crea on-demand desde la UI.
5. Crea el venv del dashboard e instala dependencias.
6. Abre una terminal extra con watch de pods.
7. Levanta el dashboard en `http://localhost:3000`.

## Uso del dashboard

1. Abrir `http://localhost:3000`.
2. **Cluster config** (panel superior): ajustar min/max replicas, target CPU/memory utilization, CPU request/limit. Los cambios se aplican vía K8s API y se esperan rollouts si tocan `resources`.
3. **Toggle dual-stack**: activa el stack de Rust (despliega `rust-deployment.yaml` + `rust-service.yaml` + `rust-hpa.yaml` con la misma config que Python para una comparación justa). Desactivarlo lo borra.
4. **Load generator**: fijar RPS objetivo (1–500) y `iterations` para `/work`. Start/Pause.
5. Mirar en tiempo real:
   - Latencias (avg, p90, p99), separadas por stack en modo dual.
   - Cantidad de réplicas por Deployment.
   - Utilización de CPU por pod.
   - Eventos de scaling del HPA.

El dashboard escribe logs en [dashboard/logs/dashboard.log](dashboard/logs/dashboard.log).

## Las dos APIs

Ambas exponen el mismo contrato:

| Endpoint | Respuesta |
|---|---|
| `GET /health` | `{"status": "ok"}` |
| `POST /work?iterations=N` | `{"iterations": N, "elapsed_ms": ...}` — ejecuta `N` SHA-256 encadenados |

- **Python** — [api/main.py](api/main.py). FastAPI + uvicorn + `hashlib` (OpenSSL).
- **Rust** — [api-rust/src/main.rs](api-rust/src/main.rs). axum + tokio multi-thread + crate `sha2`.

**Por qué importa**: el algoritmo es el mismo pero el rendimiento no. El análisis de las causas está en [docs/work-algorithm-comparison.md](docs/work-algorithm-comparison.md).

## Ingress

Todos los paths pasan por Traefik (`localhost:8080`):

- `http://localhost:8080/py/health` → python-api
- `http://localhost:8080/py/work` → python-api
- `http://localhost:8080/rs/health` → rust-api (solo en modo dual)
- `http://localhost:8080/rs/work` → rust-api (solo en modo dual)

El middleware `stripPrefix` deja que cada backend vea solo `/health` o `/work`.

## Tests

El módulo de cluster config tiene suite de tests:

```bash
cd dashboard && source .venv/bin/activate
pytest tests/
```

## Redeploy manual

Después de editar cualquiera de las dos APIs:

```bash
./scripts/deploy.sh
```

Rebuildea ambas imágenes, las reimporta al cluster y aplica los manifests base. El modo dual-stack (si está activo) sobrevive porque los manifests de Rust ya están en el cluster.

## Teardown

```bash
./scripts/teardown.sh
```

## Estructura del repo

```
api/                   Python API (FastAPI, hashlib SHA-256)
api-rust/              Rust API (axum, tokio, sha2 crate)
dashboard/
  app.py               FastAPI backend: WebSocket + handlers de acciones
  cluster_config.py    ClusterConfigManager: lee/valida/aplica HPA + Deployment
  load_generator.py    Cliente httpx async con semáforo de concurrencia
  k8s_monitor.py       Polling de pods/HPA vía cliente K8s
  metrics.py           Ventana rolling de 300s con p90/p99
  static/              Frontend vanilla + Chart.js
  tests/               Tests de cluster_config
  logs/                dashboard.log
k8s/
  deployment.yaml      python-api Deployment
  service.yaml         python-api Service
  hpa.yaml             python-api HPA (CPU 50% + memory 70%)
  rust-deployment.yaml rust-api Deployment (aplicado on-demand)
  rust-service.yaml    rust-api Service
  rust-hpa.yaml        rust-api HPA
  ingress.yaml         Traefik IngressRoute con strip /py y /rs
scripts/               setup / deploy / teardown
docs/                  Docs técnicas (ver abajo)
```

## Documentos

- [docs/work-algorithm-comparison.md](docs/work-algorithm-comparison.md) — Por qué `/work` responde distinto en Python y Rust aunque el algoritmo sea el mismo.
- [docs/api-stacks-comparison.md](docs/api-stacks-comparison.md) — Comparativa amplia de stacks de backend para SaaS (más allá de este repo).
- [docs/architecture-spec.md](docs/architecture-spec.md) / [docs/architecture-spec-v2.md](docs/architecture-spec-v2.md) — Specs de arquitectura del challenge.
- [docs/rust-aws-architecture.md](docs/rust-aws-architecture.md) — Propuesta de arquitectura productiva en AWS para la API de Rust.
- [docs/diagrams/](docs/diagrams/) — Diagramas referenciados por las specs.

## Notas útiles

```bash
kubectl get pods -l 'app in (python-api,rust-api)'
kubectl top pods -l 'app in (python-api,rust-api)'
kubectl get hpa
kubectl logs -l app=python-api --tail=50
kubectl logs -l app=rust-api --tail=50
```

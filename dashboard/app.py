import asyncio
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles

from cluster_config import ClusterConfig, ClusterConfigManager
from k8s_monitor import K8sMonitor
from load_generator import LoadGenerator
from metrics import MetricsStore

TARGET_URL = os.environ.get("TARGET_URL", "http://localhost:8080/work")
K8S_MANIFESTS_DIR = Path(__file__).parent.parent / "k8s"

app = FastAPI(title="Load Testing Dashboard")

# Shared state
metrics_store = MetricsStore()
load_generator = LoadGenerator(target_url=TARGET_URL, metrics_store=metrics_store)
k8s_monitor = K8sMonitor()
cluster_config_manager = ClusterConfigManager(
    namespace="default",
    deployment_name="python-api",
    hpa_name="python-api-hpa",
    k8s_manifests_dir=K8S_MANIFESTS_DIR,
    k8s_monitor=k8s_monitor,
)
connected_clients: set[WebSocket] = set()


async def broadcast(data: dict) -> None:
    """Send data to all connected WebSocket clients."""
    if not connected_clients:
        return
    message = json.dumps(data, default=str)
    disconnected = set()
    for ws in connected_clients:
        try:
            await ws.send_text(message)
        except Exception:
            disconnected.add(ws)
    connected_clients.difference_update(disconnected)


async def build_cluster_config_message() -> dict:
    """Fetch live config + defaults; wrap in a WS message."""
    try:
        current = await cluster_config_manager.get_current()
        current_dict = asdict(current)
    except Exception as e:
        print(f"get_current failed: {e}")
        current_dict = None
    try:
        defaults = cluster_config_manager.get_defaults()
        defaults_dict = asdict(defaults)
    except Exception as e:
        print(f"get_defaults failed: {e}")
        defaults_dict = None
    return {
        "type": "cluster_config",
        "current": current_dict,
        "defaults": defaults_dict,
    }


async def metrics_loop() -> None:
    """Periodically compute and broadcast metrics."""
    while True:
        try:
            # Compute response time metrics
            response_snapshot = metrics_store.compute_snapshot()

            # Fetch K8s cluster metrics
            try:
                cluster_snapshot = await k8s_monitor.get_metrics()
                cluster_data = {
                    "replicas": cluster_snapshot.replicas,
                    "hpa_desired": cluster_snapshot.hpa_desired,
                    "hpa_current_cpu": cluster_snapshot.hpa_current_cpu,
                    "pods": [asdict(p) for p in cluster_snapshot.pods],
                }
            except Exception:
                cluster_data = {
                    "replicas": 0,
                    "hpa_desired": 0,
                    "hpa_current_cpu": None,
                    "pods": [],
                }

            payload = {
                "type": "metrics",
                "timestamp": time.time(),
                "response_times": {
                    "avg_ms": response_snapshot.avg_ms,
                    "p90_ms": response_snapshot.p90_ms,
                    "p99_ms": response_snapshot.p99_ms,
                },
                "load": {
                    "target_rps": load_generator.target_rps,
                    "actual_rps": response_snapshot.actual_rps,
                    "is_running": load_generator.is_running,
                },
                "cluster": cluster_data,
            }
            await broadcast(payload)
        except Exception as e:
            print(f"Metrics loop error: {e}")

        await asyncio.sleep(1)


@app.on_event("startup")
async def startup() -> None:
    asyncio.create_task(metrics_loop())


async def handle_apply_cluster_config(value: dict) -> None:
    """Validate and apply a new ClusterConfig. Broadcasts result + refreshed config."""
    if load_generator.is_running:
        await broadcast({
            "type": "cluster_config_result",
            "status": "validation_error",
            "error": "load generator is running",
            "restart_triggered": False,
        })
        return
    try:
        new_cfg = ClusterConfig(
            min_replicas=int(value["min_replicas"]),
            max_replicas=int(value["max_replicas"]),
            target_cpu_utilization=int(value["target_cpu_utilization"]),
            target_memory_utilization=int(value["target_memory_utilization"]),
            cpu_request_millicores=int(value["cpu_request_millicores"]),
            cpu_limit_millicores=int(value["cpu_limit_millicores"]),
        )
    except (KeyError, TypeError, ValueError) as e:
        await broadcast({
            "type": "cluster_config_result",
            "status": "validation_error",
            "error": f"malformed payload: {e}",
            "restart_triggered": False,
        })
        return

    result = await cluster_config_manager.apply(new_cfg)
    await broadcast({
        "type": "cluster_config_result",
        "status": result.status,
        "error": result.error,
        "restart_triggered": result.restart_triggered,
    })
    # Always broadcast refreshed config so clients re-sync.
    await broadcast(await build_cluster_config_message())


async def handle_reset_cluster_config() -> None:
    if load_generator.is_running:
        await broadcast({
            "type": "cluster_config_result",
            "status": "validation_error",
            "error": "load generator is running",
            "restart_triggered": False,
        })
        return
    defaults = cluster_config_manager.get_defaults()
    result = await cluster_config_manager.apply(defaults)
    await broadcast({
        "type": "cluster_config_result",
        "status": result.status,
        "error": result.error,
        "restart_triggered": result.restart_triggered,
    })
    await broadcast(await build_cluster_config_message())


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    connected_clients.add(ws)
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action")

            if action == "start":
                await load_generator.start()
            elif action == "pause":
                await load_generator.pause()
            elif action == "set_rps":
                value = float(msg.get("value", 10))
                load_generator.set_rps(value)
            elif action == "set_url":
                url = msg.get("value", TARGET_URL)
                load_generator.target_url = url
            elif action == "get_cluster_config":
                message = await build_cluster_config_message()
                await ws.send_text(json.dumps(message, default=str))
            elif action == "apply_cluster_config":
                await handle_apply_cluster_config(msg.get("value") or {})
            elif action == "reset_cluster_config":
                await handle_reset_cluster_config()
    except WebSocketDisconnect:
        pass
    except Exception as e:
        print(f"WebSocket error: {e}")
    finally:
        connected_clients.discard(ws)


# Serve static files (dashboard frontend)
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

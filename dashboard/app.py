import asyncio
import json
import logging
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

INGRESS_BASE = os.environ.get("INGRESS_BASE", "http://localhost:8080")
PYTHON_URL = f"{INGRESS_BASE}/py/work"
RUST_URL = f"{INGRESS_BASE}/rs/work"

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
LOG_FILE = LOG_DIR / "dashboard.log"

logging.basicConfig(
    level=os.environ.get("DASHBOARD_LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_FILE, mode="a"),
    ],
)
logger = logging.getLogger("dashboard")
logger.info("----- dashboard starting — python_url=%s rust_url=%s log_file=%s -----",
            PYTHON_URL, RUST_URL, LOG_FILE)

K8S_MANIFESTS_DIR = Path(__file__).parent.parent / "k8s"

app = FastAPI(title="Load Testing Dashboard")

# Shared state
metrics_store = MetricsStore()
load_generator = LoadGenerator(
    python_url=PYTHON_URL,
    rust_url=RUST_URL,
    metrics_store=metrics_store,
)
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
        logger.exception("get_current failed: %s", e)
        current_dict = None
    try:
        defaults = cluster_config_manager.get_defaults()
        defaults_dict = asdict(defaults)
    except Exception as e:
        logger.exception("get_defaults failed: %s", e)
        defaults_dict = None
    return {
        "type": "cluster_config",
        "current": current_dict,
        "defaults": defaults_dict,
    }


def _stack_section(stack_snapshot) -> dict:
    if stack_snapshot is None:
        return None
    return {
        "replicas": stack_snapshot.replicas,
        "hpa_desired": stack_snapshot.hpa_desired,
        "hpa_current_cpu": stack_snapshot.hpa_current_cpu,
        "pods": [asdict(p) for p in stack_snapshot.pods],
    }


async def metrics_loop() -> None:
    """Periodically compute and broadcast metrics."""
    while True:
        try:
            response_snapshot = metrics_store.compute_snapshot()

            try:
                cluster_snapshot = await k8s_monitor.get_metrics()
                cluster_data = {
                    "python": _stack_section(cluster_snapshot.python),
                    "rust": _stack_section(cluster_snapshot.rust),
                }
            except Exception as e:
                logger.exception("K8s monitor error: %s", e)
                cluster_data = {
                    "python": {"replicas": 0, "hpa_desired": 0, "hpa_current_cpu": None, "pods": []},
                    "rust": None,
                }

            payload = {
                "type": "metrics",
                "timestamp": time.time(),
                "response_times": {
                    "python": asdict(response_snapshot.python),
                    "rust": asdict(response_snapshot.rust),
                },
                "load": {
                    "target_rps": load_generator.target_rps,
                    "dual_stack_enabled": load_generator.dual_stack_enabled,
                    "is_running": load_generator.is_running,
                },
                "cluster": cluster_data,
            }
            await broadcast(payload)
        except Exception as e:
            logger.exception("Metrics loop error: %s", e)

        await asyncio.sleep(1)


@app.on_event("startup")
async def startup() -> None:
    logger.info("startup: syncing dual_stack flag from cluster state")
    try:
        current = await cluster_config_manager.get_current()
        load_generator.set_dual_stack(current.dual_stack_enabled)
        logger.info("startup: dual_stack_enabled=%s", current.dual_stack_enabled)
    except Exception as e:
        logger.exception("startup sync failed: %s", e)
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
            dual_stack_enabled=bool(value.get("dual_stack_enabled", False)),
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
    if result.status == "ok":
        load_generator.set_dual_stack(new_cfg.dual_stack_enabled)
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
    if result.status == "ok":
        load_generator.set_dual_stack(defaults.dual_stack_enabled)
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
    logger.info("ws connected (clients=%d)", len(connected_clients))
    try:
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            action = msg.get("action")
            logger.info("ws action=%s value=%s", action, msg.get("value"))

            if action == "start":
                await load_generator.start()
                logger.info("load_generator started rps=%s dual=%s",
                            load_generator.target_rps, load_generator.dual_stack_enabled)
            elif action == "pause":
                await load_generator.pause()
                logger.info("load_generator paused")
            elif action == "set_rps":
                value = float(msg.get("value", 10))
                load_generator.set_rps(value)
            elif action == "get_cluster_config":
                message = await build_cluster_config_message()
                await ws.send_text(json.dumps(message, default=str))
            elif action == "apply_cluster_config":
                await handle_apply_cluster_config(msg.get("value") or {})
            elif action == "reset_cluster_config":
                await handle_reset_cluster_config()
            else:
                logger.warning("ws unknown action: %s", action)
    except WebSocketDisconnect:
        logger.info("ws disconnected")
    except Exception as e:
        logger.exception("WebSocket error: %s", e)
    finally:
        connected_clients.discard(ws)


# Serve static files (dashboard frontend)
static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

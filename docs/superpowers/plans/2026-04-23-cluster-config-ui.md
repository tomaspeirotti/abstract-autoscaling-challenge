# Cluster Config UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a dashboard UI panel to live-tune HPA (min/max replicas, CPU/memory targets) and Deployment (CPU request/limit), applying changes via direct K8s API patches. Editing is gated on the load generator being paused.

**Architecture:** New `ClusterConfigManager` module in the dashboard reads live cluster state, parses `k8s/*.yaml` for defaults, and applies patches via the Kubernetes Python client. Three new WebSocket actions wire it to a collapsible frontend panel. A server-side guard rejects edits when the load generator is running.

**Tech Stack:** Python 3.12, FastAPI, kubernetes-python client, PyYAML (new dep), vanilla JS/CSS, pytest (new).

**Reference spec:** [docs/superpowers/specs/2026-04-23-cluster-config-ui-design.md](../specs/2026-04-23-cluster-config-ui-design.md)

---

## File Structure

**Created:**
- `dashboard/cluster_config.py` — `ClusterConfig` dataclass, `ApplyResult`, `validate_config`, `ClusterConfigManager`
- `dashboard/tests/__init__.py` — empty, marks tests dir as a package
- `dashboard/tests/test_cluster_config.py` — unit tests for validation + YAML parsing
- `dashboard/tests/fixtures/deployment.yaml` — fixture for `get_defaults` tests
- `dashboard/tests/fixtures/hpa.yaml` — fixture for `get_defaults` tests

**Modified:**
- `dashboard/requirements.txt` — add `pyyaml==6.0.2`, `pytest==8.3.4`
- `dashboard/k8s_monitor.py` — rename `_cpu_request_m` → `cpu_request_millicores` (public)
- `dashboard/app.py` — instantiate `ClusterConfigManager`, add 3 WS handlers, broadcast config on connect
- `dashboard/static/index.html` — collapsible panel markup + JS state/handlers/validation
- `dashboard/static/style.css` — panel, badge, disabled input styles
- `docs/architecture-spec.md` — document new module and WS actions

---

## Task 1: Setup — dependencies and K8sMonitor rename

**Files:**
- Modify: `dashboard/requirements.txt`
- Modify: `dashboard/k8s_monitor.py`

- [ ] **Step 1: Add dependencies**

Edit `dashboard/requirements.txt` to:

```
fastapi==0.115.12
uvicorn[standard]==0.34.2
httpx==0.28.1
kubernetes==32.0.1
websockets==15.0.1
pyyaml==6.0.2
pytest==8.3.4
```

- [ ] **Step 2: Install into the existing venv**

Run: `cd dashboard && source .venv/bin/activate && pip install -r requirements.txt`
Expected: `pyyaml-6.0.2` and `pytest-8.3.4` are installed (other packages already satisfied).

- [ ] **Step 3: Rename internal cpu_request field to public in k8s_monitor.py**

In `dashboard/k8s_monitor.py`, change the `__init__` body and usage:

```python
class K8sMonitor:
    def __init__(
        self,
        namespace: str = "default",
        deployment_name: str = "python-api",
        hpa_name: str = "python-api-hpa",
        cpu_request_millicores: int = 100,
    ):
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._hpa_name = hpa_name
        self.cpu_request_millicores = cpu_request_millicores  # public: mutable by ClusterConfigManager
        self._initialized = False
```

Then update the one remaining usage inside `_get_metrics_sync` (line ~95):

```python
cpu_pct = (cpu_cores * 1000 / self.cpu_request_millicores) * 100
```

- [ ] **Step 4: Verify dashboard still starts**

Run: `cd dashboard && source .venv/bin/activate && python -c "from k8s_monitor import K8sMonitor; m = K8sMonitor(); print(m.cpu_request_millicores)"`
Expected: `100`

- [ ] **Step 5: Commit**

```bash
git add dashboard/requirements.txt dashboard/k8s_monitor.py
git commit -m "chore: add pyyaml/pytest, expose cpu_request_millicores on K8sMonitor"
```

---

## Task 2: ClusterConfig dataclass + validation (TDD)

**Files:**
- Create: `dashboard/cluster_config.py`
- Create: `dashboard/tests/__init__.py`
- Create: `dashboard/tests/test_cluster_config.py`

- [ ] **Step 1: Write failing tests for validation**

Create `dashboard/tests/__init__.py` (empty file).

Create `dashboard/tests/test_cluster_config.py`:

```python
import pytest

from cluster_config import ClusterConfig, ValidationError, validate_config


def make_valid() -> ClusterConfig:
    return ClusterConfig(
        min_replicas=1,
        max_replicas=10,
        target_cpu_utilization=50,
        target_memory_utilization=70,
        cpu_request_millicores=100,
        cpu_limit_millicores=500,
    )


def test_valid_config_passes():
    validate_config(make_valid())  # no raise


def test_min_replicas_below_range_fails():
    cfg = make_valid()
    cfg.min_replicas = 0
    with pytest.raises(ValidationError, match="min_replicas"):
        validate_config(cfg)


def test_max_replicas_above_range_fails():
    cfg = make_valid()
    cfg.max_replicas = 21
    with pytest.raises(ValidationError, match="max_replicas"):
        validate_config(cfg)


def test_min_greater_than_max_fails():
    cfg = make_valid()
    cfg.min_replicas = 5
    cfg.max_replicas = 3
    with pytest.raises(ValidationError, match="min_replicas.*max_replicas"):
        validate_config(cfg)


def test_cpu_target_out_of_range_fails():
    cfg = make_valid()
    cfg.target_cpu_utilization = 9
    with pytest.raises(ValidationError, match="target_cpu_utilization"):
        validate_config(cfg)


def test_memory_target_out_of_range_fails():
    cfg = make_valid()
    cfg.target_memory_utilization = 101
    with pytest.raises(ValidationError, match="target_memory_utilization"):
        validate_config(cfg)


def test_cpu_request_out_of_range_fails():
    cfg = make_valid()
    cfg.cpu_request_millicores = 49
    with pytest.raises(ValidationError, match="cpu_request_millicores"):
        validate_config(cfg)


def test_cpu_limit_out_of_range_fails():
    cfg = make_valid()
    cfg.cpu_limit_millicores = 2001
    with pytest.raises(ValidationError, match="cpu_limit_millicores"):
        validate_config(cfg)


def test_cpu_request_greater_than_limit_fails():
    cfg = make_valid()
    cfg.cpu_request_millicores = 600
    cfg.cpu_limit_millicores = 500
    with pytest.raises(ValidationError, match="cpu_request.*cpu_limit"):
        validate_config(cfg)


def test_min_equals_max_is_valid():
    cfg = make_valid()
    cfg.min_replicas = 5
    cfg.max_replicas = 5
    validate_config(cfg)


def test_request_equals_limit_is_valid():
    cfg = make_valid()
    cfg.cpu_request_millicores = 500
    cfg.cpu_limit_millicores = 500
    validate_config(cfg)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd dashboard && source .venv/bin/activate && pytest tests/test_cluster_config.py -v`
Expected: collection error — `cluster_config` module does not exist.

- [ ] **Step 3: Implement ClusterConfig and validation**

Create `dashboard/cluster_config.py`:

```python
from dataclasses import dataclass


class ValidationError(ValueError):
    """Raised when a ClusterConfig fails validation."""


@dataclass
class ClusterConfig:
    min_replicas: int
    max_replicas: int
    target_cpu_utilization: int
    target_memory_utilization: int
    cpu_request_millicores: int
    cpu_limit_millicores: int


_RANGES: dict[str, tuple[int, int]] = {
    "min_replicas": (1, 10),
    "max_replicas": (1, 20),
    "target_cpu_utilization": (10, 100),
    "target_memory_utilization": (10, 100),
    "cpu_request_millicores": (50, 1000),
    "cpu_limit_millicores": (50, 2000),
}


def validate_config(cfg: ClusterConfig) -> None:
    """Raise ValidationError if cfg is invalid. Range + cross-field rules."""
    for field, (lo, hi) in _RANGES.items():
        value = getattr(cfg, field)
        if not isinstance(value, int) or value < lo or value > hi:
            raise ValidationError(
                f"{field}={value} is out of range [{lo}..{hi}]"
            )
    if cfg.min_replicas > cfg.max_replicas:
        raise ValidationError(
            f"min_replicas ({cfg.min_replicas}) must be <= max_replicas ({cfg.max_replicas})"
        )
    if cfg.cpu_request_millicores > cfg.cpu_limit_millicores:
        raise ValidationError(
            f"cpu_request_millicores ({cfg.cpu_request_millicores}) must be "
            f"<= cpu_limit_millicores ({cfg.cpu_limit_millicores})"
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd dashboard && source .venv/bin/activate && pytest tests/test_cluster_config.py -v`
Expected: 11 passed.

- [ ] **Step 5: Commit**

```bash
git add dashboard/cluster_config.py dashboard/tests/__init__.py dashboard/tests/test_cluster_config.py
git commit -m "feat(dashboard): add ClusterConfig dataclass and validate_config"
```

---

## Task 3: ClusterConfigManager.get_defaults (YAML parsing, TDD)

**Files:**
- Modify: `dashboard/cluster_config.py`
- Create: `dashboard/tests/fixtures/deployment.yaml`
- Create: `dashboard/tests/fixtures/hpa.yaml`
- Modify: `dashboard/tests/test_cluster_config.py`

- [ ] **Step 1: Create fixture YAMLs**

Create `dashboard/tests/fixtures/deployment.yaml`:

```yaml
apiVersion: apps/v1
kind: Deployment
metadata:
  name: python-api
spec:
  template:
    spec:
      containers:
      - name: python-api
        resources:
          requests:
            cpu: 100m
            memory: 128Mi
          limits:
            cpu: 500m
            memory: 256Mi
```

Create `dashboard/tests/fixtures/hpa.yaml`:

```yaml
apiVersion: autoscaling/v2
kind: HorizontalPodAutoscaler
metadata:
  name: python-api-hpa
spec:
  minReplicas: 2
  maxReplicas: 8
  metrics:
  - type: Resource
    resource:
      name: cpu
      target:
        type: Utilization
        averageUtilization: 60
  - type: Resource
    resource:
      name: memory
      target:
        type: Utilization
        averageUtilization: 75
```

- [ ] **Step 2: Write failing test for get_defaults**

Append to `dashboard/tests/test_cluster_config.py`:

```python
from pathlib import Path

from cluster_config import ClusterConfigManager


FIXTURES = Path(__file__).parent / "fixtures"


def test_get_defaults_parses_yaml():
    mgr = ClusterConfigManager(
        namespace="default",
        deployment_name="python-api",
        hpa_name="python-api-hpa",
        k8s_manifests_dir=FIXTURES,
        k8s_monitor=None,  # not used by get_defaults
    )
    cfg = mgr.get_defaults()
    assert cfg.min_replicas == 2
    assert cfg.max_replicas == 8
    assert cfg.target_cpu_utilization == 60
    assert cfg.target_memory_utilization == 75
    assert cfg.cpu_request_millicores == 100
    assert cfg.cpu_limit_millicores == 500


def test_parse_cpu_millicores_variants():
    from cluster_config import _parse_cpu_millicores
    assert _parse_cpu_millicores("500m") == 500
    assert _parse_cpu_millicores("1") == 1000      # bare integer → cores
    assert _parse_cpu_millicores(1) == 1000        # YAML may parse as int
    assert _parse_cpu_millicores(0.5) == 500       # or float
```

- [ ] **Step 3: Run test to verify it fails**

Run: `cd dashboard && source .venv/bin/activate && pytest tests/test_cluster_config.py::test_get_defaults_parses_yaml -v`
Expected: ImportError — `ClusterConfigManager` is not defined.

- [ ] **Step 4: Implement get_defaults**

Append to `dashboard/cluster_config.py`:

```python
from pathlib import Path

import yaml


def _parse_cpu_millicores(value) -> int:
    """Parse K8s CPU value to millicores. '500m' -> 500, '1' -> 1000, 0.5 -> 500."""
    if isinstance(value, (int, float)):
        return int(value * 1000)
    s = str(value).strip()
    if s.endswith("m"):
        return int(s[:-1])
    return int(float(s) * 1000)


class ClusterConfigManager:
    """Reads, validates, and applies HPA + Deployment config changes."""

    def __init__(
        self,
        namespace: str,
        deployment_name: str,
        hpa_name: str,
        k8s_manifests_dir: Path,
        k8s_monitor,  # K8sMonitor; untyped to avoid circular import
    ):
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._hpa_name = hpa_name
        self._manifests_dir = Path(k8s_manifests_dir)
        self._k8s_monitor = k8s_monitor

    def get_defaults(self) -> ClusterConfig:
        """Parse k8s/hpa.yaml and k8s/deployment.yaml for the baseline config."""
        hpa_doc = yaml.safe_load((self._manifests_dir / "hpa.yaml").read_text())
        dep_doc = yaml.safe_load((self._manifests_dir / "deployment.yaml").read_text())

        min_replicas = int(hpa_doc["spec"]["minReplicas"])
        max_replicas = int(hpa_doc["spec"]["maxReplicas"])

        cpu_util = 50
        mem_util = 70
        for metric in hpa_doc["spec"].get("metrics", []):
            if metric.get("type") != "Resource":
                continue
            res = metric["resource"]
            value = int(res["target"]["averageUtilization"])
            if res["name"] == "cpu":
                cpu_util = value
            elif res["name"] == "memory":
                mem_util = value

        container = dep_doc["spec"]["template"]["spec"]["containers"][0]
        resources = container["resources"]
        cpu_request = _parse_cpu_millicores(resources["requests"]["cpu"])
        cpu_limit = _parse_cpu_millicores(resources["limits"]["cpu"])

        return ClusterConfig(
            min_replicas=min_replicas,
            max_replicas=max_replicas,
            target_cpu_utilization=cpu_util,
            target_memory_utilization=mem_util,
            cpu_request_millicores=cpu_request,
            cpu_limit_millicores=cpu_limit,
        )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd dashboard && source .venv/bin/activate && pytest tests/test_cluster_config.py -v`
Expected: 13 passed.

- [ ] **Step 6: Commit**

```bash
git add dashboard/cluster_config.py dashboard/tests/test_cluster_config.py dashboard/tests/fixtures/
git commit -m "feat(dashboard): ClusterConfigManager.get_defaults reads YAML defaults"
```

---

## Task 4: ClusterConfigManager.get_current and apply

**Files:**
- Modify: `dashboard/cluster_config.py`

No unit tests here — both methods talk to a live K8s API. Manual smoke test runs in Task 9.

- [ ] **Step 1: Add new imports at the top of cluster_config.py**

At the top of `dashboard/cluster_config.py`, replace the existing imports block with:

```python
import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException
```

- [ ] **Step 2: Add ApplyResult dataclass at module level**

After the `ClusterConfig` dataclass and before `_RANGES`, insert:

```python
@dataclass
class ApplyResult:
    status: Literal["ok", "validation_error", "k8s_error", "rollout_timeout"]
    error: str | None = None
    restart_triggered: bool = False
```

- [ ] **Step 3: Add the new methods to ClusterConfigManager**

Append inside the `ClusterConfigManager` class (after `get_defaults`):

```python
    def _ensure_init(self) -> None:
        if getattr(self, "_initialized", False):
            return
        try:
            config.load_kube_config()
        except config.ConfigException:
            config.load_incluster_config()
        self._initialized = True

    async def get_current(self) -> ClusterConfig:
        """Read live HPA + Deployment state from the cluster."""
        self._ensure_init()
        return await asyncio.to_thread(self._get_current_sync)

    def _get_current_sync(self) -> ClusterConfig:
        apps_api = client.AppsV1Api()
        autoscaling_api = client.AutoscalingV2Api()

        hpa = autoscaling_api.read_namespaced_horizontal_pod_autoscaler(
            name=self._hpa_name, namespace=self._namespace
        )
        dep = apps_api.read_namespaced_deployment(
            name=self._deployment_name, namespace=self._namespace
        )

        cpu_util = 50
        mem_util = 70
        for metric in hpa.spec.metrics or []:
            if metric.type != "Resource" or metric.resource is None:
                continue
            avg = metric.resource.target.average_utilization
            if avg is None:
                continue
            if metric.resource.name == "cpu":
                cpu_util = int(avg)
            elif metric.resource.name == "memory":
                mem_util = int(avg)

        container = dep.spec.template.spec.containers[0]
        resources = container.resources
        cpu_request = _parse_cpu_millicores(resources.requests.get("cpu", "100m"))
        cpu_limit = _parse_cpu_millicores(resources.limits.get("cpu", "500m"))

        return ClusterConfig(
            min_replicas=int(hpa.spec.min_replicas),
            max_replicas=int(hpa.spec.max_replicas),
            target_cpu_utilization=cpu_util,
            target_memory_utilization=mem_util,
            cpu_request_millicores=cpu_request,
            cpu_limit_millicores=cpu_limit,
        )

    async def apply(self, new_cfg: ClusterConfig) -> ApplyResult:
        """Validate, patch HPA + Deployment, update monitor, await rollout if needed."""
        try:
            validate_config(new_cfg)
        except ValidationError as e:
            return ApplyResult(status="validation_error", error=str(e))

        self._ensure_init()
        try:
            current = await asyncio.to_thread(self._get_current_sync)
        except ApiException as e:
            return ApplyResult(status="k8s_error", error=f"read current: {e.reason}")

        resources_changed = (
            new_cfg.cpu_request_millicores != current.cpu_request_millicores
            or new_cfg.cpu_limit_millicores != current.cpu_limit_millicores
        )

        try:
            await asyncio.to_thread(self._patch_hpa_sync, new_cfg)
            await asyncio.to_thread(self._patch_deployment_sync, new_cfg)
        except ApiException as e:
            return ApplyResult(status="k8s_error", error=f"patch failed: {e.reason}")

        # Update monitor's denominator for per-pod CPU % math
        if self._k8s_monitor is not None:
            self._k8s_monitor.cpu_request_millicores = new_cfg.cpu_request_millicores

        if resources_changed:
            ok = await asyncio.to_thread(self._wait_rollout_sync, timeout_s=60)
            if not ok:
                return ApplyResult(
                    status="rollout_timeout",
                    error="deployment rollout did not complete within 60s",
                    restart_triggered=True,
                )

        return ApplyResult(status="ok", restart_triggered=resources_changed)

    def _patch_hpa_sync(self, cfg: ClusterConfig) -> None:
        api = client.AutoscalingV2Api()
        body = {
            "spec": {
                "minReplicas": cfg.min_replicas,
                "maxReplicas": cfg.max_replicas,
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "cpu",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": cfg.target_cpu_utilization,
                            },
                        },
                    },
                    {
                        "type": "Resource",
                        "resource": {
                            "name": "memory",
                            "target": {
                                "type": "Utilization",
                                "averageUtilization": cfg.target_memory_utilization,
                            },
                        },
                    },
                ],
            }
        }
        api.patch_namespaced_horizontal_pod_autoscaler(
            name=self._hpa_name, namespace=self._namespace, body=body
        )

    def _patch_deployment_sync(self, cfg: ClusterConfig) -> None:
        api = client.AppsV1Api()
        body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": self._deployment_name,
                                "resources": {
                                    "requests": {
                                        "cpu": f"{cfg.cpu_request_millicores}m",
                                    },
                                    "limits": {
                                        "cpu": f"{cfg.cpu_limit_millicores}m",
                                    },
                                },
                            }
                        ]
                    }
                }
            }
        }
        api.patch_namespaced_deployment(
            name=self._deployment_name, namespace=self._namespace, body=body
        )

    def _wait_rollout_sync(self, timeout_s: int) -> bool:
        """Poll Deployment status until updatedReplicas == replicas and generation matches."""
        api = client.AppsV1Api()
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            dep = api.read_namespaced_deployment(
                name=self._deployment_name, namespace=self._namespace
            )
            spec_replicas = dep.spec.replicas or 0
            status = dep.status
            updated = status.updated_replicas or 0
            ready = status.ready_replicas or 0
            observed_gen = status.observed_generation or 0
            meta_gen = dep.metadata.generation or 0
            if (
                observed_gen >= meta_gen
                and updated >= spec_replicas
                and ready >= spec_replicas
            ):
                return True
            time.sleep(1)
        return False
```

- [ ] **Step 4: Run existing tests to ensure nothing broke**

Run: `cd dashboard && source .venv/bin/activate && pytest tests/ -v`
Expected: 13 passed (same as Task 3 end — new methods have no unit tests).

- [ ] **Step 5: Quick import smoke**

Run: `cd dashboard && source .venv/bin/activate && python -c "from cluster_config import ClusterConfigManager, ApplyResult; print('ok')"`
Expected: `ok`

- [ ] **Step 6: Commit**

```bash
git add dashboard/cluster_config.py
git commit -m "feat(dashboard): ClusterConfigManager.get_current + apply with rollout wait"
```

---

## Task 5: Wire backend into app.py (WS handlers + broadcast)

**Files:**
- Modify: `dashboard/app.py`

- [ ] **Step 1: Update imports and instantiate the manager**

Replace the imports and shared-state section of `dashboard/app.py`:

```python
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
```

- [ ] **Step 2: Add a helper to build the cluster_config message**

Add after the `broadcast` function in `dashboard/app.py`:

```python
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
```

- [ ] **Step 3: Add action handlers to the websocket_endpoint**

Replace the websocket_endpoint `if action ==` chain:

```python
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
```

- [ ] **Step 4: Implement the two apply/reset helpers**

Add above `websocket_endpoint`:

```python
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
```

- [ ] **Step 5: Smoke the dashboard boot**

Run: `cd dashboard && source .venv/bin/activate && python -c "import app; print(app.cluster_config_manager)"`
Expected: prints the `ClusterConfigManager` repr without error.

- [ ] **Step 6: Commit**

```bash
git add dashboard/app.py
git commit -m "feat(dashboard): wire ClusterConfigManager to WebSocket actions"
```

---

## Task 6: Frontend HTML — collapsible panel markup

**Files:**
- Modify: `dashboard/static/index.html`

- [ ] **Step 1: Insert panel markup**

In `dashboard/static/index.html`, locate the `</div>` that closes `arch-controls` (after the `url-input` block, around line 39). Immediately after it, insert:

```html
<!-- ==================== CLUSTER CONFIG PANEL ==================== -->
<div class="cluster-config" id="clusterConfig">
    <div class="cc-header" onclick="toggleConfigPanel()">
        <span class="cc-toggle" id="ccToggle">▸</span>
        <span class="cc-title">Cluster config</span>
        <span class="cc-spacer"></span>
        <span class="cc-badge" id="ccBadge" data-state="synced">
            <span class="cc-badge-dot"></span>
            <span class="cc-badge-label" id="ccBadgeLabel">Synced</span>
        </span>
    </div>
    <div class="cc-body" id="ccBody" hidden>
        <div class="cc-warning" id="ccWarning" hidden>
            ⚠ Pause the load generator to edit these values.
        </div>
        <div class="cc-group">
            <div class="cc-group-title">HPA</div>
            <div class="cc-grid">
                <label class="cc-field">
                    <span>min replicas</span>
                    <input type="range" id="ccMinReplicas" min="1" max="10" step="1"
                           oninput="onConfigInput('min_replicas', this.value)">
                    <span class="cc-value" id="ccMinReplicasValue">—</span>
                </label>
                <label class="cc-field">
                    <span>max replicas</span>
                    <input type="range" id="ccMaxReplicas" min="1" max="20" step="1"
                           oninput="onConfigInput('max_replicas', this.value)">
                    <span class="cc-value" id="ccMaxReplicasValue">—</span>
                </label>
                <label class="cc-field">
                    <span>target CPU %</span>
                    <input type="range" id="ccTargetCpu" min="10" max="100" step="1"
                           oninput="onConfigInput('target_cpu_utilization', this.value)">
                    <span class="cc-value" id="ccTargetCpuValue">—</span>
                </label>
                <label class="cc-field">
                    <span>target memory %</span>
                    <input type="range" id="ccTargetMem" min="10" max="100" step="1"
                           oninput="onConfigInput('target_memory_utilization', this.value)">
                    <span class="cc-value" id="ccTargetMemValue">—</span>
                </label>
            </div>
        </div>
        <div class="cc-group">
            <div class="cc-group-title">Deployment</div>
            <div class="cc-grid">
                <label class="cc-field">
                    <span>cpu request (m)</span>
                    <input type="range" id="ccCpuRequest" min="50" max="1000" step="10"
                           oninput="onConfigInput('cpu_request_millicores', this.value)">
                    <span class="cc-value" id="ccCpuRequestValue">—</span>
                </label>
                <label class="cc-field">
                    <span>cpu limit (m)</span>
                    <input type="range" id="ccCpuLimit" min="50" max="2000" step="10"
                           oninput="onConfigInput('cpu_limit_millicores', this.value)">
                    <span class="cc-value" id="ccCpuLimitValue">—</span>
                </label>
            </div>
        </div>
        <div class="cc-errors" id="ccErrors"></div>
        <div class="cc-actions">
            <button class="cc-btn cc-btn-secondary" id="ccResetBtn" onclick="onConfigReset()">Reset to defaults</button>
            <button class="cc-btn cc-btn-primary" id="ccApplyBtn" onclick="onConfigApply()" disabled>Apply</button>
        </div>
    </div>
</div>
```

- [ ] **Step 2: Verify the dashboard still loads**

Run: `cd dashboard && source .venv/bin/activate && uvicorn app:app --port 3000 &` (kill with `kill %1` after), then `curl -s http://localhost:3000/ | grep 'Cluster config'`
Expected: one line containing `Cluster config`.

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/index.html
git commit -m "feat(dashboard): add cluster config panel markup"
```

---

## Task 7: Frontend CSS — panel + badge + disabled state

**Files:**
- Modify: `dashboard/static/style.css`

- [ ] **Step 1: Append new styles**

Append to the end of `dashboard/static/style.css`:

```css
/* ==================== CLUSTER CONFIG PANEL ==================== */
.cluster-config {
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
}

.cc-header {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px 16px;
    cursor: pointer;
    user-select: none;
    font-size: 12px;
    color: var(--text-dim);
}

.cc-header:hover { background: var(--surface-hover); }

.cc-toggle {
    display: inline-block;
    width: 10px;
    font-family: var(--font-mono);
    color: var(--text-dim);
    transition: transform 0.2s;
}

.cc-toggle.open { transform: rotate(90deg); }

.cc-title {
    font-weight: 600;
    color: var(--text);
    letter-spacing: 0.01em;
}

.cc-spacer { flex: 1; }

.cc-badge {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    font-family: var(--font-mono);
    font-size: 10px;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 10px;
    background: var(--bg);
    border: 1px solid var(--border);
}

.cc-badge-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
}

.cc-badge[data-state="synced"]   .cc-badge-dot { background: var(--accent); }
.cc-badge[data-state="modified"] .cc-badge-dot { background: var(--orange); }
.cc-badge[data-state="applying"] .cc-badge-dot { background: var(--blue); animation: ccPulse 1s infinite; }
.cc-badge[data-state="error"]    .cc-badge-dot { background: var(--red); }

@keyframes ccPulse {
    0%, 100% { opacity: 1; }
    50%      { opacity: 0.3; }
}

.cc-body {
    padding: 14px 16px 16px;
    border-top: 1px solid var(--border);
    background: var(--bg);
}

.cc-warning {
    background: rgba(245, 166, 35, 0.08);
    border: 1px solid rgba(245, 166, 35, 0.3);
    color: var(--orange);
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 11px;
    margin-bottom: 12px;
}

.cc-group { margin-bottom: 14px; }
.cc-group:last-of-type { margin-bottom: 0; }

.cc-group-title {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--text-dim);
    margin-bottom: 8px;
}

.cc-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 16px;
}

.cc-field {
    display: grid;
    grid-template-columns: 110px 1fr 44px;
    align-items: center;
    gap: 10px;
    font-size: 11px;
    color: var(--text-dim);
}

.cc-field input[type="range"] {
    width: 100%;
    accent-color: var(--accent);
}

.cc-field input[type="range"]:disabled { opacity: 0.4; cursor: not-allowed; }

.cc-value {
    font-family: var(--font-mono);
    font-size: 11px;
    color: var(--text);
    text-align: right;
}

.cc-errors {
    margin-top: 10px;
    min-height: 14px;
    font-size: 11px;
    color: var(--red);
    font-family: var(--font-mono);
}

.cc-errors div + div { margin-top: 2px; }

.cc-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 12px;
}

.cc-btn {
    padding: 6px 16px;
    border-radius: 6px;
    border: 1px solid var(--border);
    background: var(--surface);
    color: var(--text);
    font-size: 11px;
    font-weight: 600;
    cursor: pointer;
    transition: background 0.15s, border-color 0.15s;
    font-family: var(--font-sans);
}

.cc-btn:hover:not(:disabled) { background: var(--surface-hover); }

.cc-btn:disabled {
    opacity: 0.4;
    cursor: not-allowed;
}

.cc-btn-primary {
    background: var(--accent);
    border-color: var(--accent);
    color: var(--bg);
}

.cc-btn-primary:hover:not(:disabled) {
    background: var(--accent-dim);
    border-color: var(--accent-dim);
}
```

- [ ] **Step 2: Visual smoke**

Run: `cd dashboard && source .venv/bin/activate && uvicorn app:app --port 3000 &`. Open `http://localhost:3000/` in a browser. The `▸ Cluster config ● Synced` row should be visible below the Start/RPS/Target controls. Clicking it shouldn't do anything yet (no JS). Kill with `kill %1`.

- [ ] **Step 3: Commit**

```bash
git add dashboard/static/style.css
git commit -m "feat(dashboard): styles for cluster config panel"
```

---

## Task 8: Frontend JS — state, handlers, validation, gating

**Files:**
- Modify: `dashboard/static/index.html`

- [ ] **Step 1: Add cluster-config state block**

In `dashboard/static/index.html`, locate the `// STATE` block (around line 385). Right after the `let isRunning = false;` line, add:

```javascript
// ---- Cluster config state ----
let clusterConfig = {
    current: null,
    defaults: null,
    draft: null,
    state: 'synced',   // 'synced' | 'modified' | 'applying' | 'error'
    error: null,
};
const CC_RANGES = {
    min_replicas:               [1, 10],
    max_replicas:               [1, 20],
    target_cpu_utilization:     [10, 100],
    target_memory_utilization:  [10, 100],
    cpu_request_millicores:     [50, 1000],
    cpu_limit_millicores:       [50, 2000],
};
const CC_INPUT_IDS = {
    min_replicas:              'ccMinReplicas',
    max_replicas:              'ccMaxReplicas',
    target_cpu_utilization:    'ccTargetCpu',
    target_memory_utilization: 'ccTargetMem',
    cpu_request_millicores:    'ccCpuRequest',
    cpu_limit_millicores:      'ccCpuLimit',
};
const CC_VALUE_IDS = {
    min_replicas:              'ccMinReplicasValue',
    max_replicas:              'ccMaxReplicasValue',
    target_cpu_utilization:    'ccTargetCpuValue',
    target_memory_utilization: 'ccTargetMemValue',
    cpu_request_millicores:    'ccCpuRequestValue',
    cpu_limit_millicores:      'ccCpuLimitValue',
};
```

- [ ] **Step 2: Extend the WebSocket message dispatcher**

Still in `dashboard/static/index.html`, find `ws.onmessage` inside `connect()`. Replace the `if (data.type === 'metrics')` block so it also handles the new message types:

```javascript
    ws.onmessage = (event) => {
        const data = JSON.parse(event.data);
        if (data.type === 'metrics') {
            updateDashboard(data);
            updateArchitecture(data);
        } else if (data.type === 'cluster_config') {
            onClusterConfigReceived(data);
        } else if (data.type === 'cluster_config_result') {
            onClusterConfigResult(data);
        }
    };
```

Then find `ws.onopen` inside `connect()`. Add the `get_cluster_config` request right after the "Connected" status text:

```javascript
    ws.onopen = () => {
        document.getElementById('statusDot').classList.add('connected');
        document.getElementById('statusText').textContent = 'Connected';
        send({ action: 'get_cluster_config' });
    };
```

- [ ] **Step 3: Add cluster-config handler functions**

Insert this block right before the `// ARCHITECTURE VIEW UPDATE` comment section (around line 533):

```javascript
// ============================================================
//  CLUSTER CONFIG
// ============================================================
function toggleConfigPanel() {
    const body = document.getElementById('ccBody');
    const toggle = document.getElementById('ccToggle');
    const wasHidden = body.hasAttribute('hidden');
    if (wasHidden) {
        body.removeAttribute('hidden');
        toggle.classList.add('open');
        toggle.textContent = '▾';
    } else {
        body.setAttribute('hidden', '');
        toggle.classList.remove('open');
        toggle.textContent = '▸';
    }
    localStorage.setItem('clusterConfigExpanded', wasHidden ? '1' : '0');
}

function onClusterConfigReceived(data) {
    if (!data.current) return;
    clusterConfig.current = { ...data.current };
    clusterConfig.defaults = data.defaults ? { ...data.defaults } : null;
    // Hydrate draft from current on first load or after successful apply
    if (!clusterConfig.draft || clusterConfig.state !== 'modified') {
        clusterConfig.draft = { ...data.current };
    }
    clusterConfig.error = null;
    clusterConfig.state = computeState();
    renderConfigPanel();
}

function onClusterConfigResult(data) {
    if (data.status === 'ok') {
        clusterConfig.state = 'applying';  // will be flipped to 'synced' when cluster_config arrives
        clusterConfig.error = null;
    } else {
        clusterConfig.state = 'error';
        clusterConfig.error = data.error || data.status;
    }
    renderConfigPanel();
}

function onConfigInput(field, rawValue) {
    if (!clusterConfig.draft) return;
    clusterConfig.draft[field] = parseInt(rawValue, 10);
    clusterConfig.state = computeState();
    clusterConfig.error = null;
    renderConfigPanel();
}

function onConfigApply() {
    if (clusterConfig.state !== 'modified') return;
    if (validateDraft(clusterConfig.draft).length > 0) return;
    clusterConfig.state = 'applying';
    renderConfigPanel();
    send({ action: 'apply_cluster_config', value: clusterConfig.draft });
}

function onConfigReset() {
    clusterConfig.state = 'applying';
    renderConfigPanel();
    send({ action: 'reset_cluster_config' });
}

function computeState() {
    if (!clusterConfig.current || !clusterConfig.draft) return 'synced';
    const equal = Object.keys(CC_RANGES).every(
        k => clusterConfig.draft[k] === clusterConfig.current[k]
    );
    return equal ? 'synced' : 'modified';
}

function validateDraft(draft) {
    const errors = [];
    if (!draft) return errors;
    for (const [field, [lo, hi]] of Object.entries(CC_RANGES)) {
        const v = draft[field];
        if (!Number.isInteger(v) || v < lo || v > hi) {
            errors.push(`${field} must be between ${lo} and ${hi}`);
        }
    }
    if (draft.min_replicas > draft.max_replicas) {
        errors.push(`min_replicas (${draft.min_replicas}) must be ≤ max_replicas (${draft.max_replicas})`);
    }
    if (draft.cpu_request_millicores > draft.cpu_limit_millicores) {
        errors.push(`cpu_request (${draft.cpu_request_millicores}m) must be ≤ cpu_limit (${draft.cpu_limit_millicores}m)`);
    }
    return errors;
}

function renderConfigPanel() {
    const draft = clusterConfig.draft;
    if (!draft) return;

    // Sliders + value labels
    for (const field of Object.keys(CC_RANGES)) {
        const input = document.getElementById(CC_INPUT_IDS[field]);
        const label = document.getElementById(CC_VALUE_IDS[field]);
        if (input) input.value = draft[field];
        if (label) {
            if (field === 'target_cpu_utilization' || field === 'target_memory_utilization') {
                label.textContent = draft[field] + '%';
            } else if (field.endsWith('_millicores')) {
                label.textContent = draft[field] + 'm';
            } else {
                label.textContent = draft[field];
            }
        }
    }

    // Badge
    const badge = document.getElementById('ccBadge');
    const badgeLabel = document.getElementById('ccBadgeLabel');
    badge.dataset.state = clusterConfig.state;
    const labels = { synced: 'Synced', modified: 'Modified', applying: 'Applying…', error: 'Error' };
    badgeLabel.textContent = labels[clusterConfig.state] || clusterConfig.state;
    badge.title = clusterConfig.error || '';

    // Errors block
    const errorsDiv = document.getElementById('ccErrors');
    const errors = validateDraft(draft);
    if (clusterConfig.error) {
        errorsDiv.innerHTML = `<div>${clusterConfig.error}</div>` + errors.map(e => `<div>${e}</div>`).join('');
    } else {
        errorsDiv.innerHTML = errors.map(e => `<div>${e}</div>`).join('');
    }

    // Apply button enablement
    const applyBtn = document.getElementById('ccApplyBtn');
    applyBtn.disabled = clusterConfig.state !== 'modified' || errors.length > 0 || isRunning;

    // Reset enablement — disabled while applying or while load is running
    const resetBtn = document.getElementById('ccResetBtn');
    resetBtn.disabled = clusterConfig.state === 'applying' || isRunning;

    // Gating (load running)
    document.getElementById('ccWarning').hidden = !isRunning;
    for (const field of Object.keys(CC_INPUT_IDS)) {
        const el = document.getElementById(CC_INPUT_IDS[field]);
        if (el) el.disabled = isRunning || clusterConfig.state === 'applying';
    }
}
```

- [ ] **Step 4: Hook gating into updateDashboard**

Find the existing `updateDashboard` function. Append one line at the end of its body, right after the `cpuChart.update('none');` line:

```javascript
    // Re-render config panel so gating (disabled) tracks is_running.
    renderConfigPanel();
```

- [ ] **Step 5: Restore expanded state on load**

At the very end of the `<script>` block (right after `connect();` at line ~681), add:

```javascript
// Restore panel expanded state
if (localStorage.getItem('clusterConfigExpanded') === '1') {
    toggleConfigPanel();
}
```

- [ ] **Step 6: Smoke the end-to-end flow (without a cluster)**

Run: `cd dashboard && source .venv/bin/activate && uvicorn app:app --port 3000 &`. Open `http://localhost:3000/`. Expand the panel — sliders should show `—` (no cluster means `get_cluster_config` returned `current: null`). No JS errors in the browser console. Kill with `kill %1`.

Expected: no exceptions in the browser console. Panel renders, but sliders have no values because there's no cluster.

- [ ] **Step 7: Commit**

```bash
git add dashboard/static/index.html
git commit -m "feat(dashboard): cluster config frontend state, handlers, gating"
```

---

## Task 9: Manual smoke test with a live cluster + docs update

**Files:**
- Modify: `docs/architecture-spec.md`

- [ ] **Step 1: Bring up the cluster and dashboard**

Run:
```bash
./start.sh
```
Wait until the dashboard is serving on `http://localhost:3000/` and `kubectl get hpa` shows `python-api-hpa`.

- [ ] **Step 2: Happy-path smoke**

In the browser:
1. Expand "Cluster config". Sliders should hydrate with `min=1, max=10, cpu=50%, mem=70%, request=100m, limit=500m`. Badge: `Synced`.
2. Move `max replicas` slider to `15`. Badge switches to `Modified`; Apply enables.
3. Click Apply. Badge goes to `Applying…` then `Synced`.
4. Verify in a terminal: `kubectl get hpa python-api-hpa -o jsonpath='{.spec.maxReplicas}'` → `15`.

- [ ] **Step 3: Rolling-restart smoke**

1. Lower `cpu request (m)` slider to `200`. Apply.
2. Badge stays in `Applying…` while the deployment does a rolling restart (watch `kubectl get pods -l app=python-api -w`).
3. After the rollout completes, badge returns to `Synced`.
4. Verify: `kubectl get deployment python-api -o jsonpath='{.spec.template.spec.containers[0].resources.requests.cpu}'` → `200m`.
5. In the UI, the per-pod CPU % values reflect the new request (halved relative to previous, since request doubled).

- [ ] **Step 4: Gating smoke**

1. Click Start in the top controls. `is_running` becomes true.
2. Warning banner appears in the config panel; all sliders + Apply + Reset are disabled.
3. Click Pause; controls re-enable.

- [ ] **Step 5: Validation smoke**

1. Set `min replicas` to `8`, `max replicas` to `5`.
2. Red error text appears: `min_replicas (8) must be ≤ max_replicas (5)`.
3. Apply button stays disabled.

- [ ] **Step 6: Reset smoke**

1. Click "Reset to defaults". Badge: `Applying…` → `Synced`.
2. All sliders return to the YAML defaults (`max=10`, `cpu_request=100m`, etc.).
3. Verify: `kubectl get hpa python-api-hpa -o jsonpath='{.spec.maxReplicas}'` → `10`.

- [ ] **Step 7: Error smoke (optional)**

1. `kubectl delete hpa python-api-hpa`.
2. Change any slider and Apply. Badge goes to `Error`, tooltip on badge shows the K8s error ("read current: Not Found").
3. Re-apply HPA: `kubectl apply -f k8s/hpa.yaml`.

- [ ] **Step 8: Teardown**

Run: `./scripts/teardown.sh`

- [ ] **Step 9: Update architecture spec**

Edit `docs/architecture-spec.md`. Under section `### 3.2 Dashboard Backend (dashboard/)` after subsection `#### 3.2.4 Module: metrics.py`, insert:

```markdown
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
```

Then in section `## 4 WebSocket Protocol`, under `### 4.1 Client → Server Messages`, append rows:

```markdown
| `get_cluster_config` | `{"action": "get_cluster_config"}` | Returns `{type: "cluster_config", current, defaults}` |
| `apply_cluster_config` | `{"action": "apply_cluster_config", "value": {<ClusterConfig>}}` | Validates, patches, returns `cluster_config_result` + refreshed `cluster_config` |
| `reset_cluster_config` | `{"action": "reset_cluster_config"}` | Equivalent to `apply` with YAML defaults |
```

Under `### 4.2 Server → Client Messages`, add after the existing metrics JSON:

```markdown
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
```

- [ ] **Step 10: Commit docs**

```bash
git add docs/architecture-spec.md
git commit -m "docs: document cluster config module and WS protocol additions"
```

- [ ] **Step 11: Push all work**

```bash
git push origin main
```

Expected: all commits on `main` land on `origin/main`.

---

## Summary

- 9 tasks, each commit-sized.
- TDD on the two most valuable units: validation and YAML parsing.
- Integration (K8s API calls, rolling restart wait) validated via manual smoke with a live k3d cluster.
- Server-side guard (not just UI) enforces the "paused to edit" rule.
- Docs updated in the same branch.

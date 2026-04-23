import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


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


@dataclass
class ApplyResult:
    status: Literal["ok", "validation_error", "k8s_error", "rollout_timeout"]
    error: str | None = None
    restart_triggered: bool = False


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
        cpu_request = _parse_cpu_millicores((resources.requests or {}).get("cpu", "100m"))
        cpu_limit = _parse_cpu_millicores((resources.limits or {}).get("cpu", "500m"))

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

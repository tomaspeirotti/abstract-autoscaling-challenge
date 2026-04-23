from dataclasses import dataclass
from pathlib import Path

import yaml


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

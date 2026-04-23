import asyncio
import re
from dataclasses import dataclass

from kubernetes import client, config
from kubernetes.client.exceptions import ApiException


@dataclass
class PodMetrics:
    name: str
    cpu_percent: float  # relative to request (100m = 100%)
    memory_mb: float
    status: str


@dataclass
class ClusterSnapshot:
    replicas: int
    hpa_desired: int
    hpa_current_cpu: int | None  # current avg CPU utilization %
    pods: list[PodMetrics]


def parse_cpu(value: str) -> float:
    """Parse K8s CPU value to cores. '25m' -> 0.025, '1' -> 1.0"""
    if value.endswith("n"):
        return int(value[:-1]) / 1_000_000_000
    if value.endswith("u"):
        return int(value[:-1]) / 1_000_000
    if value.endswith("m"):
        return int(value[:-1]) / 1000
    return float(value)


def parse_memory(value: str) -> float:
    """Parse K8s memory value to MB."""
    units = {"Ki": 1024, "Mi": 1024**2, "Gi": 1024**3, "K": 1000, "M": 1000**2, "G": 1000**3}
    for suffix, multiplier in units.items():
        if value.endswith(suffix):
            return int(value[: -len(suffix)]) * multiplier / (1024**2)  # convert to MB
    return int(value) / (1024**2)  # raw bytes to MB


class K8sMonitor:
    """Monitors Kubernetes cluster metrics for the python-api deployment."""

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
        self._cpu_request_m = cpu_request_millicores
        self._initialized = False

    def _ensure_init(self) -> None:
        if not self._initialized:
            try:
                config.load_kube_config()
            except config.ConfigException:
                config.load_incluster_config()
            self._initialized = True

    async def get_metrics(self) -> ClusterSnapshot:
        """Fetch cluster metrics. Runs blocking K8s API calls in a thread."""
        self._ensure_init()
        return await asyncio.to_thread(self._get_metrics_sync)

    def _get_metrics_sync(self) -> ClusterSnapshot:
        custom_api = client.CustomObjectsApi()
        v1 = client.CoreV1Api()
        autoscaling_api = client.AutoscalingV2Api()

        # Get pod metrics (CPU, memory)
        pod_metrics_map: dict[str, tuple[float, float]] = {}
        try:
            metrics_result = custom_api.list_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=self._namespace,
                plural="pods",
                label_selector=f"app={self._deployment_name}",
            )
            for item in metrics_result.get("items", []):
                name = item["metadata"]["name"]
                for container in item.get("containers", []):
                    cpu_cores = parse_cpu(container["usage"]["cpu"])
                    mem_mb = parse_memory(container["usage"]["memory"])
                    # CPU percent relative to request
                    cpu_pct = (cpu_cores * 1000 / self._cpu_request_m) * 100
                    pod_metrics_map[name] = (round(cpu_pct, 1), round(mem_mb, 1))
        except ApiException:
            pass  # metrics-server may not be ready

        # Get pod list and statuses
        pods_result = v1.list_namespaced_pod(
            namespace=self._namespace,
            label_selector=f"app={self._deployment_name}",
        )
        pods: list[PodMetrics] = []
        for pod in pods_result.items:
            name = pod.metadata.name
            status = pod.status.phase or "Unknown"
            cpu_pct, mem_mb = pod_metrics_map.get(name, (0.0, 0.0))
            pods.append(PodMetrics(name=name, cpu_percent=cpu_pct, memory_mb=mem_mb, status=status))

        # Get HPA status
        hpa_desired = len(pods)
        hpa_current_cpu = None
        try:
            hpa = autoscaling_api.read_namespaced_horizontal_pod_autoscaler(
                name=self._hpa_name,
                namespace=self._namespace,
            )
            hpa_desired = hpa.status.desired_replicas or len(pods)
            if hpa.status.current_metrics:
                for metric in hpa.status.current_metrics:
                    if metric.resource and metric.resource.name == "cpu":
                        hpa_current_cpu = metric.resource.current.average_utilization
        except ApiException:
            pass  # HPA may not exist yet

        return ClusterSnapshot(
            replicas=len([p for p in pods if p.status == "Running"]),
            hpa_desired=hpa_desired,
            hpa_current_cpu=hpa_current_cpu,
            pods=pods,
        )

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

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

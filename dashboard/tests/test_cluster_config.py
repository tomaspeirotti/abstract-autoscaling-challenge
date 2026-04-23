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

"""Branch coverage for _validate_service in status_page.config_schema.

Exercises every service type and the optional-field validation branches
(timeout, requires_env, expected_status, auth_token_env, command list,
azure types) that drive the function's complexity.
"""

import pytest

from status_page.config_schema import validate_config


def _svc(**kwargs):
    """Validate a config containing a single top-level service."""
    return validate_config({"services": [kwargs]})


# --- happy paths for each type ---


def test_http_with_all_optional_fields():
    cfg = _svc(
        name="API",
        type="http",
        url="https://example.com",
        interval=30,
        timeout=10,
        requires_env="ON_VPN",
        expected_status=204,
        auth_token_env="API_TOKEN",
    )
    assert cfg["services"][0]["expected_status"] == 204


def test_tcp_valid():
    cfg = _svc(name="Redis", type="tcp", host="localhost", port=6379)
    assert cfg["services"][0]["port"] == 6379


def test_dns_valid():
    cfg = _svc(name="DNS", type="dns", hostname="example.com")
    assert cfg["services"][0]["hostname"] == "example.com"


def test_script_with_string_command():
    cfg = _svc(name="Job", type="script", command="/usr/bin/true")
    assert cfg["services"][0]["command"] == "/usr/bin/true"


def test_script_with_list_command():
    cfg = _svc(name="Job", type="script", command=["echo", "hello"])
    assert cfg["services"][0]["command"] == ["echo", "hello"]


def test_azure_resource_health_valid():
    cfg = _svc(
        name="VM",
        type="azure_resource_health",
        resource_id="/subscriptions/x/resourceGroups/y",
    )
    assert cfg["services"][0]["type"] == "azure_resource_health"


def test_azure_service_health_with_subscription():
    cfg = _svc(
        name="Health",
        type="azure_service_health",
        subscription_id="sub-123",
    )
    assert cfg["services"][0]["subscription_id"] == "sub-123"


def test_azure_service_health_without_subscription_ok():
    # subscription_id is optional for the service variant
    cfg = _svc(name="Health", type="azure_service_health")
    assert cfg["services"][0]["type"] == "azure_service_health"


# --- error branches ---


def test_rejects_unknown_type():
    with pytest.raises(ValueError, match="type must be one of"):
        _svc(name="X", type="bogus")


def test_rejects_missing_name():
    with pytest.raises(ValueError, match="name"):
        _svc(type="http", url="https://example.com")


def test_rejects_non_dict_service():
    with pytest.raises(ValueError, match="must be a mapping"):
        validate_config({"services": ["not-a-dict"]})


def test_rejects_bad_timeout():
    with pytest.raises(ValueError, match="timeout"):
        _svc(name="API", type="http", url="https://example.com", timeout=0)


def test_rejects_bad_requires_env():
    with pytest.raises(ValueError, match="requires_env"):
        _svc(name="API", type="http", url="https://example.com", requires_env="")


def test_rejects_bad_expected_status_too_high():
    with pytest.raises(ValueError, match="expected_status"):
        _svc(name="API", type="http", url="https://example.com", expected_status=600)


def test_rejects_bad_auth_token_env():
    with pytest.raises(ValueError, match="auth_token_env"):
        _svc(name="API", type="http", url="https://example.com", auth_token_env=123)


def test_rejects_tcp_missing_host():
    with pytest.raises(ValueError, match="host"):
        _svc(name="Redis", type="tcp", port=6379)


def test_rejects_tcp_port_too_low():
    with pytest.raises(ValueError, match="port"):
        _svc(name="Redis", type="tcp", host="localhost", port=0)


def test_rejects_dns_missing_hostname():
    with pytest.raises(ValueError, match="hostname"):
        _svc(name="DNS", type="dns")


def test_rejects_empty_script_command_list():
    with pytest.raises(ValueError, match="must not be empty"):
        _svc(name="Job", type="script", command=[])


def test_rejects_non_string_in_script_command_list():
    with pytest.raises(ValueError, match=r"command\[1\]"):
        _svc(name="Job", type="script", command=["echo", 5])


def test_rejects_missing_script_command():
    with pytest.raises(ValueError, match="command"):
        _svc(name="Job", type="script")


def test_rejects_azure_resource_health_missing_resource_id():
    with pytest.raises(ValueError, match="resource_id"):
        _svc(name="VM", type="azure_resource_health")


def test_rejects_azure_service_health_bad_subscription():
    with pytest.raises(ValueError, match="subscription_id"):
        _svc(name="Health", type="azure_service_health", subscription_id="")

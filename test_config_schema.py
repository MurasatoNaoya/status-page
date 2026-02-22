import pytest

from config_schema import validate_config


def test_validate_config_accepts_minimal():
    cfg = validate_config(
        {
            "page": {"title": "Status", "description": "Service Status"},
            "status_feeds": [],
            "services": [],
            "groups": [],
        }
    )
    assert cfg["page"]["title"] == "Status"


def test_validate_config_rejects_bad_interval():
    with pytest.raises(ValueError):
        validate_config({"services": [{"name": "Svc", "interval": 0}]})


def test_validate_config_rejects_bad_feed():
    with pytest.raises(ValueError):
        validate_config({"status_feeds": [{"name": "FeedWithoutUrl"}]})


def test_validate_config_rejects_http_service_without_url():
    with pytest.raises(ValueError):
        validate_config({"services": [{"name": "Web", "type": "http"}]})


def test_validate_config_rejects_bad_tcp_port():
    with pytest.raises(ValueError):
        validate_config(
            {
                "services": [
                    {"name": "Redis", "type": "tcp", "host": "localhost", "port": 70000}
                ]
            }
        )


def test_validate_config_rejects_dns_target_missing_hostname():
    with pytest.raises(ValueError):
        validate_config({"dns_bar": {"targets": [{"label": "No host"}]}})


def test_validate_config_rejects_bool_interval():
    with pytest.raises(ValueError):
        validate_config({"services": [{"name": "Svc", "interval": True}]})


def test_validate_config_accepts_http_service():
    cfg = validate_config(
        {"services": [{"name": "API", "type": "http", "url": "https://example.com"}]}
    )
    assert cfg["services"][0]["type"] == "http"


def test_validate_config_accepts_dns_bar_with_requires_env():
    cfg = validate_config(
        {
            "dns_bar": {
                "name": "DNS",
                "interval": 60,
                "targets": [
                    {
                        "hostname": "api.github.com",
                        "label": "GitHub API",
                        "requires_env": "ON_PRIVATE_NETWORK",
                    }
                ],
            }
        }
    )
    assert cfg["dns_bar"]["targets"][0]["requires_env"] == "ON_PRIVATE_NETWORK"

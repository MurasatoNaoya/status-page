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

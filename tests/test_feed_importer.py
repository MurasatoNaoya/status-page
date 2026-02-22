"""Direct tests for feed_importer.py incident sync behavior."""

import sqlite3
from unittest.mock import patch

import status_page.database as database
from status_page import feed_importer


def _item(**overrides):
    base = {
        "source": "GitHub",
        "title": "Incident with Actions",
        "external_id": "abc123",
        "impact": "partial",
        "status": "investigating",
        "services": ["GitHub Actions"],
        "updates": [
            {
                "status": "investigating",
                "message": "Investigating.",
                "created_at": "2026-02-22T10:00:00Z",
            }
        ],
        "created_at": "2026-02-22T10:00:00Z",
        "resolved_at": None,
    }
    base.update(overrides)
    return base


def test_import_active_incident_sends_alert_and_saves_jira(monkeypatch):
    monkeypatch.setattr(feed_importer, "poll_feed", lambda cfg: [_item()])
    monkeypatch.setattr(feed_importer, "get_incident_by_external_id", lambda _x: None)

    created = {}

    def _create(**kwargs):
        created.update(kwargs)
        return 42

    monkeypatch.setattr(feed_importer, "create_incident", _create)

    alerts = []
    monkeypatch.setattr(
        feed_importer,
        "send_alerts",
        lambda **kwargs: alerts.append(kwargs) or "ABC-123",
    )

    jira_links = []
    monkeypatch.setattr(
        feed_importer,
        "set_incident_jira_key",
        lambda inc_id, key: jira_links.append((inc_id, key)),
    )

    monkeypatch.setattr(feed_importer, "update_incident", lambda *a, **k: None)
    monkeypatch.setattr(feed_importer, "send_resolution", lambda **_k: None)
    monkeypatch.setattr(feed_importer, "update_incident_impact", lambda *a, **k: None)

    feed_importer.poll_status_feed({"name": "GitHub"})

    assert created["external_id"] == "abc123:GitHub Actions"
    assert alerts and alerts[0]["incident_id"] == 42
    assert jira_links == [(42, "ABC-123")]


def test_import_resolved_incident_does_not_send_alert(monkeypatch):
    monkeypatch.setattr(
        feed_importer,
        "poll_feed",
        lambda cfg: [_item(status="resolved", resolved_at="2026-02-22T12:00:00Z")],
    )
    monkeypatch.setattr(feed_importer, "get_incident_by_external_id", lambda _x: None)
    monkeypatch.setattr(feed_importer, "create_incident", lambda **_kwargs: 7)

    alert_calls = []
    monkeypatch.setattr(
        feed_importer, "send_alerts", lambda **kwargs: alert_calls.append(kwargs)
    )

    monkeypatch.setattr(feed_importer, "update_incident", lambda *a, **k: None)
    monkeypatch.setattr(feed_importer, "set_incident_jira_key", lambda *_a: None)
    monkeypatch.setattr(feed_importer, "send_resolution", lambda **_k: None)
    monkeypatch.setattr(feed_importer, "update_incident_impact", lambda *a, **k: None)

    feed_importer.poll_status_feed({"name": "GitHub"})

    assert alert_calls == []


def test_existing_incident_resolve_sends_resolution(monkeypatch):
    monkeypatch.setattr(
        feed_importer,
        "poll_feed",
        lambda cfg: [_item(status="resolved", resolved_at="2026-02-22T12:00:00Z")],
    )

    existing = {
        "id": 11,
        "status": "investigating",
        "impact": "partial",
        "jira_key": "ABC-11",
    }
    monkeypatch.setattr(
        feed_importer, "get_incident_by_external_id", lambda _x: existing
    )

    monkeypatch.setattr(feed_importer, "update_incident", lambda *a, **k: None)
    monkeypatch.setattr(feed_importer, "update_incident_impact", lambda *a, **k: None)

    resolutions = []
    monkeypatch.setattr(
        feed_importer, "send_resolution", lambda **kwargs: resolutions.append(kwargs)
    )
    monkeypatch.setattr(feed_importer, "send_alerts", lambda **_k: None)
    monkeypatch.setattr(feed_importer, "set_incident_jira_key", lambda *_a: None)

    feed_importer.poll_status_feed({"name": "GitHub"})

    assert resolutions and resolutions[0]["incident_id"] == 11


def test_existing_incident_reopen_sends_alert(monkeypatch):
    monkeypatch.setattr(
        feed_importer, "poll_feed", lambda cfg: [_item(status="investigating")]
    )

    existing = {
        "id": 99,
        "status": "resolved",
        "impact": "partial",
        "jira_key": None,
    }
    monkeypatch.setattr(
        feed_importer, "get_incident_by_external_id", lambda _x: existing
    )

    monkeypatch.setattr(feed_importer, "update_incident", lambda *a, **k: None)
    monkeypatch.setattr(feed_importer, "update_incident_impact", lambda *a, **k: None)
    monkeypatch.setattr(feed_importer, "send_resolution", lambda **_k: None)

    alerts = []
    monkeypatch.setattr(
        feed_importer,
        "send_alerts",
        lambda **kwargs: alerts.append(kwargs) or "ABC-99",
    )

    jira_links = []
    monkeypatch.setattr(
        feed_importer,
        "set_incident_jira_key",
        lambda inc_id, key: jira_links.append((inc_id, key)),
    )

    feed_importer.poll_status_feed({"name": "GitHub"})

    assert alerts and "reopened" in alerts[0]["message"].lower()
    assert jira_links == [(99, "ABC-99")]


def test_integrityerror_race_path_still_sends_resolution(monkeypatch):
    monkeypatch.setattr(
        feed_importer,
        "poll_feed",
        lambda cfg: [_item(status="resolved", resolved_at="2026-02-22T12:00:00Z")],
    )

    calls = {"n": 0}

    def _get_existing(_svc_ext_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return {
            "id": 501,
            "status": "investigating",
            "impact": "partial",
            "jira_key": None,
        }

    monkeypatch.setattr(feed_importer, "get_incident_by_external_id", _get_existing)

    def _raise_integrity(**_kwargs):
        raise sqlite3.IntegrityError("duplicate")

    monkeypatch.setattr(feed_importer, "create_incident", _raise_integrity)
    monkeypatch.setattr(feed_importer, "update_incident", lambda *a, **k: None)
    monkeypatch.setattr(feed_importer, "update_incident_impact", lambda *a, **k: None)

    resolutions = []
    monkeypatch.setattr(
        feed_importer, "send_resolution", lambda **kwargs: resolutions.append(kwargs)
    )
    monkeypatch.setattr(feed_importer, "send_alerts", lambda **_k: None)
    monkeypatch.setattr(feed_importer, "set_incident_jira_key", lambda *_a: None)

    feed_importer.poll_status_feed({"name": "GitHub"})

    assert resolutions and resolutions[0]["incident_id"] == 501


# ---------------------------------------------------------------------------
# Integration tests (require database fixture from conftest.py)
# ---------------------------------------------------------------------------


class TestPollStatusFeedServiceFiltering:
    """Test that poll_status_feed correctly skips or creates incidents based on service mapping."""

    def test_feed_with_components_skips_unmapped_incidents(self):
        """When a feed has a component map, incidents with no matching services should be skipped."""
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "unmapped-123",
                "title": "Docker Desktop issue",
                "status": "resolved",
                "services": None,
                "updates": [{"status": "investigating", "message": "Looking"}],
                "source": "Docker",
            }
        ]
        feed_config = {
            "name": "Docker",
            "components": {"Docker Hub Registry": "Docker Hub"},
        }
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed(feed_config)

        inc = database.get_incident_by_external_id("unmapped-123")
        assert inc is None

    def test_feed_with_components_creates_mapped_incidents(self):
        """When a feed has a component map, incidents WITH matching services should be created."""
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "mapped-456",
                "title": "Docker Hub outage",
                "status": "resolved",
                "services": ["Docker Hub"],
                "updates": [{"status": "investigating", "message": "Looking"}],
                "source": "Docker",
            }
        ]
        feed_config = {
            "name": "Docker",
            "components": {"Docker Hub Registry": "Docker Hub"},
        }
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed(feed_config)

        inc = database.get_incident_by_external_id("mapped-456:Docker Hub")
        assert inc is not None
        assert inc["service_name"] == "Docker Hub"

    def test_feed_without_components_creates_null_service_incidents(self):
        """Feeds without component maps should still create incidents with service_name=NULL."""
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "azure-rss-999",
                "title": "Some Azure issue",
                "status": "resolved",
                "services": None,
                "updates": [{"status": "investigating", "message": "Looking"}],
                "source": "Azure",
            }
        ]
        feed_config = {"name": "Azure"}
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed(feed_config)

        inc = database.get_incident_by_external_id("azure-rss-999")
        assert inc is not None
        assert inc["service_name"] is None


class TestPollStatusFeedIntegration:
    """Test poll_status_feed per-service incident creation (integration tests)."""

    def test_creates_one_incident_per_affected_service(self):
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "test-multi-svc",
                "title": "Multi-service outage",
                "status": "resolved",
                "impact": "partial",
                "services": ["ServiceA", "ServiceB", "ServiceC"],
                "created_at": "2026-02-14T10:00:00Z",
                "resolved_at": "2026-02-14T12:00:00Z",
                "source": "TestFeed",
                "updates": [
                    {"status": "investigating", "message": "Looking into it"},
                ],
            }
        ]
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed({"name": "TestFeed"})

        a = database.get_incident_by_external_id("test-multi-svc:ServiceA")
        b = database.get_incident_by_external_id("test-multi-svc:ServiceB")
        c = database.get_incident_by_external_id("test-multi-svc:ServiceC")
        assert a is not None
        assert b is not None
        assert c is not None
        assert a["service_name"] == "ServiceA"
        assert b["service_name"] == "ServiceB"

    def test_skips_already_imported_incidents(self):
        from status_page.feed_importer import poll_status_feed

        database.create_incident(
            title="Existing", external_id="existing-123", service_name="Svc"
        )
        feed_results = [
            {
                "external_id": "existing-123",
                "title": "Existing incident",
                "status": "investigating",
                "services": ["Svc"],
                "updates": [],
                "source": "TestFeed",
            }
        ]
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed({"name": "TestFeed"})

        incidents = database.get_recent_incidents(limit=50)
        matching = [i for i in incidents if i["external_id"] == "existing-123"]
        assert len(matching) == 1

    def test_updates_status_to_resolved(self):
        from status_page.feed_importer import poll_status_feed

        database.create_incident(
            title="Open incident",
            external_id="resolve-me:Svc",
            service_name="Svc",
            status="investigating",
        )
        feed_results = [
            {
                "external_id": "resolve-me",
                "title": "Open incident",
                "status": "resolved",
                "services": ["Svc"],
                "updates": [],
                "source": "TestFeed",
            }
        ]
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed({"name": "TestFeed"})

        inc = database.get_incident_by_external_id("resolve-me:Svc")
        assert inc["status"] == "resolved"
        assert inc["resolved_at"] is not None

    def test_skips_component_status_items(self):
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "type": "component_status",
                "service_name": "Svc",
                "status": "operational",
                "source": "TestFeed",
            }
        ]
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed({"name": "TestFeed"})
        assert database.get_recent_incidents(limit=10) == []

    def test_incident_with_no_services_uses_none(self):
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "no-svc-123",
                "title": "Unknown service outage",
                "status": "investigating",
                "services": None,
                "updates": [{"status": "investigating", "message": "Looking into it"}],
                "source": "TestFeed",
            }
        ]
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed({"name": "TestFeed"})

        inc = database.get_incident_by_external_id("no-svc-123")
        assert inc is not None
        assert inc["service_name"] is None

    def test_new_active_feed_incident_sends_alerts(self):
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "active-123",
                "title": "Active outage",
                "status": "investigating",
                "impact": "major",
                "services": ["Svc"],
                "updates": [{"status": "investigating", "message": "Investigating"}],
                "source": "TestFeed",
            }
        ]
        with (
            patch("status_page.feed_importer.poll_feed", return_value=feed_results),
            patch(
                "status_page.feed_importer.send_alerts", return_value=None
            ) as mock_alerts,
        ):
            poll_status_feed({"name": "TestFeed"})

        mock_alerts.assert_called_once()

    def test_new_resolved_feed_incident_does_not_send_alerts(self):
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "resolved-123",
                "title": "Old resolved outage",
                "status": "resolved",
                "impact": "minor",
                "services": ["Svc"],
                "updates": [{"status": "resolved", "message": "Resolved"}],
                "source": "TestFeed",
            }
        ]
        with (
            patch("status_page.feed_importer.poll_feed", return_value=feed_results),
            patch("status_page.feed_importer.send_alerts") as mock_alerts,
        ):
            poll_status_feed({"name": "TestFeed"})

        mock_alerts.assert_not_called()

    def test_existing_feed_incident_resolution_sends_resolution_alert(self):
        from status_page.feed_importer import poll_status_feed

        database.create_incident(
            title="Open feed incident",
            external_id="resolve-me-2:Svc",
            service_name="Svc",
            status="investigating",
            jira_key="OPS-123",
        )
        feed_results = [
            {
                "external_id": "resolve-me-2",
                "title": "Open feed incident",
                "status": "resolved",
                "impact": "minor",
                "services": ["Svc"],
                "updates": [],
                "source": "TestFeed",
            }
        ]
        with (
            patch("status_page.feed_importer.poll_feed", return_value=feed_results),
            patch("status_page.feed_importer.send_resolution") as mock_resolution,
        ):
            poll_status_feed({"name": "TestFeed"})

        mock_resolution.assert_called_once()

    def test_existing_resolved_feed_incident_reopen_sends_alert(self):
        from status_page.feed_importer import poll_status_feed

        database.create_incident(
            title="Resolved feed incident",
            external_id="reopen-me:Svc",
            service_name="Svc",
            status="resolved",
            jira_key="OPS-777",
        )
        feed_results = [
            {
                "external_id": "reopen-me",
                "title": "Resolved feed incident",
                "status": "investigating",
                "impact": "major",
                "services": ["Svc"],
                "updates": [],
                "source": "TestFeed",
            }
        ]
        with (
            patch("status_page.feed_importer.poll_feed", return_value=feed_results),
            patch(
                "status_page.feed_importer.send_alerts", return_value=None
            ) as mock_alerts,
        ):
            poll_status_feed({"name": "TestFeed"})

        mock_alerts.assert_called_once()

    def test_integrity_race_path_applies_reopen_alert_logic(self):
        from status_page.feed_importer import poll_status_feed
        import sqlite3

        feed_results = [
            {
                "external_id": "race-id",
                "title": "Race incident",
                "status": "investigating",
                "impact": "partial",
                "services": ["Svc"],
                "updates": [],
                "source": "TestFeed",
            }
        ]
        existing = {
            "id": 99,
            "status": "resolved",
            "impact": "minor",
            "jira_key": "OPS-99",
        }
        with (
            patch("status_page.feed_importer.poll_feed", return_value=feed_results),
            patch(
                "status_page.feed_importer.get_incident_by_external_id",
                side_effect=[None, existing],
            ),
            patch(
                "status_page.feed_importer.create_incident",
                side_effect=sqlite3.IntegrityError(),
            ),
            patch(
                "status_page.feed_importer._sync_existing_incident",
                return_value=("resolved", "investigating"),
            ),
            patch(
                "status_page.feed_importer.send_alerts", return_value=None
            ) as mock_alerts,
        ):
            poll_status_feed({"name": "TestFeed"})

        mock_alerts.assert_called_once()

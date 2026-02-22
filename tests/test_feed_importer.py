"""Direct tests for feed_importer.py incident sync behavior."""

import sqlite3

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

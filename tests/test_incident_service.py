from unittest.mock import patch

import status_page.database as database
from status_page.incident_service import (
    declare_incident_with_alerts,
    resolve_incident_with_alerts,
)


class TestIncidentService:
    def test_declare_persists_jira_key(self):
        with patch("status_page.incident_service.send_alerts", return_value="OPS-500"):
            inc_id = declare_incident_with_alerts(
                title="Outage",
                impact="major",
                message="Investigating",
                service_name="GitHub API",
            )
        inc = database.get_incident(inc_id)
        assert inc["jira_key"] == "OPS-500"

    def test_declare_without_jira_key_leaves_null(self):
        with patch("status_page.incident_service.send_alerts", return_value=None):
            inc_id = declare_incident_with_alerts(
                title="Outage",
                impact="major",
                message="Investigating",
                service_name="GitHub API",
            )
        inc = database.get_incident(inc_id)
        assert inc["jira_key"] is None

    def test_resolve_uses_persisted_jira_key(self):
        inc_id = database.create_incident(
            title="Outage", impact="major", message="Investigating", jira_key="OPS-500"
        )
        with patch("status_page.incident_service.send_resolution") as mock_resolution:
            ok = resolve_incident_with_alerts(inc_id, "fixed")
        assert ok is True
        assert mock_resolution.call_args.kwargs["jira_key"] == "OPS-500"

    def test_resolve_returns_false_for_missing_incident(self):
        with patch("status_page.incident_service.send_resolution") as mock_resolution:
            ok = resolve_incident_with_alerts(999999, "fixed")
        assert ok is False
        mock_resolution.assert_not_called()

    def test_resolve_returns_false_when_update_fails(self):
        inc_id = database.create_incident(
            title="Outage", impact="major", message="Investigating"
        )
        with (
            patch("status_page.incident_service.update_incident", return_value=False),
            patch("status_page.incident_service.send_resolution") as mock_resolution,
        ):
            ok = resolve_incident_with_alerts(inc_id, "fixed")
        assert ok is False
        mock_resolution.assert_not_called()

    def test_resolve_sends_resolution_with_null_jira_key(self):
        inc_id = database.create_incident(
            title="Outage", impact="major", message="Investigating"
        )
        with patch("status_page.incident_service.send_resolution") as mock_resolution:
            ok = resolve_incident_with_alerts(inc_id, "fixed")
        assert ok is True
        assert mock_resolution.call_args.kwargs["jira_key"] is None

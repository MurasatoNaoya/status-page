from unittest.mock import patch

import database
from incident_service import declare_incident_with_alerts, resolve_incident_with_alerts


class TestIncidentService:
    def test_declare_persists_jira_key(self):
        with patch("incident_service.send_alerts", return_value="OPS-500"):
            inc_id = declare_incident_with_alerts(
                title="Outage",
                impact="major",
                message="Investigating",
                service_name="GitHub API",
            )
        inc = database.get_incident(inc_id)
        assert inc["jira_key"] == "OPS-500"

    def test_resolve_uses_persisted_jira_key(self):
        inc_id = database.create_incident(
            title="Outage", impact="major", message="Investigating", jira_key="OPS-500"
        )
        with patch("incident_service.send_resolution") as mock_resolution:
            ok = resolve_incident_with_alerts(inc_id, "fixed")
        assert ok is True
        assert mock_resolution.call_args.kwargs["jira_key"] == "OPS-500"

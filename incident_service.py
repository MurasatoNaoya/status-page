"""Incident orchestration helpers."""

from alerts import send_alerts, send_resolution
from database import (
    create_incident,
    get_incident,
    set_incident_jira_key,
    update_incident,
)


def declare_incident_with_alerts(title, impact, message, service_name=None):
    incident_id = create_incident(
        title=title, impact=impact, message=message, service_name=service_name
    )
    jira_key = send_alerts(
        incident_id=incident_id,
        title=title,
        impact=impact,
        message=message,
        service=service_name,
    )
    if jira_key:
        set_incident_jira_key(incident_id, jira_key)
    return incident_id


def resolve_incident_with_alerts(incident_id, message):
    incident = get_incident(incident_id)
    if not incident:
        return False
    updated = update_incident(incident_id, status="resolved", message=message)
    if not updated:
        return False
    send_resolution(
        incident_id=incident_id,
        message=message,
        jira_key=incident.get("jira_key"),
    )
    return True

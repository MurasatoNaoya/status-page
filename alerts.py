"""Alert integrations for incident notifications.

Configure via environment variables:
  SLACK_WEBHOOK_URL   - Slack incoming webhook for #incidents channel
  JIRA_URL            - e.g. https://yourcompany.atlassian.net
  JIRA_PROJECT        - e.g. OPS
  JIRA_USER           - e.g. you@company.com
  JIRA_TOKEN          - API token from https://id.atlassian.net/manage-profile/security/api-tokens
  ALERT_EMAIL_TO      - comma-separated emails (requires SMTP config)
  TEAMS_WEBHOOK_URL   - Microsoft Teams incoming webhook
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)


def send_alerts(incident_id, title, impact, message, service=None):
    """Send alert to all configured channels."""
    _send_slack(incident_id, title, impact, message, service)
    _send_teams(incident_id, title, impact, message, service)
    return _create_jira_ticket(incident_id, title, impact, message, service)


def send_resolution(incident_id, message, jira_key=None):
    """Notify channels that an incident has been resolved."""
    slack_url = os.environ.get("SLACK_WEBHOOK_URL")
    if slack_url:
        try:
            payload = {
                "text": f":white_check_mark: *Incident #{incident_id} Resolved*\n{message}",
            }
            requests.post(slack_url, json=payload, timeout=10)
        except Exception as e:
            logger.error("Slack resolution alert failed: %s", e)

    teams_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if teams_url:
        try:
            payload = {
                "text": f"Incident #{incident_id} Resolved: {message}",
            }
            requests.post(teams_url, json=payload, timeout=10)
        except Exception as e:
            logger.error("Teams resolution alert failed: %s", e)

    _resolve_jira_ticket(incident_id, message, jira_key=jira_key)


def _send_slack(incident_id, title, impact, message, service):
    webhook_url = os.environ.get("SLACK_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("SLACK_WEBHOOK_URL not set, skipping Slack alert")
        return

    emoji = {
        "major": ":rotating_light:",
        "partial": ":red_circle:",
        "minor": ":warning:",
    }.get(impact, ":warning:")

    svc_text = f" ({service})" if service else ""
    payload = {
        "text": f"{emoji} *Incident Declared{svc_text}*",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} {title}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Impact:* {impact.upper()}"},
                    {"type": "mrkdwn", "text": f"*Service:* {service or 'Multiple'}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message},
            },
        ],
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Slack alert sent for incident #%d", incident_id)
    except Exception as e:
        logger.error("Slack alert failed: %s", e)


def _send_teams(incident_id, title, impact, message, service):
    webhook_url = os.environ.get("TEAMS_WEBHOOK_URL")
    if not webhook_url:
        logger.debug("TEAMS_WEBHOOK_URL not set, skipping Teams alert")
        return

    svc_text = f" ({service})" if service else ""
    payload = {
        "text": f"**Incident Declared{svc_text}**: {title}\n\n**Impact:** {impact.upper()}\n\n{message}",
    }

    try:
        resp = requests.post(webhook_url, json=payload, timeout=10)
        resp.raise_for_status()
        logger.info("Teams alert sent for incident #%d", incident_id)
    except Exception as e:
        logger.error("Teams alert failed: %s", e)


def _create_jira_ticket(incident_id, title, impact, message, service):
    jira_url = os.environ.get("JIRA_URL")
    project = os.environ.get("JIRA_PROJECT")
    user = os.environ.get("JIRA_USER")
    token = os.environ.get("JIRA_TOKEN")

    if not all([jira_url, project, user, token]):
        logger.debug("JIRA env vars not fully set, skipping Jira ticket")
        return None

    priority_map = {
        "major": "Highest",
        "partial": "High",
        "minor": "Medium",
    }

    svc_text = f" [{service}]" if service else ""
    payload = {
        "fields": {
            "project": {"key": project},
            "summary": f"[INCIDENT]{svc_text} {title}",
            "description": f"Impact: {impact.upper()}\n\n{message}\n\nAuto-created by status-page (incident #{incident_id})",
            "issuetype": {"name": "Bug"},
            "priority": {"name": priority_map.get(impact, "Medium")},
            "labels": ["status-page-incident", f"incident-{incident_id}"],
        }
    }

    try:
        resp = requests.post(
            f"{jira_url.rstrip('/')}/rest/api/2/issue",
            json=payload,
            auth=(user, token),
            headers={"Content-Type": "application/json"},
            timeout=15,
        )
        resp.raise_for_status()
        issue_key = resp.json().get("key", "?")
        logger.info("Jira ticket %s created for incident #%d", issue_key, incident_id)
        return issue_key
    except Exception as e:
        logger.error("Jira ticket creation failed: %s", e)
        return None


def _resolve_jira_ticket(incident_id, message, jira_key=None):
    """Comment on and transition the Jira ticket tied to an incident (best effort)."""
    jira_url = os.environ.get("JIRA_URL")
    project = os.environ.get("JIRA_PROJECT")
    user = os.environ.get("JIRA_USER")
    token = os.environ.get("JIRA_TOKEN")
    if not all([jira_url, project, user, token]):
        logger.debug("JIRA env vars not fully set, skipping Jira resolution")
        return

    auth = (user, token)
    base = jira_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    try:
        issue_key = jira_key
        if not issue_key:
            label_jql = (
                f"project = {project} "
                f'AND labels = "status-page-incident" '
                f'AND labels = "incident-{incident_id}" '
                "ORDER BY created DESC"
            )
            search = requests.get(
                f"{base}/rest/api/2/search",
                params={"jql": label_jql, "maxResults": 1, "fields": "key,status"},
                auth=auth,
                headers=headers,
                timeout=15,
            )
            search.raise_for_status()
            issues = search.json().get("issues", [])
            if not issues:
                # Backward compatibility for older tickets created before labels were added.
                legacy_jql = (
                    f"project = {project} AND ("
                    f'summary ~ "\\"incident #{incident_id}\\"" OR '
                    f'description ~ "\\"incident #{incident_id}\\""'
                    ") ORDER BY created DESC"
                )
                search = requests.get(
                    f"{base}/rest/api/2/search",
                    params={"jql": legacy_jql, "maxResults": 1, "fields": "key,status"},
                    auth=auth,
                    headers=headers,
                    timeout=15,
                )
                search.raise_for_status()
                issues = search.json().get("issues", [])
            if not issues:
                logger.info("No Jira ticket found for incident #%d", incident_id)
                return
            issue_key = issues[0]["key"]
        requests.post(
            f"{base}/rest/api/2/issue/{issue_key}/comment",
            json={"body": f"Resolved via status-page: {message}"},
            auth=auth,
            headers=headers,
            timeout=15,
        ).raise_for_status()

        transitions_resp = requests.get(
            f"{base}/rest/api/2/issue/{issue_key}/transitions",
            auth=auth,
            headers=headers,
            timeout=15,
        )
        transitions_resp.raise_for_status()
        transitions = transitions_resp.json().get("transitions", [])
        preferred = {
            "done",
            "resolved",
            "resolve issue",
            "close issue",
            "closed",
        }
        target = next(
            (t for t in transitions if t.get("name", "").strip().lower() in preferred),
            None,
        )
        if target:
            requests.post(
                f"{base}/rest/api/2/issue/{issue_key}/transitions",
                json={"transition": {"id": target["id"]}},
                auth=auth,
                headers=headers,
                timeout=15,
            ).raise_for_status()
            logger.info(
                "Jira ticket %s transitioned via '%s' for incident #%d",
                issue_key,
                target.get("name", "?"),
                incident_id,
            )
        else:
            logger.info(
                "Jira ticket %s commented but no resolve transition found for incident #%d",
                issue_key,
                incident_id,
            )
    except Exception as e:
        logger.error("Jira resolution update failed: %s", e)

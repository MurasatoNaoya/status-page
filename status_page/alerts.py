"""Alert integrations for incident notifications.

Configure via environment variables:
  SLACK_WEBHOOK_URL   - Slack incoming webhook for #incidents channel
  TEAMS_WEBHOOK_URL   - Microsoft Teams incoming webhook
  JIRA_URL            - e.g. https://yourcompany.atlassian.net
  JIRA_PROJECT        - e.g. OPS
  JIRA_USER           - e.g. you@company.com
  JIRA_TOKEN          - API token from https://id.atlassian.net/manage-profile/security/api-tokens
  ALERT_EMAIL_TO      - comma-separated recipient emails
  RESEND_API_KEY      - Resend API key (preferred email transport)
  RESEND_FROM         - Verified sender address for Resend
  RESEND_REPLY_TO     - Optional reply-to address for Resend emails
  SMTP_HOST           - SMTP server hostname (required for email alerts)
  SMTP_PORT           - SMTP server port (default: 587)
  SMTP_USER           - SMTP username (optional)
  SMTP_PASS           - SMTP password (optional)
  SMTP_FROM           - From address (default: SMTP_USER or status-page@localhost)
  SMTP_STARTTLS       - Use STARTTLS on SMTP (default: true)
  SMTP_SSL            - Use implicit SSL SMTP (default: false)
"""

import logging
import os
import smtplib
from email.message import EmailMessage

import requests

logger = logging.getLogger(__name__)


def send_alerts(incident_id, title, impact, message, service=None):
    """Send alert to all configured channels."""
    _send_slack(incident_id, title, impact, message, service)
    _send_teams(incident_id, title, impact, message, service)
    _send_email_incident(incident_id, title, impact, message, service)
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

    _send_email_resolution(incident_id, message)
    _resolve_jira_ticket(incident_id, message, jira_key=jira_key)


def _send_email_incident(incident_id, title, impact, message, service):
    svc_text = f" ({service})" if service else ""
    subject = f"[Status Page] Incident #{incident_id}{svc_text}: {title}"
    body = (
        f"Incident #{incident_id} declared\n\n"
        f"Title: {title}\n"
        f"Impact: {impact.upper()}\n"
        f"Service: {service or 'Multiple'}\n\n"
        f"{message}\n"
    )
    _send_email(subject, body)


def _send_email_resolution(incident_id, message):
    subject = f"[Status Page] Incident #{incident_id} resolved"
    body = f"Incident #{incident_id} has been resolved.\n\n{message}\n"
    _send_email(subject, body)


def _send_email(subject, body):
    recipients_raw = os.environ.get("ALERT_EMAIL_TO", "")
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]
    recipients = [r for r in recipients if "@" in r]
    resend_api_key = os.environ.get("RESEND_API_KEY")
    resend_from = os.environ.get("RESEND_FROM")
    smtp_host = os.environ.get("SMTP_HOST")
    if not recipients:
        logger.debug("Email alert not configured (need ALERT_EMAIL_TO), skipping")
        return

    # Prefer Resend if configured; fall back to SMTP if it fails.
    if resend_api_key and resend_from:
        if _send_email_via_resend(
            subject, body, recipients, resend_api_key, resend_from
        ):
            return
        if not smtp_host:
            return

    if not smtp_host:
        logger.debug(
            "Email alert not configured (need RESEND_API_KEY/RESEND_FROM or SMTP_HOST), skipping"
        )
        return

    _send_email_via_smtp(subject, body, recipients, smtp_host)


def _send_email_via_resend(subject, body, recipients, api_key, from_email):
    payload = {
        "from": from_email,
        "to": recipients,
        "subject": subject,
        "text": body,
    }
    reply_to = os.environ.get("RESEND_REPLY_TO")
    if reply_to:
        payload["reply_to"] = reply_to
    try:
        resp = requests.post(
            "https://api.resend.com/emails",
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=15,
        )
        resp.raise_for_status()
        logger.info("Email alert sent via Resend to %d recipient(s)", len(recipients))
        return True
    except Exception as e:
        logger.error("Resend email alert failed: %s", e)
        return False


def _send_email_via_smtp(subject, body, recipients, smtp_host):
    raw_port = os.environ.get("SMTP_PORT", "587")
    try:
        smtp_port = int(raw_port)
    except (TypeError, ValueError):
        logger.warning("Invalid SMTP_PORT=%r; falling back to 587", raw_port)
        smtp_port = 587
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASS")
    smtp_from = os.environ.get("SMTP_FROM", smtp_user or "status-page@localhost")
    smtp_ssl = os.environ.get("SMTP_SSL", "").lower() in ("1", "true", "yes")
    smtp_starttls = os.environ.get("SMTP_STARTTLS", "true").lower() not in (
        "0",
        "false",
        "no",
    )

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = smtp_from
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)

    try:
        if smtp_ssl:
            with smtplib.SMTP_SSL(smtp_host, smtp_port, timeout=15) as server:
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        else:
            with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
                if smtp_starttls:
                    server.starttls()
                if smtp_user and smtp_pass:
                    server.login(smtp_user, smtp_pass)
                server.send_message(msg)
        logger.info("Email alert sent via SMTP to %d recipient(s)", len(recipients))
    except Exception as e:
        logger.error("SMTP email alert failed: %s", e)


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

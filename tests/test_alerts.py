"""Tests for alerts.py — Slack, Teams, Jira notification integrations."""

from unittest.mock import patch, MagicMock

import status_page.alerts as alerts


class TestSendAlerts:
    def test_send_alerts_dispatches_all_channels(self):
        with (
            patch.object(alerts, "_send_slack") as mock_slack,
            patch.object(alerts, "_send_teams") as mock_teams,
            patch.object(alerts, "_send_email_incident") as mock_email,
            patch.object(alerts, "_create_jira_ticket") as mock_jira,
        ):
            mock_jira.return_value = "OPS-1"
            alerts.send_alerts(1, "Outage", "major", "Investigating", "Svc")
            mock_slack.assert_called_once_with(
                1, "Outage", "major", "Investigating", "Svc"
            )
            mock_teams.assert_called_once_with(
                1, "Outage", "major", "Investigating", "Svc"
            )
            mock_email.assert_called_once_with(
                1, "Outage", "major", "Investigating", "Svc"
            )
            mock_jira.assert_called_once_with(
                1, "Outage", "major", "Investigating", "Svc"
            )

    def test_send_alerts_returns_jira_key(self):
        with patch.object(alerts, "_create_jira_ticket", return_value="OPS-9"):
            key = alerts.send_alerts(1, "Outage", "major", "Investigating", "Svc")
        assert key == "OPS-9"


class TestSlack:
    @patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"})
    @patch("status_page.alerts.requests.post")
    def test_sends_block_kit_payload(self, mock_post):
        mock_post.return_value.raise_for_status = lambda: None
        alerts._send_slack(1, "Outage", "major", "Investigating", "MySvc")
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "blocks" in payload
        assert payload["blocks"][0]["type"] == "header"

    @patch.dict("os.environ", {}, clear=True)
    @patch("status_page.alerts.requests.post")
    def test_skips_when_no_webhook(self, mock_post):
        alerts._send_slack(1, "Outage", "major", "msg", None)
        mock_post.assert_not_called()

    @patch.dict("os.environ", {"SLACK_WEBHOOK_URL": "https://hooks.slack.com/test"})
    @patch(
        "status_page.alerts.requests.post", side_effect=Exception("Connection refused")
    )
    def test_handles_post_failure(self, mock_post):
        # Should not raise
        alerts._send_slack(1, "Outage", "major", "msg", None)


class TestTeams:
    @patch.dict("os.environ", {"TEAMS_WEBHOOK_URL": "https://teams.webhook.test"})
    @patch("status_page.alerts.requests.post")
    def test_sends_teams_payload(self, mock_post):
        mock_post.return_value.raise_for_status = lambda: None
        alerts._send_teams(1, "Outage", "partial", "Investigating", "Svc")
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert "PARTIAL" in payload["text"]

    @patch.dict("os.environ", {}, clear=True)
    @patch("status_page.alerts.requests.post")
    def test_skips_when_no_webhook(self, mock_post):
        alerts._send_teams(1, "Outage", "major", "msg", None)
        mock_post.assert_not_called()


class TestJira:
    @patch.dict(
        "os.environ",
        {
            "JIRA_URL": "https://company.atlassian.net",
            "JIRA_PROJECT": "OPS",
            "JIRA_USER": "user@co.com",
            "JIRA_TOKEN": "token123",
        },
    )
    @patch("status_page.alerts.requests.post")
    def test_creates_jira_ticket(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "OPS-42"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp
        alerts._create_jira_ticket(1, "Outage", "major", "Investigating", "Svc")
        mock_post.assert_called_once()
        payload = mock_post.call_args[1]["json"]
        assert payload["fields"]["project"]["key"] == "OPS"
        assert payload["fields"]["priority"]["name"] == "Highest"
        assert "status-page-incident" in payload["fields"]["labels"]
        assert "incident-1" in payload["fields"]["labels"]

    @patch.dict(
        "os.environ",
        {
            "JIRA_URL": "https://company.atlassian.net",
            "JIRA_PROJECT": "OPS",
            "JIRA_USER": "user@co.com",
            "JIRA_TOKEN": "token123",
        },
    )
    @patch("status_page.alerts.requests.post")
    def test_priority_mapping(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"key": "OPS-1"}
        mock_resp.raise_for_status = lambda: None
        mock_post.return_value = mock_resp

        alerts._create_jira_ticket(1, "T", "minor", "m", None)
        assert mock_post.call_args[1]["json"]["fields"]["priority"]["name"] == "Medium"

    @patch.dict("os.environ", {}, clear=True)
    @patch("status_page.alerts.requests.post")
    def test_skips_when_not_configured(self, mock_post):
        alerts._create_jira_ticket(1, "T", "major", "m", None)
        mock_post.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "JIRA_URL": "https://company.atlassian.net",
            "JIRA_PROJECT": "OPS",
            "JIRA_USER": "user@co.com",
            "JIRA_TOKEN": "token123",
        },
    )
    @patch("status_page.alerts.requests.post", side_effect=Exception("Network error"))
    def test_handles_post_failure(self, mock_post):
        # Should not raise
        alerts._create_jira_ticket(1, "T", "major", "m", None)


class TestSendResolution:
    @patch.dict(
        "os.environ",
        {
            "SLACK_WEBHOOK_URL": "https://hooks.slack.com/test",
            "TEAMS_WEBHOOK_URL": "https://teams.webhook.test",
        },
    )
    @patch("status_page.alerts.requests.post")
    @patch("status_page.alerts._send_email_resolution")
    def test_sends_to_both_channels(self, mock_email, mock_post):
        alerts.send_resolution(42, "Service recovered")
        assert mock_post.call_count == 2
        mock_email.assert_called_once_with(42, "Service recovered")

    @patch.dict("os.environ", {}, clear=True)
    @patch("status_page.alerts.requests.post")
    def test_skips_when_not_configured(self, mock_post):
        alerts.send_resolution(42, "recovered")
        mock_post.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "JIRA_URL": "https://company.atlassian.net",
            "JIRA_PROJECT": "OPS",
            "JIRA_USER": "user@co.com",
            "JIRA_TOKEN": "token123",
        },
    )
    @patch("status_page.alerts.requests.get")
    @patch("status_page.alerts.requests.post")
    def test_resolution_attempts_jira_comment_and_transition(self, mock_post, mock_get):
        mock_get.side_effect = [
            MagicMock(
                json=lambda: {"issues": [{"key": "OPS-42"}]},
                raise_for_status=lambda: None,
            ),
            MagicMock(
                json=lambda: {"transitions": [{"id": "31", "name": "Done"}]},
                raise_for_status=lambda: None,
            ),
        ]
        mock_post.return_value.raise_for_status = lambda: None

        alerts.send_resolution(42, "Service recovered")

        assert mock_get.call_count == 2
        # comment + transition
        assert mock_post.call_count == 2

    @patch.dict(
        "os.environ",
        {
            "JIRA_URL": "https://company.atlassian.net",
            "JIRA_PROJECT": "OPS",
            "JIRA_USER": "user@co.com",
            "JIRA_TOKEN": "token123",
        },
    )
    @patch("status_page.alerts.requests.get")
    @patch("status_page.alerts.requests.post")
    def test_resolution_uses_jira_key_without_search(self, mock_post, mock_get):
        mock_get.return_value = MagicMock(
            json=lambda: {"transitions": [{"id": "31", "name": "Done"}]},
            raise_for_status=lambda: None,
        )
        mock_post.return_value.raise_for_status = lambda: None

        alerts.send_resolution(42, "Service recovered", jira_key="OPS-42")

        # Should fetch transitions only (no JQL search)
        assert mock_get.call_count == 1
        assert "/transitions" in mock_get.call_args.args[0]

    @patch.dict(
        "os.environ",
        {
            "JIRA_URL": "https://company.atlassian.net",
            "JIRA_PROJECT": "OPS",
            "JIRA_USER": "user@co.com",
            "JIRA_TOKEN": "token123",
        },
    )
    @patch("status_page.alerts.requests.get")
    @patch("status_page.alerts.requests.post")
    def test_resolution_falls_back_to_legacy_jql_when_labels_missing(
        self, mock_post, mock_get
    ):
        mock_get.side_effect = [
            MagicMock(json=lambda: {"issues": []}, raise_for_status=lambda: None),
            MagicMock(
                json=lambda: {"issues": [{"key": "OPS-42"}]},
                raise_for_status=lambda: None,
            ),
            MagicMock(
                json=lambda: {"transitions": [{"id": "31", "name": "Done"}]},
                raise_for_status=lambda: None,
            ),
        ]
        mock_post.return_value.raise_for_status = lambda: None

        alerts.send_resolution(42, "Service recovered")

        assert mock_get.call_count == 3
        first_jql = mock_get.call_args_list[0].kwargs["params"]["jql"]
        second_jql = mock_get.call_args_list[1].kwargs["params"]["jql"]
        assert 'labels = "status-page-incident"' in first_jql
        assert "description" in second_jql


class TestEmail:
    @patch.dict("os.environ", {}, clear=True)
    @patch("status_page.alerts.smtplib.SMTP")
    def test_email_skips_when_unconfigured(self, mock_smtp):
        alerts._send_email("Subject", "Body")
        mock_smtp.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "RESEND_API_KEY": "re_test",
            "RESEND_FROM": "alerts@example.com",
            "ALERT_EMAIL_TO": "ops@example.com",
        },
    )
    @patch("status_page.alerts.smtplib.SMTP")
    @patch("status_page.alerts.requests.post")
    def test_email_prefers_resend_when_configured(self, mock_post, mock_smtp):
        mock_post.return_value.raise_for_status = lambda: None

        alerts._send_email("Subject", "Body")

        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert args[0] == "https://api.resend.com/emails"
        assert kwargs["json"]["from"] == "alerts@example.com"
        assert kwargs["json"]["to"] == ["ops@example.com"]
        mock_smtp.assert_not_called()

    @patch.dict(
        "os.environ",
        {
            "RESEND_API_KEY": "re_test",
            "RESEND_FROM": "alerts@example.com",
            "ALERT_EMAIL_TO": "ops@example.com",
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "587",
        },
    )
    @patch("status_page.alerts.smtplib.SMTP")
    @patch("status_page.alerts.requests.post", side_effect=Exception("Resend down"))
    def test_email_falls_back_to_smtp_when_resend_fails(self, mock_post, mock_smtp):
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        alerts._send_email("Subject", "Body")

        mock_post.assert_called_once()
        mock_smtp.assert_called_once()
        server.send_message.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "not-a-port",
            "SMTP_USER": "alerts@example.com",
            "SMTP_PASS": "secret",
            "SMTP_FROM": "alerts@example.com",
            "ALERT_EMAIL_TO": "ops1@example.com, ops2@example.com",
        },
    )
    @patch("status_page.alerts.smtplib.SMTP")
    def test_email_sends_via_smtp_starttls(self, mock_smtp):
        server = MagicMock()
        mock_smtp.return_value.__enter__.return_value = server

        alerts._send_email("Subject", "Body")

        server.starttls.assert_called_once()
        server.login.assert_called_once_with("alerts@example.com", "secret")
        server.send_message.assert_called_once()
        mock_smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)

    @patch.dict(
        "os.environ",
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "465",
            "SMTP_SSL": "true",
            "ALERT_EMAIL_TO": "ops@example.com",
        },
    )
    @patch("status_page.alerts.smtplib.SMTP_SSL")
    def test_email_sends_via_smtp_ssl(self, mock_smtp_ssl):
        server = MagicMock()
        mock_smtp_ssl.return_value.__enter__.return_value = server

        alerts._send_email("Subject", "Body")

        server.send_message.assert_called_once()

    @patch.dict(
        "os.environ",
        {
            "SMTP_HOST": "smtp.example.com",
            "ALERT_EMAIL_TO": "ops@example.com",
        },
    )
    @patch("status_page.alerts.smtplib.SMTP", side_effect=Exception("Connection error"))
    def test_email_handles_smtp_failure(self, mock_smtp):
        alerts._send_email("Subject", "Body")

    @patch.dict(
        "os.environ",
        {
            "SMTP_HOST": "smtp.example.com",
            "ALERT_EMAIL_TO": "not-an-email,still-bad",
        },
    )
    @patch("status_page.alerts.smtplib.SMTP")
    def test_email_skips_when_no_valid_recipients(self, mock_smtp):
        alerts._send_email("Subject", "Body")
        mock_smtp.assert_not_called()

    @patch.object(alerts, "_send_email", return_value=True)
    def test_send_test_email_returns_true(self, mock_send):
        assert alerts.send_test_email() is True
        mock_send.assert_called_once()

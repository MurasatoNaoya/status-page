"""Tests for admin routes (auth, operations)."""

import re
import time
from unittest.mock import patch

import status_page.database as database


def _get_csrf_token(app_client):
    """Fetch the login page and extract the CSRF token."""
    resp = app_client.get("/admin/login")
    match = re.search(r'name="_csrf_token"\s+value="([^"]+)"', resp.data.decode())
    return match.group(1) if match else ""


class TestAdminAuth:
    def test_admin_requires_login(self, app_client):
        resp = app_client.get("/admin")
        assert resp.status_code == 302  # redirect to login

    def test_admin_login_page(self, app_client):
        resp = app_client.get("/admin/login")
        assert resp.status_code == 200

    def test_admin_login_wrong_creds(self, app_client):
        token = _get_csrf_token(app_client)
        resp = app_client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong", "_csrf_token": token},
        )
        assert resp.status_code == 200  # stays on login page

    def test_admin_login_success(self, app_client):
        token = _get_csrf_token(app_client)
        resp = app_client.post(
            "/admin/login",
            data={"username": "admin", "password": "testpass", "_csrf_token": token},
            follow_redirects=True,
        )
        assert resp.status_code == 200

    def test_admin_logout(self, app_client):
        # Login first
        token = _get_csrf_token(app_client)
        app_client.post(
            "/admin/login",
            data={"username": "admin", "password": "testpass", "_csrf_token": token},
        )
        token = _get_csrf_token(app_client)
        resp = app_client.post("/admin/logout", data={"_csrf_token": token})
        assert resp.status_code == 302
        # Should be redirected away from admin after logout
        resp = app_client.get("/admin")
        assert resp.status_code == 302

    def test_brute_force_rate_limit(self, app_client):
        """After 5 failed login attempts, return 429."""
        import status_page.app as app_module

        # Clear any prior state
        app_module._login_failures.clear()
        for _ in range(5):
            token = _get_csrf_token(app_client)
            app_client.post(
                "/admin/login",
                data={"username": "admin", "password": "wrong", "_csrf_token": token},
            )
        token = _get_csrf_token(app_client)
        resp = app_client.post(
            "/admin/login",
            data={"username": "admin", "password": "wrong", "_csrf_token": token},
        )
        assert resp.status_code == 429
        # Clean up
        app_module._login_failures.clear()


class TestAdminOperations:
    def test_admin_backfill_reports_partial_failure(self, app_client):
        import status_page.app as app_module

        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token

        feeds = [{"name": "FeedA"}, {"name": "FeedB"}]
        with (
            patch.object(app_module, "STATUS_FEEDS", feeds),
            patch.object(app_module, "poll_status_feed") as mock_poll,
        ):
            mock_poll.side_effect = [None, Exception("timeout")]
            resp = app_client.post(
                "/admin/backfill",
                data={"_csrf_token": csrf_token},
                follow_redirects=True,
            )
        assert resp.status_code == 200
        assert b"Backfill partially completed" in resp.data
        assert b"Failed feed(s): FeedB" in resp.data

    def test_admin_feed_coverage_returns_json(self, app_client):
        import status_page.app as app_module

        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token

        feeds = [
            {
                "name": "GitHub",
                "components": {"Actions": "GitHub Actions"},
                "covered_services": ["GitHub API"],
            }
        ]
        with (
            patch.object(app_module, "STATUS_FEEDS", feeds),
            patch.object(
                app_module,
                "get_feed_incident_stats",
                return_value={
                    "cnt": 2,
                    "oldest": "2026-01-01T00:00:00Z",
                    "newest": "2026-02-01T00:00:00Z",
                },
            ),
            patch.object(
                app_module,
                "get_feed_backfill_capability",
                return_value={
                    "feed_type": "statuspage",
                    "ingestion": "Statuspage API (/incidents.json, paginated)",
                    "known_limit_days": None,
                    "cap_type": "implementation_bounded",
                    "cap_summary": "Walks up to 10 incidents page(s).",
                },
            ),
        ):
            resp = app_client.get("/admin/feed-coverage")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["feed"] == "GitHub"
        assert data[0]["incident_count"] == 2
        assert data[0]["feed_type"] == "statuspage"
        assert data[0]["cap_type"] == "implementation_bounded"
        assert "GitHub Actions" in data[0]["services"]

    def test_prune_login_failures_removes_stale_and_caps(self, app_client):
        import status_page.app as app_module

        app_module._login_failures.clear()
        now = time.monotonic()
        app_module._login_failures["stale"] = [now - 10000]
        app_module._login_failures["fresh"] = [now]
        for i in range(10050):
            app_module._login_failures[f"ip-{i}"] = [now - (i % 10)]
        app_module._prune_login_failures()
        assert "stale" not in app_module._login_failures
        assert len(app_module._login_failures) <= 10000
        app_module._login_failures.clear()

    def test_declare_incident_calls_service_orchestrator(self, app_client):
        import status_page.app as app_module

        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token

        with patch.object(
            app_module, "declare_incident_with_alerts", return_value=42
        ) as mock_decl:
            resp = app_client.post(
                "/admin/declare",
                data={
                    "_csrf_token": csrf_token,
                    "title": "API outage",
                    "impact": "major",
                    "message": "Investigating",
                    "service": "GitHub API",
                },
            )
        assert resp.status_code == 302
        assert mock_decl.called

    def test_resolve_routes_through_service_orchestrator(self, app_client):
        import status_page.app as app_module

        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token

        inc_id = database.create_incident(
            title="Outage", impact="major", message="Investigating"
        )
        with patch.object(
            app_module, "resolve_incident_with_alerts", return_value=True
        ) as mock_resolve:
            resp = app_client.post(
                f"/admin/update/{inc_id}",
                data={
                    "_csrf_token": csrf_token,
                    "status": "resolved",
                    "message": "Fixed",
                },
            )
        assert resp.status_code == 302
        assert mock_resolve.called

    def test_admin_test_email_success(self, app_client):
        import status_page.app as app_module

        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token

        with patch.object(app_module, "send_test_email", return_value=True):
            resp = app_client.post(
                "/admin/test-email",
                data={"_csrf_token": csrf_token},
                follow_redirects=True,
            )

        assert resp.status_code == 200
        assert b"Test email sent." in resp.data

    def test_admin_test_email_failure(self, app_client):
        import status_page.app as app_module

        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token

        with patch.object(app_module, "send_test_email", return_value=False):
            resp = app_client.post(
                "/admin/test-email",
                data={"_csrf_token": csrf_token},
                follow_redirects=True,
            )

        assert resp.status_code == 200
        assert b"Test email failed." in resp.data

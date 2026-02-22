"""Tests for app.py — Flask routes and core logic."""

import re
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import status_page.database as database


def _get_csrf_token(app_client):
    """Fetch the login page and extract the CSRF token."""
    resp = app_client.get("/admin/login")
    match = re.search(r'name="_csrf_token"\s+value="([^"]+)"', resp.data.decode())
    return match.group(1) if match else ""


class TestIndexPage:
    def test_index_returns_200(self, app_client):
        resp = app_client.get("/")
        assert resp.status_code == 200

    def test_index_contains_page_title(self, app_client):
        resp = app_client.get("/")
        assert b"Status" in resp.data

    def test_index_contains_theme_toggle(self, app_client):
        resp = app_client.get("/")
        assert b"theme-toggle" in resp.data
        assert b"icon-sun" in resp.data
        assert b"icon-moon" in resp.data

    def test_index_contains_uptime_lines(self, app_client):
        resp = app_client.get("/")
        assert b"uptime-line" in resp.data
        assert b"90 days ago" in resp.data

    def test_index_contains_filter_pills(self, app_client):
        resp = app_client.get("/")
        assert b"filter-pill" in resp.data

    def test_index_dark_mode_css_variables(self, app_client):
        resp = app_client.get("/static/style.css")
        assert resp.status_code == 200
        css = resp.data.decode()
        assert '[data-theme="dark"]' in css
        assert "--green: #4080cf" in css

    def test_csp_uses_nonce_for_scripts(self, app_client):
        resp = app_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "script-src 'self' 'nonce-" in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp


class TestThemeToggleJS:
    """Verify the shared theme JS is loaded on pages."""

    def test_index_loads_theme_js(self, app_client):
        resp = app_client.get("/")
        html = resp.data.decode()
        assert "/static/theme.js" in html

    def test_theme_js_serves(self, app_client):
        resp = app_client.get("/static/theme.js")
        assert resp.status_code == 200
        js = resp.data.decode()
        assert "toggleTheme" in js
        assert "localStorage.setItem" in js
        assert "prefers-color-scheme" in js
        assert "removeChild(_themeStyle)" in js


class TestAPIRoutes:
    def test_health_endpoint(self, app_client):
        resp = app_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_create_incident_api_unauthenticated(self, app_client):
        resp = app_client.post(
            "/api/incidents",
            json={
                "title": "Test Incident",
                "impact": "partial",
                "message": "Testing",
            },
        )
        assert resp.status_code == 401

    def test_update_incident_api_unauthenticated(self, app_client):
        resp = app_client.patch(
            "/api/incidents/1", json={"status": "resolved", "message": "Fixed"}
        )
        assert resp.status_code == 401

    def test_create_incident_api(self, app_client):
        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token
        resp = app_client.post(
            "/api/incidents",
            json={
                "title": "Test Incident",
                "impact": "partial",
                "message": "Testing",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "id" in data

    def test_create_incident_api_missing_csrf(self, app_client):
        with app_client.session_transaction() as sess:
            sess["admin"] = True
        resp = app_client.post(
            "/api/incidents",
            json={"title": "Test Incident"},
        )
        assert resp.status_code == 403

    def test_update_incident_api(self, app_client):
        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token
        # Create first
        resp = app_client.post(
            "/api/incidents",
            json={"title": "Update Test", "message": "init"},
            headers={"X-CSRF-Token": csrf_token},
        )
        inc_id = resp.get_json()["id"]
        # Update
        resp = app_client.patch(
            f"/api/incidents/{inc_id}",
            json={"status": "resolved", "message": "Fixed"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200

    def test_update_incident_api_not_found(self, app_client):
        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token
        resp = app_client.patch(
            "/api/incidents/999999",
            json={"status": "resolved", "message": "Fixed"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 404


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
                    "ingestion": "Statuspage API (/incidents.json)",
                    "known_limit_days": None,
                    "cap_type": "implementation_bounded",
                    "cap_summary": "First incidents page only.",
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


class TestGMTFilter:
    def test_format_gmt_filter(self, app_client):
        from status_page.app import format_gmt

        result = format_gmt("2026-02-14T18:22:00Z")
        assert "Feb 14, 2026" in result
        assert "GMT" in result

    def test_format_gmt_empty(self, app_client):
        from status_page.app import format_gmt

        assert format_gmt("") == ""
        assert format_gmt(None) == ""

    def test_format_gmt_invalid(self, app_client):
        from status_page.app import format_gmt

        assert format_gmt("not-a-date") == "not-a-date"


class TestIncidentAutoDetection:
    """Test the auto-incident creation and resolution logic."""

    def test_auto_create_incident_after_threshold(self):
        from status_page.app import run_service_check

        svc = {"name": "TestAutoSvc", "type": "http", "url": "https://fail.example.com"}

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "status_page.app.run_check"
        ) as mock:
            mock.return_value = ("down", None, "Connection refused")
            for _ in range(3):
                run_service_check(svc)

        active = database.get_active_incident_for_service("TestAutoSvc")
        assert active is not None
        assert "Connection refused" in active["title"]

    def test_auto_resolve_on_recovery(self):
        from status_page.app import run_service_check

        svc = {"name": "RecoverSvc", "type": "http", "url": "https://example.com"}

        # Create an active incident
        database.create_incident(
            title="RecoverSvc Outage",
            impact="partial",
            message="down",
            service_name="RecoverSvc",
        )

        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "status_page.app.run_check"
        ) as mock:
            mock.return_value = ("up", 50.0, None)
            run_service_check(svc)

        active = database.get_active_incident_for_service("RecoverSvc")
        assert active is None  # Should be resolved

    def test_auto_resolve_on_recovery_uses_alert_orchestrator(self):
        from status_page.app import run_service_check

        svc = {"name": "RecoverSvc2", "type": "http", "url": "https://example.com"}
        inc_id = database.create_incident(
            title="RecoverSvc2 Outage",
            impact="partial",
            message="down",
            service_name="RecoverSvc2",
        )

        with (
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "status_page.app.run_check"
            ) as mock_check,
            __import__("unittest.mock", fromlist=["patch"]).patch(
                "status_page.app.resolve_incident_with_alerts"
            ) as mock_resolve,
        ):
            mock_check.return_value = ("up", 50.0, None)
            mock_resolve.return_value = True
            run_service_check(svc)

        mock_resolve.assert_called_once_with(
            incident_id=inc_id,
            message="Service has recovered. Automatically resolved.",
        )

    def test_skipped_check_does_not_record_or_incident(self):
        from status_page.app import run_service_check

        svc = {"name": "VpnSvc", "type": "dns", "hostname": "private.example"}
        with __import__("unittest.mock", fromlist=["patch"]).patch(
            "status_page.app.run_check"
        ) as mock:
            mock.return_value = (
                "skip",
                None,
                "Skipped: requires env ON_PRIVATE_NETWORK",
            )
            run_service_check(svc)

        latest = database.get_latest_status(["VpnSvc"])
        assert latest["VpnSvc"] is None
        active = database.get_active_incident_for_service("VpnSvc")
        assert active is None


class TestIncidentSeverity:
    """Test _incident_severity classification from incidents."""

    def test_no_incidents_returns_none(self):
        from status_page.app import _incident_severity

        assert _incident_severity([]) is None

    def test_major_incident(self):
        from status_page.app import _incident_severity

        assert _incident_severity([{"impact": "major"}]) == "major"

    def test_partial_incident(self):
        from status_page.app import _incident_severity

        assert _incident_severity([{"impact": "partial"}]) == "partial"


class TestDnsBarResolution:
    def test_dns_bar_auto_resolve_uses_alert_orchestrator(self, app_client):
        import status_page.app as app_module

        database.create_incident(
            title="DNS Incident",
            impact="partial",
            message="down",
            service_name="DNS Resolution",
        )

        with (
            patch.object(
                app_module,
                "DNS_BAR",
                {
                    "name": "DNS Resolution",
                    "targets": [{"hostname": "api.github.com", "label": "GitHub API"}],
                },
            ),
            patch.object(
                app_module,
                "check_dns_bar",
                return_value=[
                    {
                        "label": "GitHub API",
                        "hostname": "api.github.com",
                        "status": "up",
                        "ms": 1.0,
                        "error": None,
                    }
                ],
            ),
            patch.object(app_module, "resolve_incident_with_alerts") as mock_resolve,
        ):
            mock_resolve.return_value = True
            app_module.run_dns_bar_check()

        mock_resolve.assert_called_once()

    def test_minor_incident(self):
        from status_page.app import _incident_severity

        assert _incident_severity([{"impact": "minor"}]) == "degraded"


class TestFilterIncidentsForService:
    """Test _filter_incidents_for_service filtering and dedup."""

    def test_matches_exact_service_name(self):
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": "AKS", "title": "AKS down"},
            {"id": 2, "service_name": "Entra", "title": "Entra down"},
        ]
        result = _filter_incidents_for_service(incidents, "AKS")
        assert len(result) == 1
        assert result[0]["title"] == "AKS down"

    def test_includes_incidents_with_no_service(self):
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": None, "title": "Global outage"},
            {"id": 2, "service_name": "AKS", "title": "AKS down"},
        ]
        result = _filter_incidents_for_service(incidents, "Entra")
        assert len(result) == 1
        assert result[0]["title"] == "Global outage"

    def test_deduplicates_by_id(self):
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": "AKS", "title": "AKS down"},
            {"id": 1, "service_name": "AKS", "title": "AKS down"},
        ]
        result = _filter_incidents_for_service(incidents, "AKS")
        assert len(result) == 1

    def test_limits_to_3(self):
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": i, "service_name": "Svc", "title": f"Inc {i}"} for i in range(10)
        ]
        result = _filter_incidents_for_service(incidents, "Svc")
        assert len(result) == 3

    def test_does_not_match_different_service(self):
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": "AKS", "title": "AKS down"},
        ]
        result = _filter_incidents_for_service(incidents, "Entra")
        assert len(result) == 0


class TestFormatDayFilter:
    """Test the format_day template filter."""

    def test_formats_iso_date(self, app_client):
        from status_page.app import format_day

        assert format_day("2026-02-14") == "14 Feb 2026"

    def test_formats_iso_datetime(self, app_client):
        from status_page.app import format_day

        result = format_day("2026-01-03")
        assert result == "3 Jan 2026"

    def test_empty_returns_empty(self, app_client):
        from status_page.app import format_day

        assert format_day("") == ""
        assert format_day(None) == ""

    def test_invalid_returns_input(self, app_client):
        from status_page.app import format_day

        assert format_day("not-a-date") == "not-a-date"


class TestBuildServiceData:
    """Test build_service_data — badge logic, history threshold, downtime."""

    def _seed_checks(self, name, days, status="up", interval=60):
        """Insert check records spanning multiple days."""
        for d in range(days):
            for h in range(24):
                database.record_check(
                    name,
                    status,
                    10.0 if status == "up" else None,
                    None if status == "up" else "error",
                )

    def test_bars_gray_outside_coverage(self):
        """Days outside coverage should have uptime_pct = None (grey)."""
        from status_page.app import build_service_data

        database.record_check("NewSvc", "up", 10.0, None)
        latest = database.get_latest_status(["NewSvc"])
        # No coverage passed — all days except those with check data are grey
        data, _ = build_service_data([{"name": "NewSvc", "interval": 60}], latest)
        svc = data[0]
        # Today's bar should have data (from the check), the rest grey
        today_bar = svc["days"][-1]  # most recent day
        assert today_bar["uptime_pct"] == 100.0
        # Earlier days without coverage should be grey
        grey_days = [d for d in svc["days"][:-1] if d["uptime_pct"] is None]
        assert len(grey_days) == 89  # all 89 prior days are grey

    def test_no_data_status_when_no_checks(self):
        """Services with no check data at all should show 'no_data' status."""
        from status_page.app import build_service_data

        latest = database.get_latest_status(["NeverChecked"])
        data, _ = build_service_data([{"name": "NeverChecked", "interval": 60}], latest)
        assert data[0]["status"] == "no_data"

    def test_operational_status_with_check_data(self):
        """Services with check data showing 'up' should be operational."""
        from status_page.app import build_service_data

        database.record_check("FreshSvc", "up", 10.0, None)
        latest = database.get_latest_status(["FreshSvc"])
        data, _ = build_service_data([{"name": "FreshSvc", "interval": 60}], latest)
        assert data[0]["status"] == "operational"

    def test_bars_colored_with_enough_history(self):
        """Services with >= 3 days and incident coverage should show colored bars."""
        from status_page.app import build_service_data

        now = datetime.now(timezone.utc)
        # Insert checks with timestamps spread across 4 days
        from status_page.database import get_db

        for d in range(4):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        ("OldSvc", "up", 10.0, ts),
                    )
        latest = database.get_latest_status(["OldSvc"])
        data, _ = build_service_data([{"name": "OldSvc", "interval": 60}], latest)
        svc = data[0]
        # Today's bar should have a percentage (not None)
        today_bar = svc["days"][-1]
        assert today_bar["uptime_pct"] is not None

    def test_incidents_included_even_without_history(self):
        """Incidents must show even when service has < 3 days of data."""
        from status_page.app import build_service_data

        database.record_check("NewSvc", "up", 10.0, None)
        today = datetime.now(timezone.utc).date().isoformat()
        incidents_by_day = {
            today: [{"id": 99, "service_name": "NewSvc", "title": "Test incident"}]
        }
        latest = database.get_latest_status(["NewSvc"])
        data, _ = build_service_data(
            [{"name": "NewSvc", "interval": 60}], latest, incidents_by_day
        )
        today_bar = data[0]["days"][-1]
        assert len(today_bar["incidents"]) == 1
        assert today_bar["incidents"][0]["title"] == "Test incident"

    def test_coverage_turns_no_data_days_green(self):
        """Days within feed coverage but without check data should be green."""
        from status_page.app import build_service_data

        latest = database.get_latest_status(["CovSvc"])
        # Coverage starts 10 days ago — those 10 days should be green, rest grey
        from datetime import timedelta

        coverage_start = (
            (datetime.now(timezone.utc) - timedelta(days=10)).date().isoformat()
        )
        data, _ = build_service_data(
            [{"name": "CovSvc", "interval": 60}],
            latest,
            coverage={"CovSvc": coverage_start},
        )
        svc = data[0]
        green = [d for d in svc["days"] if d["uptime_pct"] is not None]
        grey = [d for d in svc["days"] if d["uptime_pct"] is None]
        assert len(green) == 11  # 10 days ago through today = 11 days
        assert len(grey) == 79  # rest are grey (no coverage)

    def test_badge_operational_when_all_up(self):
        from status_page.app import build_service_data
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        # Seed 4 days of check data to pass has_history threshold
        for d in range(4):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        ("GoodSvc", "up", 10.0, ts),
                    )
        latest = database.get_latest_status(["GoodSvc"])
        data, all_ok = build_service_data([{"name": "GoodSvc", "interval": 60}], latest)
        assert data[0]["status"] == "operational"
        assert all_ok is True

    def test_badge_degraded_when_latest_down_no_incident(self):
        from status_page.app import build_service_data

        for _ in range(25):
            database.record_check("DownSvc", "up", 10.0, None)
        database.record_check("DownSvc", "down", None, "Connection refused")
        latest = database.get_latest_status(["DownSvc"])
        data, all_ok = build_service_data([{"name": "DownSvc", "interval": 60}], latest)
        # Latest check is down but no active incident yet — badge is degraded
        assert data[0]["status"] == "degraded"
        assert all_ok is False

    def test_badge_degraded_when_latest_down_despite_high_uptime(self):
        """Even with >95% uptime today, badge reflects current status (latest check)."""
        from status_page.app import build_service_data

        # 4 days of data to pass has_history check
        for d in range(4):
            for _ in range(24):
                database.record_check("DegSvc", "up", 10.0, None)
        # Add 1 failure today (out of 24+ checks = >95% uptime)
        database.record_check("DegSvc", "down", None, "timeout")
        latest = database.get_latest_status(["DegSvc"])
        # Latest check is "down" with no active incident — badge is degraded
        data, _ = build_service_data([{"name": "DegSvc", "interval": 60}], latest)
        assert data[0]["status"] == "degraded"

    def test_uptime_pct_suppressed_with_insufficient_history(self):
        from status_page.app import build_service_data

        # Only 1 day of data
        for _ in range(25):
            database.record_check("ShortSvc", "up", 10.0, None)
        latest = database.get_latest_status(["ShortSvc"])
        data, _ = build_service_data([{"name": "ShortSvc", "interval": 60}], latest)
        assert data[0]["uptime_pct"] is None

    def test_downtime_calculation(self):
        """Downtime should be down_count * interval_sec."""
        from status_page.app import build_service_data

        for d in range(4):
            for _ in range(20):
                database.record_check("DtSvc", "up", 10.0, None)
        # Add 5 failures
        for _ in range(5):
            database.record_check("DtSvc", "down", None, "err")
        latest = database.get_latest_status(["DtSvc"])
        data, _ = build_service_data([{"name": "DtSvc", "interval": 300}], latest)
        today_bar = data[0]["days"][-1]
        # 5 failures * 300s = 1500s = 0 hrs, 25 mins
        assert today_bar["downtime_mins"] == 25
        assert today_bar["downtime_hours"] == 0

    def test_severity_from_incident_impact_major(self):
        """Major incidents should produce 'major' severity (red bar)."""
        from status_page.app import build_service_data
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        for d in range(4):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        ("CritSvc", "up", 10.0, ts),
                    )
        today = now.date().isoformat()
        incidents_by_day = {
            today: [
                {
                    "id": 42,
                    "service_name": "CritSvc",
                    "title": "Major outage",
                    "impact": "major",
                }
            ]
        }
        latest = database.get_latest_status(["CritSvc"])
        data, _ = build_service_data(
            [{"name": "CritSvc", "interval": 60}], latest, incidents_by_day
        )
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] == "major"
        assert today_bar["down_count"] == 0

    def test_severity_from_incident_impact_partial(self):
        """Partial incidents should produce 'partial' severity (orange bar)."""
        from status_page.app import build_service_data
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        for d in range(4):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        ("MajSvc", "up", 10.0, ts),
                    )
        today = now.date().isoformat()
        incidents_by_day = {
            today: [
                {
                    "id": 43,
                    "service_name": "MajSvc",
                    "title": "Partial outage",
                    "impact": "partial",
                }
            ]
        }
        latest = database.get_latest_status(["MajSvc"])
        data, _ = build_service_data(
            [{"name": "MajSvc", "interval": 60}], latest, incidents_by_day
        )
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] == "partial"

    def test_severity_from_incident_impact_minor(self):
        """Minor incidents should produce 'degraded' severity (yellow bar)."""
        from status_page.app import build_service_data
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        for d in range(4):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        ("MinSvc", "up", 10.0, ts),
                    )
        today = now.date().isoformat()
        incidents_by_day = {
            today: [
                {
                    "id": 44,
                    "service_name": "MinSvc",
                    "title": "Minor issue",
                    "impact": "minor",
                }
            ]
        }
        latest = database.get_latest_status(["MinSvc"])
        data, _ = build_service_data(
            [{"name": "MinSvc", "interval": 60}], latest, incidents_by_day
        )
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] == "degraded"

    def test_severity_none_when_no_incidents_and_no_downtime(self):
        """Days with 100% uptime and no incidents should have severity=None."""
        from status_page.app import build_service_data
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        for d in range(4):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        ("CleanSvc", "up", 10.0, ts),
                    )
        latest = database.get_latest_status(["CleanSvc"])
        data, _ = build_service_data([{"name": "CleanSvc", "interval": 60}], latest)
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] is None
        assert today_bar["down_count"] == 0

    def test_severity_preserved_for_incidents_without_enough_history(self):
        """Incidents should set severity even when service has < 3 days of history."""
        from status_page.app import build_service_data

        database.record_check("NoHistSvc", "up", 10.0, None)
        today = datetime.now(timezone.utc).date().isoformat()
        incidents_by_day = {
            today: [{"id": 77, "service_name": "NoHistSvc", "title": "Feed incident"}]
        }
        latest = database.get_latest_status(["NoHistSvc"])
        data, _ = build_service_data(
            [{"name": "NoHistSvc", "interval": 60}], latest, incidents_by_day
        )
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] == "degraded"
        assert today_bar["incidents"][0]["title"] == "Feed incident"


class TestBarCoverage:
    """Test bar color logic per data source — no synthetic green states.

    These tests verify the exact bugs we hit:
    - Services with no check data must stay GREY (no false green)
    - Feed coverage does not imply success data
    - Incidents with service_name=None must NOT leak to unrelated services
    - Feed service mapping still behaves correctly
    """

    def _seed_days(self, name, days=5):
        """Insert check records spanning multiple days with timestamps."""
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        for d in range(days):
            ts = (now - timedelta(days=d)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with get_db() as db:
                for _ in range(5):
                    db.execute(
                        "INSERT INTO check_results (service_name, status, response_time_ms, checked_at) VALUES (?, ?, ?, ?)",
                        (name, "up", 10.0, ts),
                    )

    # --- Coverage: green vs grey ---

    def test_feed_covered_service_without_checks_stays_grey(self):
        """Feed coverage alone must not imply operational data."""
        from status_page.app import build_service_data

        latest = database.get_latest_status(["FeedSvc"])
        data, _ = build_service_data([{"name": "FeedSvc", "interval": 60}], latest)
        today_bar = data[0]["days"][-1]
        assert today_bar["uptime_pct"] is None

    def test_no_feed_no_incidents_shows_grey(self):
        """Service with NO feed and no incidents should show GREY bars.
        This is the correct 'no data' state."""
        from status_page.app import build_service_data

        latest = database.get_latest_status(["NoFeedSvc"])
        data, _ = build_service_data([{"name": "NoFeedSvc", "interval": 60}], latest)
        today_bar = data[0]["days"][-1]
        assert today_bar["uptime_pct"] is None, "Should be grey (no checks)"

    def test_check_history_determines_green_vs_grey_boundary(self):
        """Only days with check history should be green."""
        from status_page.app import build_service_data

        self._seed_days("BoundarySvc", 30)
        latest = database.get_latest_status(["BoundarySvc"])
        data, _ = build_service_data([{"name": "BoundarySvc", "interval": 60}], latest)
        days = data[0]["days"]
        for d in days[-30:]:
            assert d["uptime_pct"] is not None, (
                f"Day {d['date']} should be green (check data exists)"
            )
        assert days[-31]["uptime_pct"] is None, (
            "Day before check history should be grey"
        )

    # --- Incident isolation: no cross-service leaks ---

    def test_null_service_incidents_only_show_on_matching_service(self):
        """Incidents with service_name=None should match ALL services.
        Bug: Docker Hub incidents without mapped components leaked to Azure bars."""
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": "Docker Hub", "title": "Docker outage"},
            {"id": 2, "service_name": None, "title": "Unknown outage"},
        ]
        # Docker Hub should see both
        docker_result = _filter_incidents_for_service(incidents, "Docker Hub")
        assert len(docker_result) == 2

        # Azure should only see the NULL one
        azure_result = _filter_incidents_for_service(incidents, "Azure AKS")
        assert len(azure_result) == 1
        assert azure_result[0]["service_name"] is None

    def test_docker_incident_does_not_appear_on_azure_bar(self):
        """Docker Hub incidents must NOT appear on Azure service bars."""
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": "Docker Hub", "title": "Docker registry outage"},
        ]
        result = _filter_incidents_for_service(
            incidents, "Azure Kubernetes Service (AKS)"
        )
        assert len(result) == 0

    def test_github_incident_does_not_appear_on_docker_bar(self):
        """GitHub incidents must NOT appear on Docker Hub bar."""
        from status_page.app import _filter_incidents_for_service

        incidents = [
            {"id": 1, "service_name": "GitHub Actions", "title": "Actions degraded"},
        ]
        result = _filter_incidents_for_service(incidents, "Docker Hub")
        assert len(result) == 0

    # --- Severity from real feed incidents ---

    def test_bar_colored_from_feed_incident_not_checks(self):
        """Bar severity should come from feed incidents, NOT from check failures."""
        from status_page.app import build_service_data

        self._seed_days("IncSvc", 5)
        today = datetime.now(timezone.utc).date().isoformat()
        incidents_by_day = {
            today: [
                {
                    "id": 10,
                    "service_name": "IncSvc",
                    "title": "Feed outage",
                    "impact": "partial",
                }
            ]
        }
        latest = database.get_latest_status(["IncSvc"])
        data, _ = build_service_data(
            [{"name": "IncSvc", "interval": 60}], latest, incidents_by_day
        )
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] == "partial"  # partial → partial (orange)
        assert today_bar["down_count"] == 0  # checks were all UP

    def test_bar_green_when_checks_pass_and_no_incidents(self):
        """Bar should be green when checks pass and no incidents exist."""
        from status_page.app import build_service_data

        self._seed_days("GreenSvc", 5)
        latest = database.get_latest_status(["GreenSvc"])
        data, _ = build_service_data([{"name": "GreenSvc", "interval": 60}], latest)
        today_bar = data[0]["days"][-1]
        assert today_bar["severity"] is None  # No severity = green
        assert today_bar["uptime_pct"] == 100.0

    # --- Per-source coverage integration ---

    def test_statuspage_feed_components_provide_coverage(self):
        """Services in a Statuspage feed's components map should have coverage."""
        feed = {"name": "GitHub", "components": {"Actions": "GitHub Actions"}}
        feed_services = set()
        feed_services.update(feed.get("components", {}).values())
        feed_services.update(feed.get("covered_services", []))
        assert "GitHub Actions" in feed_services

    def test_statusio_feed_components_provide_coverage(self):
        """Services in a Status.io feed's components map should have coverage."""
        feed = {
            "name": "Docker",
            "type": "statusio",
            "components": {"Docker Hub Registry": "Docker Hub"},
        }
        feed_services = set()
        feed_services.update(feed.get("components", {}).values())
        feed_services.update(feed.get("covered_services", []))
        assert "Docker Hub" in feed_services

    def test_azure_rss_covered_services_provide_coverage(self):
        """Azure RSS feed uses covered_services instead of components map.
        Bug: Azure bars were all grey because RSS has no components map."""
        feed = {
            "name": "Azure",
            "type": "azure_rss",
            "covered_services": [
                "Azure Kubernetes Service (AKS)",
                "Azure AD / Entra ID",
            ],
        }
        feed_services = set()
        feed_services.update(feed.get("components", {}).values())
        feed_services.update(feed.get("covered_services", []))
        assert "Azure Kubernetes Service (AKS)" in feed_services
        assert "Azure AD / Entra ID" in feed_services

    def test_feed_without_components_or_covered_provides_no_coverage(self):
        """A feed with neither components nor covered_services adds nothing."""
        feed = {"name": "Empty", "type": "azure_rss"}
        feed_services = set()
        feed_services.update(feed.get("components", {}).values())
        feed_services.update(feed.get("covered_services", []))
        assert len(feed_services) == 0


class TestTooltipLabels:
    """Test that tooltip severity labels are correct in the template source.

    These check the Jinja2 template directly since labels only render when
    matching data exists (no way to guarantee all severity levels at runtime).
    """

    def test_partial_severity_tooltip_text(self):
        """Orange bars should show 'Partial outage', not 'Outage reported'.
        Tooltips are rendered by JS using severity labels."""
        with open("status_page/templates/index.html") as f:
            template = f.read()
        assert "Partial outage" in template

    def test_tooltip_severity_labels_in_template(self):
        """Verify all three severity labels exist in the JS tooltip renderer."""
        with open("status_page/templates/index.html") as f:
            template = f.read()
        assert "Major outage" in template
        assert "Partial outage" in template
        assert "Degraded performance" in template

    def test_no_bare_outage_reported_label(self):
        """'Outage reported' without qualifier (Partial/Major) should not exist.
        Bug: template had just 'Outage reported' for partial severity."""
        with open("status_page/templates/index.html") as f:
            template = f.read()
        import re

        # Find all 'outage reported' that aren't prefixed by Major or Partial
        matches = re.findall(r"(?<!Major )(?<!Partial )Outage reported", template)
        assert len(matches) == 0, (
            f"Found bare 'Outage reported' without qualifier: {matches}"
        )


class TestPollStatusFeedServiceFiltering:
    """Test that poll_status_feed correctly skips or creates incidents based on service mapping."""

    def test_feed_with_components_skips_unmapped_incidents(self):
        """When a feed has a component map, incidents with no matching services should be skipped.
        Bug: Docker incidents for Docker Desktop/Billing created with service_name=NULL
        and leaked to all service bars."""
        from status_page.feed_importer import poll_status_feed

        feed_results = [
            {
                "external_id": "unmapped-123",
                "title": "Docker Desktop issue",
                "status": "resolved",
                "services": None,  # No services matched the component map
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

        # Should NOT create an incident (service not mapped)
        inc = database.get_incident_by_external_id("unmapped-123")
        assert inc is None, (
            "Should skip incidents with no mapped services when feed has components"
        )

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
        """Feeds without component maps (like Azure RSS keyword matching)
        should still create incidents with service_name=NULL when no services matched."""
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
        # No components key = no component map
        feed_config = {"name": "Azure"}
        with patch("status_page.feed_importer.poll_feed", return_value=feed_results):
            poll_status_feed(feed_config)

        inc = database.get_incident_by_external_id("azure-rss-999")
        assert inc is not None
        assert inc["service_name"] is None


class TestParseAzureHistory:
    """Test Azure history page scraping."""

    SAMPLE_HTML = """
    <div class="row incident-history-header">
      <div class="col-sm-1 incident-history-day">Feb 2</div>
      <div class="col-sm-11 incident-history-item">
        <div class="col-md-8 incident-history-title">PIR – Virtual Machines and AKS outage</div>
        <div class="col-md-3 incident-history-tracking-id">Tracking ID: TEST-123</div>
      </div>
      <div class="collapse incident-history-collapse">
        <div class="card-body">
          Between 19:46 UTC on 02 February 2026 and 06:05 UTC on 03 February 2026,
          customers using Virtual Machines and AKS in all regions may have experienced failures.
        </div>
      </div>
    </div>
    <div class="row incident-history-header">
      <div class="col-sm-1 incident-history-day">Feb 7</div>
      <div class="col-sm-11 incident-history-item">
        <div class="col-md-8 incident-history-title">PIR – Power event West US</div>
        <div class="col-md-3 incident-history-tracking-id">Tracking ID: WUS-456</div>
      </div>
      <div class="collapse incident-history-collapse">
        <div class="card-body">
          Between 07:58 UTC on 07 February 2026 power event impacted services in West US region.
        </div>
      </div>
    </div>
    """

    def test_parses_incident_title_and_id(self):
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(self.SAMPLE_HTML, exclude_regions=[])
        assert len(incidents) == 2
        assert incidents[0]["title"] == "PIR – Virtual Machines and AKS outage"
        assert incidents[0]["external_id"] == "azure-pir-TEST-123"

    def test_matches_azure_services(self):
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(self.SAMPLE_HTML, exclude_regions=[])
        vm_inc = incidents[0]
        # Should match AKS and Virtual Machines
        assert vm_inc["services"] is not None
        assert any("AKS" in s or "Kubernetes" in s for s in vm_inc["services"])

    def test_region_filtering_excludes_west_us(self):
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(self.SAMPLE_HTML, exclude_regions=["West US"])
        # First incident mentions "all regions" so it passes
        # Second incident is West US only so it's excluded
        assert len(incidents) == 1
        assert "Virtual Machines" in incidents[0]["title"]

    def test_global_incident_not_excluded(self):
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(
            self.SAMPLE_HTML, exclude_regions=["West US", "East US"]
        )
        # "all regions" incident should still pass even though regions are excluded
        assert len(incidents) == 1
        # Summary is in the last update (used as initial message by poll_status_feed)
        assert "all regions" in incidents[0]["updates"][-1]["message"].lower()

    def test_extracts_start_and_end_times(self):
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(self.SAMPLE_HTML, exclude_regions=[])
        inc = incidents[0]
        # Start: 19:46 UTC on 02 February 2026
        assert inc["created_at"] == "2026-02-02T19:46:00Z"
        # End: 06:05 UTC on 03 February 2026
        assert inc["resolved_at"] == "2026-02-03T06:05:00Z"

    def test_strips_video_preamble(self):
        html = """
        <div class="row incident-history-header">
          <div class="col-sm-11 incident-history-item">
            <div class="col-md-8 incident-history-title">PIR – Entra PIM failures</div>
            <div class="col-md-3 incident-history-tracking-id">Tracking ID: VID-789</div>
          </div>
          <div class="collapse incident-history-collapse">
            <div class="card-body">
              Watch our 'Azure Incident Retrospective' video about this incident: https://aka.ms/air/VID-789
              What happened? Between 08:05 UTC and 18:30 UTC on 22 December 2025,
              Entra PIM experienced API failures.
            </div>
          </div>
        </div>
        """
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(html, exclude_regions=[])
        assert len(incidents) == 1
        summary = incidents[0]["updates"][-1]["message"]
        assert "Watch our" not in summary
        assert "Entra PIM" in summary

    def test_all_history_incidents_are_resolved(self):
        from status_page.status_feeds import _parse_azure_history

        incidents = _parse_azure_history(self.SAMPLE_HTML, exclude_regions=[])
        for inc in incidents:
            assert inc["status"] == "resolved"


class TestPollStatusioAPI:
    """Test the Status.io API poller (used by Docker Hub)."""

    def _make_statusio_response(self, incidents=None, components=None):
        """Build a fake Status.io API response."""
        return {
            "result": {
                "status_overall": {"status": "Operational", "status_code": 100},
                "status": components or [],
                "incidents": incidents or [],
                "maintenance": {"active": [], "upcoming": []},
            }
        }

    def test_returns_incidents_with_correct_format(self):
        from status_page.status_feeds import poll_statusio_api

        feed_config = {
            "name": "Docker",
            "url": "https://api.status.io/1.0/status/533c6539221ae15e3f000031",
            "components": {"Docker Hub Registry": "Docker Hub"},
            "interval": 300,
        }
        fake_resp = self._make_statusio_response(
            incidents=[
                {
                    "_id": "abc123",
                    "name": "Registry outage",
                    "datetime_open": "2026-02-10T10:00:00.000Z",
                    "datetime_close": "",
                    "messages": [
                        {
                            "details": "Investigating the issue.",
                            "status": 500,
                            "datetime": "2026-02-10T10:00:00.000Z",
                        },
                    ],
                    "components_affected": [
                        {"name": "Docker Hub Registry", "_id": "comp1"}
                    ],
                }
            ]
        )
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = fake_resp
            mock_get.return_value.raise_for_status = lambda: None
            results = poll_statusio_api(feed_config)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 1
        inc = incidents[0]
        assert inc["title"] == "Registry outage"
        assert inc["external_id"] == "abc123"
        assert inc["services"] == ["Docker Hub"]
        assert inc["impact"] == "major"  # status 500 = major
        assert inc["status"] == "investigating"  # not closed
        assert len(inc["updates"]) == 1

    def test_resolved_incident_has_resolved_status(self):
        from status_page.status_feeds import poll_statusio_api

        feed_config = {
            "name": "Docker",
            "url": "https://api.status.io/1.0/status/fake",
            "components": {"Docker Hub Registry": "Docker Hub"},
        }
        fake_resp = self._make_statusio_response(
            incidents=[
                {
                    "_id": "def456",
                    "name": "Brief outage",
                    "datetime_open": "2026-02-10T10:00:00.000Z",
                    "datetime_close": "2026-02-10T11:00:00.000Z",
                    "messages": [
                        {
                            "details": "Fixed.",
                            "status": 100,
                            "datetime": "2026-02-10T11:00:00.000Z",
                        },
                        {
                            "details": "Investigating.",
                            "status": 500,
                            "datetime": "2026-02-10T10:00:00.000Z",
                        },
                    ],
                    "components_affected": [
                        {"name": "Docker Hub Registry", "_id": "comp1"}
                    ],
                }
            ]
        )
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = fake_resp
            mock_get.return_value.raise_for_status = lambda: None
            results = poll_statusio_api(feed_config)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 1
        assert incidents[0]["status"] == "resolved"
        assert incidents[0]["resolved_at"] == "2026-02-10T11:00:00.000Z"

    def test_maps_status_codes_to_impact(self):
        from status_page.status_feeds import _statusio_code_to_impact

        assert _statusio_code_to_impact(100) == "none"
        assert _statusio_code_to_impact(300) == "minor"
        assert _statusio_code_to_impact(400) == "partial"
        assert _statusio_code_to_impact(500) == "major"
        assert _statusio_code_to_impact(600) == "major"

    def test_returns_component_status(self):
        from status_page.status_feeds import poll_statusio_api

        feed_config = {
            "name": "Docker",
            "url": "https://api.status.io/1.0/status/fake",
            "components": {"Docker Hub Registry": "Docker Hub"},
        }
        fake_resp = self._make_statusio_response(
            components=[
                {
                    "id": "comp1",
                    "name": "Docker Hub Registry",
                    "status": "Degraded Performance",
                    "status_code": 300,
                    "containers": [],
                    "updated": "2026-02-10T10:00:00.000Z",
                }
            ]
        )
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = fake_resp
            mock_get.return_value.raise_for_status = lambda: None
            results = poll_statusio_api(feed_config)

        comp_items = [r for r in results if r.get("type") == "component_status"]
        assert len(comp_items) == 1
        assert comp_items[0]["service_name"] == "Docker Hub"
        assert comp_items[0]["status"] == "degraded_performance"

    def test_unmapped_components_ignored(self):
        from status_page.status_feeds import poll_statusio_api

        feed_config = {
            "name": "Docker",
            "url": "https://api.status.io/1.0/status/fake",
            "components": {"Docker Hub Registry": "Docker Hub"},
        }
        fake_resp = self._make_statusio_response(
            incidents=[
                {
                    "_id": "xyz789",
                    "name": "Desktop issue",
                    "datetime_open": "2026-02-10T10:00:00.000Z",
                    "datetime_close": "",
                    "messages": [
                        {
                            "details": "Issue.",
                            "status": 400,
                            "datetime": "2026-02-10T10:00:00.000Z",
                        }
                    ],
                    "components_affected": [{"name": "Docker Desktop", "_id": "comp2"}],
                }
            ]
        )
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.return_value.status_code = 200
            mock_get.return_value.json.return_value = fake_resp
            mock_get.return_value.raise_for_status = lambda: None
            results = poll_statusio_api(feed_config)

        incidents = [r for r in results if r.get("type") != "component_status"]
        # Incident should still be returned but with no matched services
        assert len(incidents) == 1
        assert incidents[0]["services"] is None or incidents[0]["services"] == []

    def test_api_failure_returns_empty(self):
        from status_page.status_feeds import poll_statusio_api

        feed_config = {
            "name": "Docker",
            "url": "https://api.status.io/1.0/status/fake",
            "components": {},
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.side_effect = Exception("Connection refused")
            results = poll_statusio_api(feed_config)
        assert results == []


class TestScrapeStatusioHistory:
    """Test scraping Status.io history page for historical incidents."""

    SAMPLE_HTML = """
    <div class="row incident" id="statusio_incident_abc123def">
      <div class="col-md-12"><div class="panel panel-default make_round">
        <div class="panel-heading make_round" style="background:#ffb463;">
          <div class="panel-title"><h5 class="white">
            <a href="/pages/incident/fake/abc123def" style="color:#FFF;">Registry outage title</a>
            <span class="pull-right status_description">Partial Service Disruption</span>
          </h5></div>
        </div>
        <div class="panel-body">
          <div class="row"><div class="col-xs-12 col-md-2">
            <p class="pull-left text event_inner_title">Components  </p></div>
            <div class="col-xs-12 col-md-10">
              <p class="incident_section event_inner_text">Docker Hub Registry, Docker Authentication</p>
          </div></div>
          <div class="row" style="margin-top:20px;">
            <div class="col-md-4"><strong class="incident_time">Feb 10, 2026 14:52 PST<br>February 10, 2026 22:52 UTC</strong></div>
            <div class="col-md-8">
              <div class="incident_update_status"><strong><span></span>RESOLVED</strong></div>
              <span class="incident_message_details" id="statusio_incident_message_msg1">Issue resolved.</span>
            </div>
          </div>
          <div class="row" style="margin-top:20px;">
            <div class="col-md-4"><strong class="incident_time">Feb 10, 2026 13:00 PST<br>February 10, 2026 21:00 UTC</strong></div>
            <div class="col-md-8">
              <div class="incident_update_status"><strong><span></span>INVESTIGATING</strong></div>
              <span class="incident_message_details" id="statusio_incident_message_msg2">Looking into it.</span>
            </div>
          </div>
        </div>
      </div></div>
    </div>
    """

    def test_parses_incident_title_and_id(self):
        from status_page.status_feeds import _parse_statusio_history

        incidents = _parse_statusio_history(
            self.SAMPLE_HTML,
            {
                "Docker Hub Registry": "Docker Hub",
                "Docker Authentication": "Docker Hub",
            },
        )
        assert len(incidents) == 1
        assert incidents[0]["title"] == "Registry outage title"
        assert incidents[0]["external_id"] == "abc123def"

    def test_maps_components_to_services(self):
        from status_page.status_feeds import _parse_statusio_history

        incidents = _parse_statusio_history(
            self.SAMPLE_HTML,
            {
                "Docker Hub Registry": "Docker Hub",
                "Docker Authentication": "Docker Hub",
            },
        )
        assert "Docker Hub" in incidents[0]["services"]

    def test_parses_severity(self):
        from status_page.status_feeds import _parse_statusio_history

        incidents = _parse_statusio_history(
            self.SAMPLE_HTML,
            {
                "Docker Hub Registry": "Docker Hub",
            },
        )
        assert (
            incidents[0]["impact"] == "partial"
        )  # Partial Service Disruption = partial

    def test_parses_updates_newest_first(self):
        from status_page.status_feeds import _parse_statusio_history

        incidents = _parse_statusio_history(
            self.SAMPLE_HTML,
            {
                "Docker Hub Registry": "Docker Hub",
            },
        )
        updates = incidents[0]["updates"]
        assert len(updates) == 2
        # Newest-first order (matching Statuspage API convention)
        assert updates[0]["status"] == "resolved"
        assert updates[1]["status"] == "investigating"

    def test_resolved_incident_has_resolved_status(self):
        from status_page.status_feeds import _parse_statusio_history

        incidents = _parse_statusio_history(
            self.SAMPLE_HTML,
            {
                "Docker Hub Registry": "Docker Hub",
            },
        )
        assert incidents[0]["status"] == "resolved"


class TestPollStatusFeed:
    """Test poll_status_feed per-service incident creation."""

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

        # Should have 3 separate incidents
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

        # Pre-create incident
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

        # Should not create duplicate
        incidents = database.get_recent_incidents(limit=50)
        matching = [i for i in incidents if i["external_id"] == "existing-123"]
        assert len(matching) == 1

    def test_updates_status_to_resolved(self):
        from status_page.feed_importer import poll_status_feed

        # poll_status_feed creates external_id as "resolve-me:Svc" for per-service copies
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
        # No incidents should be created
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

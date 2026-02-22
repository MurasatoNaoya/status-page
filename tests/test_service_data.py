"""Tests for service data building, badge logic, bar coverage, and auto-detection."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import status_page.database as database


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
        from status_page.database import get_db

        now = datetime.now(timezone.utc)
        # Insert checks with timestamps spread across 4 days
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
        assert today_bar["severity"] == "partial"  # partial -> partial (orange)
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

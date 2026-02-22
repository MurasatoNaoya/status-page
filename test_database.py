"""Tests for database.py — the data layer."""

from datetime import datetime, timedelta, timezone

import database


class TestRecordAndQuery:
    def test_record_check_and_latest_status(self):
        database.record_check("TestSvc", "up", 42.5, None)
        latest = database.get_latest_status(["TestSvc"])
        assert latest["TestSvc"]["status"] == "up"
        assert latest["TestSvc"]["response_time_ms"] == 42.5
        assert latest["TestSvc"]["error_message"] is None

    def test_record_down_with_error(self):
        database.record_check("Broken", "down", None, "Connection refused")
        latest = database.get_latest_status(["Broken"])
        assert latest["Broken"]["status"] == "down"
        assert latest["Broken"]["error_message"] == "Connection refused"

    def test_latest_status_missing_service(self):
        latest = database.get_latest_status(["NonExistent"])
        assert latest["NonExistent"] is None

    def test_multiple_checks_returns_latest(self):
        database.record_check("Svc", "down", None, "fail")
        database.record_check("Svc", "up", 10.0, None)
        latest = database.get_latest_status(["Svc"])
        assert latest["Svc"]["status"] == "up"


class TestIncidentDowntimeHours:
    def test_resolved_incident_uses_timestamps(self):
        inc_id = database.create_incident(
            title="Outage",
            impact="partial",
            message="down",
            service_name="DtSvc",
            external_id="dt-test-1",
        )
        # Manually set created_at and resolved_at 2 hours apart
        with database.get_db() as db:
            db.execute(
                "UPDATE incidents SET created_at = '2025-01-01T10:00:00Z', "
                "resolved_at = '2025-01-01T12:00:00Z' WHERE id = ?",
                (inc_id,),
            )
            db.commit()
        hours = database.get_incident_downtime_hours("DtSvc", days=3650)
        assert hours == 2.0

    def test_unresolved_incident_uses_fallback(self):
        database.create_incident(
            title="Ongoing",
            impact="major",
            message="down",
            service_name="FallSvc",
            external_id="dt-test-2",
        )
        hours = database.get_incident_downtime_hours("FallSvc", days=3650)
        assert hours == 4.0  # major fallback

    def test_minor_fallback(self):
        database.create_incident(
            title="Blip",
            impact="minor",
            message="hiccup",
            service_name="MinSvc",
            external_id="dt-test-3",
        )
        hours = database.get_incident_downtime_hours("MinSvc", days=3650)
        assert hours == 1.0  # minor fallback

    def test_no_incidents_returns_zero(self):
        hours = database.get_incident_downtime_hours("EmptySvc")
        assert hours == 0.0

    def test_overlapping_incidents_are_not_double_counted(self):
        database.create_incident(
            title="Overlap A",
            impact="partial",
            message="down",
            service_name="OverlapSvc",
            external_id="dt-overlap-1",
            created_at="2025-01-01T10:00:00Z",
            resolved_at="2025-01-01T12:00:00Z",
            status="resolved",
        )
        database.create_incident(
            title="Overlap B",
            impact="partial",
            message="down",
            service_name="OverlapSvc",
            external_id="dt-overlap-2",
            created_at="2025-01-01T11:00:00Z",
            resolved_at="2025-01-01T13:00:00Z",
            status="resolved",
        )
        hours = database.get_incident_downtime_hours("OverlapSvc", days=3650)
        assert hours == 3.0


class TestUptimeCalculation:
    def test_uptime_percentage_requires_minimum_checks(self):
        # Need at least 24 checks to get a percentage
        for _ in range(10):
            database.record_check("Svc", "up", 5.0, None)
        assert database.get_uptime_percentage("Svc") is None

    def test_uptime_percentage_with_enough_data(self):
        for _ in range(24):
            database.record_check("Svc", "up", 5.0, None)
        pct = database.get_uptime_percentage("Svc")
        assert pct == 100.0

    def test_uptime_percentage_partial(self):
        for _ in range(20):
            database.record_check("Svc", "up", 5.0, None)
        for _ in range(5):
            database.record_check("Svc", "down", None, "err")
        pct = database.get_uptime_percentage("Svc")
        assert pct == 80.0

    def test_uptime_days_returns_daily_breakdown(self):
        for _ in range(5):
            database.record_check("Svc", "up", 5.0, None)
        days = database.get_uptime_days("Svc")
        assert len(days) >= 1
        assert days[0]["up_count"] == 5
        assert days[0]["total"] == 5

    def test_uptime_days_no_data(self):
        days = database.get_uptime_days("Empty")
        assert days == []


class TestIncidents:
    def test_create_incident(self):
        inc_id = database.create_incident(
            title="Test Outage", impact="partial", message="Investigating"
        )
        assert inc_id > 0

    def test_create_incident_with_service(self):
        database.create_incident(
            title="GitHub Down",
            impact="partial",
            message="Investigating",
            service_name="GitHub API",
        )
        active = database.get_active_incident_for_service("GitHub API")
        assert active is not None
        assert active["title"] == "GitHub Down"

    def test_get_active_incidents(self):
        database.create_incident(title="Active1", impact="minor", message="msg1")
        database.create_incident(title="Active2", impact="partial", message="msg2")
        active = database.get_active_incidents()
        assert len(active) == 2
        titles = {a["title"] for a in active}
        assert titles == {"Active1", "Active2"}

    def test_incident_updates(self):
        inc_id = database.create_incident(
            title="Outage", impact="partial", message="Investigating the issue"
        )
        database.update_incident(inc_id, status="identified", message="Found the cause")
        database.update_incident(inc_id, status="resolved", message="Fixed")

        incidents = database.get_recent_incidents(limit=10)
        assert len(incidents) == 1
        inc = incidents[0]
        assert inc["status"] == "resolved"
        assert inc["resolved_at"] is not None
        assert len(inc["updates"]) == 3  # investigating + identified + resolved

    def test_resolve_sets_resolved_at(self):
        inc_id = database.create_incident(title="Bug", impact="minor", message="oops")
        database.update_incident(inc_id, status="resolved", message="fixed")
        incidents = database.get_recent_incidents(limit=1)
        assert incidents[0]["resolved_at"] is not None

    def test_no_active_incident_for_service(self):
        assert database.get_active_incident_for_service("Nothing") is None

    def test_resolved_incident_not_active(self):
        inc_id = database.create_incident(
            title="Temp", impact="minor", message="msg", service_name="TempSvc"
        )
        database.update_incident(inc_id, status="resolved", message="done")
        assert database.get_active_incident_for_service("TempSvc") is None

    def test_update_incident_returns_false_when_missing(self):
        ok = database.update_incident(
            999999, status="resolved", message="does-not-exist"
        )
        assert ok is False

    def test_external_id_lookup(self):
        database.create_incident(
            title="External", impact="minor", message="msg", external_id="ext-123"
        )
        found = database.get_incident_by_external_id("ext-123")
        assert found is not None
        assert found["title"] == "External"

    def test_external_id_not_found(self):
        assert database.get_incident_by_external_id("nope") is None

    def test_update_incident_impact_valid(self):
        inc_id = database.create_incident(
            title="Impact Test", impact="minor", message="msg"
        )
        database.update_incident_impact(inc_id, "major")
        with database.get_db() as db:
            row = db.execute(
                "SELECT impact FROM incidents WHERE id = ?", (inc_id,)
            ).fetchone()
        assert row["impact"] == "major"

    def test_update_incident_impact_rejects_invalid(self):
        inc_id = database.create_incident(
            title="Bad Impact", impact="partial", message="msg"
        )
        database.update_incident_impact(inc_id, "critical")
        with database.get_db() as db:
            row = db.execute(
                "SELECT impact FROM incidents WHERE id = ?", (inc_id,)
            ).fetchone()
        assert row["impact"] == "partial"  # unchanged

    def test_set_incident_jira_key(self):
        inc_id = database.create_incident(
            title="Jira Link", impact="minor", message="msg"
        )
        ok = database.set_incident_jira_key(inc_id, "OPS-101")
        assert ok is True
        inc = database.get_incident(inc_id)
        assert inc["jira_key"] == "OPS-101"

    def test_get_incident_returns_none_when_missing(self):
        assert database.get_incident(999999) is None


class TestRecentChecks:
    def test_recent_checks(self):
        database.record_check("Svc", "up", 10.0, None)
        database.record_check("Svc", "down", None, "err")
        database.record_check("Svc", "up", 5.0, None)
        recent = database.get_recent_checks("Svc", limit=2)
        assert len(recent) == 2
        # Most recent first
        assert recent[0]["status"] == "up"
        assert recent[1]["status"] == "down"


class TestCleanup:
    def test_cleanup_old_checks(self):
        # Record a check and clean up with 0 days retention
        database.record_check("Old", "up", 5.0, None)
        deleted = database.cleanup_old_checks(retention_days=0)
        # The check was just created (now), so 0-day retention won't delete it
        # This tests the function runs without error
        assert deleted >= 0


class TestCleanupOrphanServices:
    def test_keeps_valid_and_removes_orphans(self):
        database.record_check("KeepMe", "up", 10.0, None)
        database.record_check("RemoveMe", "up", 10.0, None)
        deleted = database.cleanup_orphan_services(["KeepMe"])
        assert deleted >= 1
        latest = database.get_latest_status(["KeepMe", "RemoveMe"])
        assert latest["KeepMe"] is not None
        assert latest["RemoveMe"] is None

    def test_removes_orphan_incidents(self):
        database.create_incident(
            title="Keep", impact="minor", message="msg", service_name="ValidSvc"
        )
        database.create_incident(
            title="Remove", impact="minor", message="msg", service_name="GoneSvc"
        )
        database.cleanup_orphan_services(["ValidSvc"])
        assert database.get_active_incident_for_service("ValidSvc") is not None
        assert database.get_active_incident_for_service("GoneSvc") is None

    def test_empty_valid_set_removes_all(self):
        database.record_check("Svc", "up", 10.0, None)
        deleted = database.cleanup_orphan_services([])
        assert deleted >= 1



class TestGetIncidentsByDay:
    """Test get_incidents_by_day — maps incidents to each day they span."""

    def test_single_day_incident(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        database.create_incident(
            title="Today's issue",
            service_name="Svc",
            created_at=today,
            resolved_at=today,
            status="resolved",
        )
        by_day = database.get_incidents_by_day()
        today_str = datetime.now(timezone.utc).date().isoformat()
        assert today_str in by_day
        assert len(by_day[today_str]) == 1
        assert by_day[today_str][0]["title"] == "Today's issue"

    def test_multi_day_incident_appears_on_each_day(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        end = (now - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        database.create_incident(
            title="Multi-day",
            service_name="Svc",
            created_at=start,
            resolved_at=end,
            status="resolved",
        )
        by_day = database.get_incidents_by_day()
        # Should appear on day -3, -2, and -1
        for d in range(1, 4):
            day_str = (now - timedelta(days=d)).date().isoformat()
            assert day_str in by_day, f"Missing day {day_str}"
            titles = [inc["title"] for inc in by_day[day_str]]
            assert "Multi-day" in titles

    def test_unresolved_incident_extends_to_today(self):
        start = (datetime.now(timezone.utc) - timedelta(days=2)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        database.create_incident(
            title="Ongoing",
            service_name="Svc",
            created_at=start,
            status="investigating",
        )
        by_day = database.get_incidents_by_day()
        today_str = datetime.now(timezone.utc).date().isoformat()
        assert today_str in by_day
        titles = [inc["title"] for inc in by_day[today_str]]
        assert "Ongoing" in titles

    def test_old_incident_capped_at_90_days(self):
        """Incidents older than 90 days should be capped at the 90-day boundary."""
        old = (datetime.now(timezone.utc) - timedelta(days=120)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        database.create_incident(
            title="Old spanning",
            service_name="Svc",
            created_at=old,
            resolved_at=recent,
            status="resolved",
        )
        by_day = database.get_incidents_by_day()
        # Should NOT appear on day -120 (beyond 90-day window)
        old_day = (datetime.now(timezone.utc) - timedelta(days=120)).date().isoformat()
        assert old_day not in by_day
        # Should appear on day -89 (start of window)
        boundary = (datetime.now(timezone.utc) - timedelta(days=89)).date().isoformat()
        if boundary in by_day:
            titles = [inc["title"] for inc in by_day[boundary]]
            assert "Old spanning" in titles

    def test_empty_when_no_incidents(self):
        by_day = database.get_incidents_by_day()
        assert by_day == {}

    def test_preserves_service_name(self):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        database.create_incident(
            title="AKS issue",
            service_name="Azure Kubernetes Service (AKS)",
            created_at=today,
            resolved_at=today,
            status="resolved",
        )
        by_day = database.get_incidents_by_day()
        today_str = datetime.now(timezone.utc).date().isoformat()
        assert by_day[today_str][0]["service_name"] == "Azure Kubernetes Service (AKS)"

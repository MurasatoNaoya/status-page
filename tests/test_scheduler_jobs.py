from unittest.mock import Mock
from datetime import datetime, timedelta, timezone

import status_page.scheduler_jobs as scheduler_jobs


def test_run_service_check_skip_short_circuits():
    run_check = Mock(return_value=("skip", None, "gated"))
    record_check = Mock()
    incr = Mock()

    scheduler_jobs.run_service_check(
        {"name": "Svc", "interval": 60},
        logger=Mock(),
        run_check_fn=run_check,
        record_check_fn=record_check,
        incr_fn=incr,
    )

    record_check.assert_not_called()
    incr.assert_called_once_with("checks.skipped")


def test_run_service_check_declares_after_threshold():
    run_check = Mock(return_value=("down", None, "boom"))
    now = datetime.now(timezone.utc)
    recent = [
        {"status": "down", "checked_at": (now - timedelta(seconds=5)).isoformat()},
        {"status": "down", "checked_at": (now - timedelta(seconds=10)).isoformat()},
        {"status": "down", "checked_at": (now - timedelta(seconds=15)).isoformat()},
    ]
    declare = Mock()

    scheduler_jobs.run_service_check(
        {"name": "Svc", "interval": 60},
        logger=Mock(),
        incident_threshold=3,
        run_check_fn=run_check,
        record_check_fn=Mock(),
        incr_fn=Mock(),
        observe_fn=Mock(),
        get_active_incident_for_service_fn=Mock(return_value=None),
        get_recent_checks_fn=Mock(return_value=recent),
        declare_incident_fn=declare,
        resolve_incident_fn=Mock(),
    )

    declare.assert_called_once()


def test_run_dns_bar_check_all_skipped():
    check_dns = Mock(
        return_value=[
            {"status": "skip", "label": "A"},
            {"status": "skip", "label": "B"},
        ]
    )
    incr = Mock()

    scheduler_jobs.run_dns_bar_check(
        {"name": "DNS", "targets": []},
        logger=Mock(),
        check_dns_bar_fn=check_dns,
        incr_fn=incr,
    )

    incr.assert_called_once_with("checks.skipped")


def test_get_scheduler_health_disabled():
    scheduler_jobs.mark_scheduler_disabled()
    health = scheduler_jobs.get_scheduler_health()
    assert health["status"] == "disabled"
    assert health["enabled"] is False

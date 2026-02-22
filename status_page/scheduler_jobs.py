import threading
import time
from datetime import datetime, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from status_page.checker import check_dns_bar, run_check
from status_page.feed_importer import poll_status_feed
from status_page.incident_service import (
    declare_incident_with_alerts,
    resolve_incident_with_alerts,
)
from status_page.telemetry import incr, observe, timed_call
from status_page.database import (
    cleanup_old_checks,
    get_active_incident_for_service,
    get_recent_checks,
    record_check,
)

_health_lock = threading.Lock()
_health = {
    "enabled": True,
    "scheduler_running": False,
    "started_at": None,
    "last_job_started_at": None,
    "last_job_finished_at": None,
    "last_error_at": None,
    "last_error": None,
    "job_runs": 0,
    "job_failures": 0,
    "running_jobs": 0,
    "stale_after_seconds": 900,
}


def _iso_now():
    return datetime.now(timezone.utc).isoformat()


def mark_scheduler_disabled():
    with _health_lock:
        _health.update(
            {
                "enabled": False,
                "scheduler_running": False,
                "started_at": None,
                "last_job_started_at": None,
                "last_job_finished_at": None,
                "last_error_at": None,
                "last_error": None,
                "job_runs": 0,
                "job_failures": 0,
                "running_jobs": 0,
            }
        )


def get_scheduler_health():
    with _health_lock:
        snap = dict(_health)
    now_ts = datetime.now(timezone.utc).timestamp()
    stale_after = snap["stale_after_seconds"]
    last_finished = snap.get("last_job_finished_at")
    started_at = snap.get("started_at")

    is_stale = False
    if snap["enabled"] and snap["scheduler_running"]:
        if last_finished:
            last_ts = datetime.fromisoformat(last_finished).timestamp()
            is_stale = (now_ts - last_ts) > stale_after
        elif started_at:
            start_ts = datetime.fromisoformat(started_at).timestamp()
            is_stale = (now_ts - start_ts) > stale_after

    if not snap["enabled"]:
        status = "disabled"
    elif snap["scheduler_running"] and not is_stale:
        status = "healthy"
    elif snap["scheduler_running"] and is_stale:
        status = "stale"
    else:
        status = "starting"

    snap["is_stale"] = is_stale
    snap["status"] = status
    return snap


def run_service_check(
    service,
    logger,
    incident_threshold=3,
    run_check_fn=run_check,
    record_check_fn=record_check,
    incr_fn=incr,
    observe_fn=observe,
    get_active_incident_for_service_fn=get_active_incident_for_service,
    get_recent_checks_fn=get_recent_checks,
    declare_incident_fn=declare_incident_with_alerts,
    resolve_incident_fn=resolve_incident_with_alerts,
):
    status, response_time_ms, error = run_check_fn(service)
    if status == "skip":
        logger.info("%s: skipped (%s)", service["name"], error or "no reason")
        incr_fn("checks.skipped")
        return
    record_check_fn(service["name"], status, response_time_ms, error)
    incr_fn("checks.total")
    incr_fn(f"checks.status.{status}")
    if response_time_ms is not None:
        observe_fn("checks.response_time_ms", response_time_ms)
    level = "debug" if status == "up" else "warning"
    getattr(logger, level)(
        "%s: %s (%.0fms)" if response_time_ms else "%s: %s%s",
        service["name"],
        status,
        response_time_ms or "",
    )

    name = service["name"]
    active = get_active_incident_for_service_fn(name)

    if status != "up":
        recent = get_recent_checks_fn(name, limit=incident_threshold + 1)
        interval = service.get("interval", 60)
        if len(recent) >= 2:
            prev_time = datetime.fromisoformat(
                recent[1]["checked_at"].replace("Z", "+00:00")
            )
            gap = (datetime.now(timezone.utc) - prev_time).total_seconds()
            if gap > interval * 3:
                logger.info(
                    "Skipping incident check for %s: stale gap of %.0fs", name, gap
                )
                return
        consecutive_failures = all(
            r["status"] != "up" for r in recent[:incident_threshold]
        )

        if consecutive_failures and len(recent) >= incident_threshold and not active:
            error_msg = error or "Service unavailable"
            declare_incident_fn(
                title=error_msg,
                impact="minor",
                message=f"Automated detection: {error_msg}",
                service_name=name,
            )
            logger.warning("Auto-created incident for %s: %s", name, error_msg)
    else:
        if active:
            resolved = resolve_incident_fn(
                incident_id=active["id"],
                message="Service has recovered. Automatically resolved.",
            )
            if resolved:
                logger.info("Auto-resolved incident #%d for %s", active["id"], name)


def run_dns_bar_check(
    dns_bar,
    logger,
    incident_threshold=3,
    check_dns_bar_fn=check_dns_bar,
    record_check_fn=record_check,
    incr_fn=incr,
    observe_fn=observe,
    get_active_incident_for_service_fn=get_active_incident_for_service,
    get_recent_checks_fn=get_recent_checks,
    declare_incident_fn=declare_incident_with_alerts,
    resolve_incident_fn=resolve_incident_with_alerts,
):
    if not dns_bar:
        return
    start = time.monotonic()
    results = check_dns_bar_fn(dns_bar.get("targets", []))
    active_results = [r for r in results if r["status"] != "skip"]
    skipped = len(results) - len(active_results)
    if not active_results:
        logger.info("DNS bar check skipped: all targets gated by env")
        incr_fn("checks.skipped")
        return
    elapsed_ms = (time.monotonic() - start) * 1000

    total = len(active_results)
    failed = [r for r in active_results if r["status"] != "up"]
    failed_count = len(failed)

    name = dns_bar.get("name", "DNS Resolution")
    if failed_count == 0:
        msg = None if skipped == 0 else f"{skipped} target(s) skipped by env gating"
        record_check_fn(name, "up", elapsed_ms, msg)
        incr_fn("checks.total")
        incr_fn("checks.status.up")
        observe_fn("checks.response_time_ms", elapsed_ms)
        active = get_active_incident_for_service_fn(name)
        if active:
            resolved = resolve_incident_fn(
                incident_id=active["id"],
                message="All DNS targets resolving normally. Automatically resolved.",
            )
            if resolved:
                logger.info("Auto-resolved DNS incident #%d", active["id"])
    else:
        failed_labels = ", ".join(r["label"] for r in failed)
        error_msg = f"{failed_count}/{total} failed: {failed_labels}"
        if skipped:
            error_msg += f" ({skipped} skipped)"
        record_check_fn(name, "down", elapsed_ms, error_msg)
        incr_fn("checks.total")
        incr_fn("checks.status.down")
        observe_fn("checks.response_time_ms", elapsed_ms)
        recent = get_recent_checks_fn(name, limit=incident_threshold)
        if (
            len(recent) >= incident_threshold
            and all(r["status"] != "up" for r in recent)
            and not get_active_incident_for_service_fn(name)
        ):
            declare_incident_fn(
                title="DNS Resolution Failures Detected",
                impact="partial",
                message=f"Automated detection: {error_msg}",
                service_name=name,
            )
            logger.warning("Auto-created DNS incident: %s", error_msg)


def create_scheduler(
    app,
    services,
    dns_bar,
    status_feeds,
    prune_login_failures,
    logger,
    incident_threshold=3,
):
    scheduler = BackgroundScheduler()
    max_interval = max(
        [s.get("interval", 60) for s in services]
        + ([dns_bar.get("interval", 60)] if dns_bar else [])
        + [f.get("interval", 300) for f in status_feeds]
        + [600]
    )
    stale_after = max(120, max_interval * 3)
    with _health_lock:
        _health.update(
            {
                "enabled": True,
                "scheduler_running": False,
                "started_at": _iso_now(),
                "last_job_started_at": None,
                "last_job_finished_at": None,
                "last_error_at": None,
                "last_error": None,
                "job_runs": 0,
                "job_failures": 0,
                "running_jobs": 0,
                "stale_after_seconds": stale_after,
            }
        )

    def _run_with_app_context(job_name, fn, *args, **kwargs):
        with _health_lock:
            _health["last_job_started_at"] = _iso_now()
            _health["running_jobs"] += 1
        try:
            with app.app_context():
                return timed_call(f"jobs.{job_name}.ms", fn, *args, **kwargs)
        except Exception as exc:
            with _health_lock:
                _health["job_failures"] += 1
                _health["last_error_at"] = _iso_now()
                _health["last_error"] = str(exc)
            raise
        finally:
            with _health_lock:
                _health["job_runs"] += 1
                _health["last_job_finished_at"] = _iso_now()
                _health["running_jobs"] = max(0, _health["running_jobs"] - 1)

    if dns_bar:
        interval = dns_bar.get("interval", 60)
        scheduler.add_job(
            _run_with_app_context,
            "interval",
            seconds=interval,
            args=[
                "run_dns_bar_check",
                run_dns_bar_check,
                dns_bar,
                logger,
                incident_threshold,
            ],
            id="dns_bar",
            replace_existing=True,
        )
        scheduler.add_job(
            _run_with_app_context,
            args=[
                "run_dns_bar_check",
                run_dns_bar_check,
                dns_bar,
                logger,
                incident_threshold,
            ],
            id="dns_bar_init",
        )

    for svc in services:
        interval = svc.get("interval", 60)
        scheduler.add_job(
            _run_with_app_context,
            "interval",
            seconds=interval,
            args=[
                "run_service_check",
                run_service_check,
                svc,
                logger,
                incident_threshold,
            ],
            id=svc["name"],
            replace_existing=True,
        )
        scheduler.add_job(
            _run_with_app_context,
            args=[
                "run_service_check",
                run_service_check,
                svc,
                logger,
                incident_threshold,
            ],
            id=f"{svc['name']}_init",
        )

    for feed in status_feeds:
        interval = feed.get("interval", 300)
        scheduler.add_job(
            _run_with_app_context,
            "interval",
            seconds=interval,
            args=["poll_status_feed", poll_status_feed, feed],
            id=f"feed_{feed['name']}",
            replace_existing=True,
        )
        scheduler.add_job(
            _run_with_app_context,
            args=["poll_status_feed", poll_status_feed, feed],
            id=f"feed_{feed['name']}_init",
        )

    scheduler.add_job(
        _run_with_app_context,
        "interval",
        minutes=10,
        args=["prune_login_failures", prune_login_failures],
        id="login_cleanup",
        replace_existing=True,
    )

    def _run_cleanup():
        deleted = cleanup_old_checks(retention_days=90)
        if deleted:
            logger.info("Cleaned up %d old check records", deleted)

    scheduler.add_job(
        _run_with_app_context,
        "cron",
        hour=3,
        minute=0,
        args=["db_cleanup", _run_cleanup],
        id="db_cleanup",
        replace_existing=True,
    )

    scheduler.start()
    with _health_lock:
        _health["scheduler_running"] = True
    return scheduler

import atexit
import hmac
import logging
import os
import secrets
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import wraps

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import (
    Flask,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)

from checker import check_dns_bar, run_check
from social import search_outage_chatter
from status_feeds import poll_feed
from database import (
    backfill_check_gaps,
    cleanup_old_checks,
    cleanup_orphan_services,
    create_incident,
    get_db,
    get_feed_incident_stats,
    get_page_view_stats,
    record_page_view,
    get_active_incident_for_service,
    get_active_incidents,
    get_check_coverage_start,
    get_incident_by_external_id,
    get_incident_coverage_start,
    get_incidents_by_day,
    get_latest_status,
    get_recent_checks,
    get_recent_incidents,
    get_uptime_days,
    get_uptime_percentage,
    init_db,
    record_check,
    update_incident,
    update_incident_impact,
)

# Number of consecutive failures before auto-creating an incident
INCIDENT_THRESHOLD = 3

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", secrets.token_hex(32))
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE", "true"
).lower() not in ("0", "false")

# Admin credentials (set via env vars in production)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")

# Brute-force protection: track failed login attempts per IP
_login_failures = defaultdict(list)  # ip -> [timestamp, ...]
_login_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes


def _get_csrf_token():
    """Generate or retrieve a CSRF token stored in the session."""
    if "_csrf_token" not in session:
        session["_csrf_token"] = secrets.token_hex(32)
    return session["_csrf_token"]


def _check_csrf_token():
    """Validate CSRF token from form submission against session."""
    token = request.form.get("_csrf_token", "")
    expected = session.get("_csrf_token", "")
    if not token or not hmac.compare_digest(token, expected):
        return False
    return True


app.jinja_env.globals["csrf_token"] = _get_csrf_token


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return decorated


def _anonymize_ip(ip):
    """Anonymize IP by zeroing the last octet (IPv4) or last 80 bits (IPv6)."""
    if not ip:
        return None
    if ":" in ip:
        # IPv6: keep first 48 bits (3 groups), zero the rest
        parts = ip.split(":")
        return ":".join(parts[:3] + ["0"] * (len(parts) - 3))
    # IPv4: zero last octet
    parts = ip.rsplit(".", 1)
    return parts[0] + ".0" if len(parts) == 2 else ip


@app.before_request
def track_page_view():
    # Only track actual page views (GET), not static files, API calls, or form POSTs
    if request.method != "GET":
        return
    if request.path.startswith("/static") or request.path.startswith("/api"):
        return
    record_page_view(
        path=request.path,
        ip=_anonymize_ip(request.remote_addr),
        user_agent=str(request.user_agent)[:200],
        referrer=request.referrer[:200] if request.referrer else None,
    )


@app.template_filter("gmt")
def format_gmt(value):
    """Format an ISO timestamp as 'Feb 14, 2026 18:22 GMT'."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt.strftime("%b %d, %Y %H:%M GMT")
    except (ValueError, AttributeError):
        return value


@app.template_filter("format_day")
def format_day(iso_date):
    """Format ISO date string as '14 Feb 2026'."""
    if not iso_date:
        return ""
    try:
        d = (
            datetime.fromisoformat(iso_date).date()
            if isinstance(iso_date, str)
            else iso_date
        )
        return f"{d.day} {d.strftime('%b')} {d.year}"
    except (ValueError, AttributeError):
        return iso_date


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


CONFIG = load_config()
SERVICES = CONFIG.get("services") or []
GROUPS = CONFIG.get("groups") or []
DNS_BAR = CONFIG.get("dns_bar", None)
PAGE = CONFIG.get("page") or {}
STATUS_FEEDS = CONFIG.get("status_feeds") or []


def run_service_check(service):
    status, response_time_ms, error = run_check(service)
    record_check(service["name"], status, response_time_ms, error)
    level = logging.DEBUG if status == "up" else logging.WARNING
    logger.log(
        level,
        "%s: %s (%.0fms)" if response_time_ms else "%s: %s%s",
        service["name"],
        status,
        response_time_ms or "",
    )

    name = service["name"]
    active = get_active_incident_for_service(name)

    if status != "up":
        # Check if we've hit the consecutive failure threshold
        recent = get_recent_checks(name, limit=INCIDENT_THRESHOLD + 1)
        # Skip if the previous check is stale (app was offline, not the service)
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
            r["status"] != "up" for r in recent[:INCIDENT_THRESHOLD]
        )

        if consecutive_failures and len(recent) >= INCIDENT_THRESHOLD and not active:
            error_msg = error or "Service unavailable"
            create_incident(
                title=error_msg,
                impact="minor",
                message=f"Automated detection: {error_msg}",
                service_name=name,
            )
            logger.warning("Auto-created incident for %s: %s", name, error_msg)
    else:
        # Service recovered — auto-resolve any active incident
        if active:
            update_incident(
                active["id"],
                status="resolved",
                message="Service has recovered. Automatically resolved.",
            )
            logger.info("Auto-resolved incident #%d for %s", active["id"], name)


def all_services():
    """Flatten all services from top-level and groups."""
    svcs = list(SERVICES)
    for group in GROUPS:
        svcs.extend(group.get("services", []))
    return svcs


def run_dns_bar_check():
    """Run all DNS bar checks and record aggregate to DB."""
    if not DNS_BAR:
        return
    start = time.monotonic()
    results = check_dns_bar(DNS_BAR.get("targets", []))
    elapsed_ms = (time.monotonic() - start) * 1000

    total = len(results)
    failed = [r for r in results if r["status"] != "up"]
    failed_count = len(failed)

    name = DNS_BAR.get("name", "DNS Resolution")
    if failed_count == 0:
        record_check(name, "up", elapsed_ms, None)
        # Auto-resolve DNS incident if active
        active = get_active_incident_for_service(name)
        if active:
            update_incident(
                active["id"],
                status="resolved",
                message="All DNS targets resolving normally. Automatically resolved.",
            )
            logger.info("Auto-resolved DNS incident #%d", active["id"])
    else:
        failed_labels = ", ".join(r["label"] for r in failed)
        error_msg = f"{failed_count}/{total} failed: {failed_labels}"
        record_check(name, "down", elapsed_ms, error_msg)
        # Auto-create DNS incident after consecutive failures
        recent = get_recent_checks(name, limit=INCIDENT_THRESHOLD)
        if (
            len(recent) >= INCIDENT_THRESHOLD
            and all(r["status"] != "up" for r in recent)
            and not get_active_incident_for_service(name)
        ):
            create_incident(
                title="DNS Resolution Failures Detected",
                impact="partial",
                message=f"Automated detection: {error_msg}",
                service_name=name,
            )
            logger.warning("Auto-created DNS incident: %s", error_msg)


def poll_status_feed(feed_config):
    """Poll an external status feed and import incidents."""
    results = poll_feed(feed_config)
    for item in results:
        if item.get("type") == "component_status":
            # Current component status — could be used for additional signals
            continue

        ext_id = item.get("external_id")
        if not ext_id:
            continue

        # Determine which services this incident affects
        affected = item.get("services") or []
        if not affected:
            if feed_config.get("components"):
                continue  # Feed has component mapping; this incident doesn't affect us
            affected = [None]  # No component map; create with service_name=NULL

        updates = item.get("updates", [])
        first_msg = updates[-1]["message"] if updates else item["title"]
        # Use the oldest update's status for the first update label
        # (e.g. "investigating" for live incidents, "resolved" for PIRs)
        first_update_status = (
            updates[-1].get("status", "investigating") if updates else "investigating"
        )

        for svc_name in affected:
            svc_ext_id = f"{ext_id}:{svc_name}" if svc_name else ext_id
            existing = get_incident_by_external_id(svc_ext_id)

            if existing:
                # Update status if it changed (e.g. resolved upstream)
                if existing["status"] != item["status"]:
                    update_incident(
                        existing["id"],
                        status=item["status"],
                        message=f"Status changed to {item['status']} (via {item['source']} status page).",
                        resolved_at=item.get("resolved_at"),
                    )
                    logger.info(
                        "Updated incident #%d (%s) to %s",
                        existing["id"],
                        svc_ext_id,
                        item["status"],
                    )
                # Keep impact in sync with feed classification
                new_impact = item.get("impact", "minor")
                if existing["impact"] != new_impact:
                    update_incident_impact(existing["id"], new_impact)
                continue

            # Import new incident
            inc_id = create_incident(
                title=item["title"],
                impact=item.get("impact", "minor"),
                message=first_msg,
                service_name=svc_name,
                external_id=svc_ext_id,
                created_at=item.get("created_at"),
                resolved_at=item.get("resolved_at"),
                status=item.get("status", "investigating"),
                initial_status=first_update_status,
            )

            # Process oldest→newest so the final update_incident sets the
            # correct current status on the incidents row.
            for upd in reversed(updates[:-1]):
                update_incident(
                    inc_id,
                    status=upd["status"],
                    message=upd["message"],
                    created_at=upd.get("created_at"),
                )

            if item.get("status") == "resolved":
                # Check if any update already marks this as resolved.
                initial_resolved = (
                    updates[-1]["status"] == "resolved" if updates else False
                )
                has_resolved = initial_resolved or any(
                    u["status"] == "resolved" for u in updates[:-1]
                )
                if not has_resolved:
                    update_incident(
                        inc_id,
                        status="resolved",
                        message=f"Resolved (via {item.get('source', 'external')} status page).",
                        created_at=item.get("resolved_at"),
                        resolved_at=item.get("resolved_at"),
                    )

            logger.info(
                "Imported incident from %s: %s for %s (id=%d)",
                item["source"],
                item["title"],
                svc_name,
                inc_id,
            )


def start_scheduler():
    scheduler = BackgroundScheduler()

    # Schedule DNS bar checks
    if DNS_BAR:
        interval = DNS_BAR.get("interval", 60)
        scheduler.add_job(
            run_dns_bar_check,
            "interval",
            seconds=interval,
            id="dns_bar",
            replace_existing=True,
        )
        scheduler.add_job(run_dns_bar_check, id="dns_bar_init")

    for svc in all_services():
        interval = svc.get("interval", 60)
        scheduler.add_job(
            run_service_check,
            "interval",
            seconds=interval,
            args=[svc],
            id=svc["name"],
            replace_existing=True,
        )
        # Run first check immediately
        scheduler.add_job(run_service_check, args=[svc], id=f"{svc['name']}_init")

    # Schedule external status feed polling
    for feed in STATUS_FEEDS:
        interval = feed.get("interval", 300)
        scheduler.add_job(
            poll_status_feed,
            "interval",
            seconds=interval,
            args=[feed],
            id=f"feed_{feed['name']}",
            replace_existing=True,
        )
        # Run first poll immediately
        scheduler.add_job(poll_status_feed, args=[feed], id=f"feed_{feed['name']}_init")

    # Periodic cleanup of stale login failure entries (every 10 minutes)
    def _prune_login_failures():
        now = time.monotonic()
        with _login_lock:
            stale = [
                ip
                for ip, ts in _login_failures.items()
                if all(now - t >= _LOGIN_WINDOW_SECONDS for t in ts)
            ]
            for ip in stale:
                del _login_failures[ip]

    scheduler.add_job(
        _prune_login_failures,
        "interval",
        minutes=10,
        id="login_cleanup",
        replace_existing=True,
    )

    # Daily cleanup of old check data (runs at 3am UTC)
    def _run_cleanup():
        deleted = cleanup_old_checks(retention_days=90)
        if deleted:
            logger.info("Cleaned up %d old check records", deleted)

    scheduler.add_job(
        _run_cleanup, "cron", hour=3, minute=0, id="db_cleanup", replace_existing=True
    )

    scheduler.start()
    return scheduler


_IMPACT_TO_SEVERITY = {
    "major": "major",
    "partial": "partial",
    "minor": "degraded",
    "none": "degraded",
}


def _incident_severity(incidents):
    """Get the worst severity from a list of incidents based on their impact."""
    if not incidents:
        return None
    worst = max(
        incidents,
        key=lambda i: {"major": 3, "partial": 2, "minor": 1}.get(
            i.get("impact", "minor"), 0
        ),
    )
    return _IMPACT_TO_SEVERITY.get(worst.get("impact"), "degraded")


def _filter_incidents_for_service(incidents_list, service_name):
    """Filter and deduplicate incidents relevant to a specific service."""
    seen = set()
    result = []
    for inc in incidents_list:
        inc_svc = inc.get("service_name")
        if inc_svc == service_name or inc_svc is None:
            if inc["id"] not in seen:
                seen.add(inc["id"])
                result.append(inc)
    return result[:3]


def build_service_data(svc_list, latest, incidents_by_day=None, coverage_start=None):
    """Build template-ready data for a list of services."""
    all_operational = True
    services_data = []
    today = datetime.now(timezone.utc).date()
    if incidents_by_day is None:
        incidents_by_day = {}
    if coverage_start is None:
        coverage_start = {}

    for svc in svc_list:
        name = svc["name"]
        status_info = latest.get(name)
        uptime_days = get_uptime_days(name)
        uptime_pct = get_uptime_percentage(name) if len(uptime_days) >= 3 else None
        interval_sec = svc.get("interval", 60)

        day_map = {d["day"]: d for d in uptime_days}
        # Need at least 3 days of data before showing colored bars
        has_history = len(uptime_days) >= 3
        # Incident coverage: days before the oldest feed incident are grey.
        # Services with no feed incidents have no coverage — also grey.
        svc_coverage = coverage_start.get(name)
        days_array = []
        for i in range(89, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            has_coverage = svc_coverage is not None and day >= svc_coverage

            # Always look up incidents for this day
            day_incidents = _filter_incidents_for_service(
                incidents_by_day.get(day, []), name
            )

            if day in day_map:
                d = day_map[day]
                raw_pct = 100.0 * d["up_count"] / d["total"] if d["total"] > 0 else None
                # Green if we have check history + coverage, OR feed coverage alone
                if has_history and has_coverage:
                    pct = raw_pct
                elif has_coverage:
                    pct = raw_pct if raw_pct is not None else 100.0
                else:
                    pct = None
                # Get unique errors for the day (deduplicated)
                errors_raw = d.get("errors") or ""
                errors = list(
                    dict.fromkeys(e.strip() for e in errors_raw.split("|") if e.strip())
                )[:3]
                down_count = d.get("down_count", 0)

                # Downtime estimation and severity
                downtime_sec = down_count * interval_sec
                severity = _incident_severity(day_incidents)

                days_array.append(
                    {
                        "date": day,
                        "uptime_pct": pct,
                        "down_count": down_count,
                        "total": d.get("total", 0),
                        "errors": errors,
                        "downtime_hours": downtime_sec // 3600,
                        "downtime_mins": (downtime_sec % 3600) // 60,
                        "severity": severity
                        if (has_coverage or day_incidents)
                        else None,
                        "incidents": day_incidents,
                    }
                )
            else:
                severity = _incident_severity(day_incidents)
                # Within coverage: assume operational (green) if no incidents/checks
                pct = 100.0 if has_coverage else None
                days_array.append(
                    {
                        "date": day,
                        "uptime_pct": pct,
                        "down_count": 0,
                        "total": 0,
                        "errors": [],
                        "downtime_hours": 0,
                        "downtime_mins": 0,
                        "severity": severity,
                        "incidents": day_incidents,
                    }
                )

        # Badge reflects the CURRENT status (latest check), not averages
        current_status = "operational"
        social_posts = []
        if not status_info:
            current_status = "no_data"
        elif status_info["status"] != "up":
            # Latest check is down — classify severity from active incident
            active_inc = get_active_incident_for_service(name)
            if active_inc:
                inc_impact = active_inc.get("impact", "minor")
                if inc_impact == "major":
                    current_status = "major_outage"
                elif inc_impact == "partial":
                    current_status = "partial_outage"
                else:
                    current_status = "degraded"
            else:
                # No active incident yet, but latest check failed — degraded
                current_status = "degraded"
            all_operational = False
            social_posts = search_outage_chatter(
                name, keywords=svc.get("social_keywords")
            )
        elif not has_history:
            current_status = "no_data"

        services_data.append(
            {
                "name": name,
                "status": current_status,
                "uptime_pct": uptime_pct,
                "response_time_ms": status_info["response_time_ms"]
                if status_info
                else None,
                "error": status_info["error_message"] if status_info else None,
                "days": days_array,
                "social_posts": social_posts,
            }
        )

    return services_data, all_operational


@app.route("/")
def index():
    all_svc_names = [s["name"] for s in all_services()]
    latest = get_latest_status(all_svc_names)
    all_incidents_by_day = get_incidents_by_day()
    coverage = get_incident_coverage_start()

    # Build set of all feed-covered service names
    feed_covered = set()
    feed_service_groups = []  # list of sets — one per feed
    for feed in STATUS_FEEDS:
        group = set()
        group.update(feed.get("components", {}).values())
        group.update(feed.get("covered_services", []))
        feed_service_groups.append(group)
        feed_covered.update(group)

    # For feed-covered services, coverage starts from the oldest incident
    # across ALL sibling services in the same feed.  Health check dates are
    # NOT used — if we have no feed data for a day we show grey (no data).
    for group in feed_service_groups:
        dates = [coverage[s] for s in group if s in coverage]
        group_floor = min(dates) if dates else None
        for svc_name in group:
            if group_floor:
                coverage[svc_name] = group_floor

    # For services NOT covered by any feed, use health-check dates for
    # coverage.  Days before the first check = grey (no data).
    non_feed_names = [n for n in all_svc_names if n not in feed_covered]
    if non_feed_names:
        check_cov = get_check_coverage_start(non_feed_names)
        for svc_name, start_date in check_cov.items():
            coverage[svc_name] = start_date

    services_data, top_ok = build_service_data(
        SERVICES, latest, all_incidents_by_day, coverage
    )

    groups_data = []
    groups_ok = True
    for group in GROUPS:
        group_svcs, group_operational = build_service_data(
            group.get("services", []), latest, all_incidents_by_day, coverage
        )
        if not group_operational:
            groups_ok = False

        # Aggregate uptime: average across all services in the group
        uptimes = [s["uptime_pct"] for s in group_svcs if s["uptime_pct"] is not None]
        group_uptime = round(sum(uptimes) / len(uptimes), 2) if uptimes else None

        # Aggregate 90-day bars: average uptime per day across all services
        days_array = []
        if group_svcs:
            for day_idx in range(90):
                day_pcts = []
                day_downs = 0
                day_totals = 0
                day_errors = []
                day_incidents_merged = {}
                day_date = None
                day_downtime_sec = 0
                for svc in group_svcs:
                    if day_idx < len(svc["days"]):
                        d = svc["days"][day_idx]
                        day_date = d["date"]
                        if d["uptime_pct"] is not None:
                            day_pcts.append(d["uptime_pct"])
                        day_downs += d.get("down_count", 0)
                        day_totals += d.get("total", 0)
                        day_errors.extend(d.get("errors", []))
                        day_downtime_sec += (
                            d.get("downtime_hours", 0) * 3600
                            + d.get("downtime_mins", 0) * 60
                        )
                        for inc in d.get("incidents", []):
                            day_incidents_merged[inc["id"]] = inc
                avg_pct = round(sum(day_pcts) / len(day_pcts), 1) if day_pcts else None
                # Deduplicate by title for display (same real-world event across services)
                seen_titles = {}
                for inc in day_incidents_merged.values():
                    if inc["title"] not in seen_titles:
                        seen_titles[inc["title"]] = inc
                merged_incidents = list(seen_titles.values())[:3]
                severity = _incident_severity(merged_incidents)
                days_array.append(
                    {
                        "date": day_date or "",
                        "uptime_pct": avg_pct,
                        "down_count": day_downs,
                        "total": day_totals,
                        "errors": list(dict.fromkeys(day_errors))[:3],
                        "downtime_hours": day_downtime_sec // 3600,
                        "downtime_mins": (day_downtime_sec % 3600) // 60,
                        "severity": severity,
                        "incidents": merged_incidents,
                    }
                )

        # Aggregate response time: average of latest across services
        resp_times = [
            s["response_time_ms"]
            for s in group_svcs
            if s["response_time_ms"] is not None
        ]
        avg_resp = round(sum(resp_times) / len(resp_times), 0) if resp_times else None

        groups_data.append(
            {
                "name": group["name"],
                "services": group_svcs,
                "operational": group_operational,
                "uptime_pct": group_uptime,
                "days": days_array,
                "response_time_ms": avg_resp,
            }
        )

    all_operational = top_ok and groups_ok
    active_incidents = get_active_incidents()
    past_incidents = get_recent_incidents(limit=200)

    # Build DNS bar as an aggregate service with 90-day uptime bar
    dns_bar_data = None
    if DNS_BAR:
        dns_name = DNS_BAR.get("name", "DNS Resolution")
        dns_svc_list = [{"name": dns_name, "interval": DNS_BAR.get("interval", 60)}]
        dns_latest = get_latest_status([dns_name])
        dns_services, dns_ok = build_service_data(
            dns_svc_list, dns_latest, all_incidents_by_day, coverage
        )
        if not dns_ok:
            all_operational = False
        dns_bar_data = dns_services[0] if dns_services else None

    # Group past incidents by date, deduplicating per-service copies
    # (e.g. Azure PIRs create one row per affected service, but should
    # show as a single entry in the Past Incidents list).
    # Track alias IDs so click-to-scroll from per-service bars still works.
    _impact_rank = {"major": 3, "partial": 2, "minor": 1, "none": 0}
    incidents_by_date = {}
    seen_base_ids = {}  # base_id → canonical incident dict
    for inc in past_incidents:
        ext_id = inc.get("external_id") or ""
        base_id = ext_id.rsplit(":", 1)[0] if ":" in ext_id else ext_id
        if base_id and base_id in seen_base_ids:
            # Add this ID as an alias; promote impact if this copy is worse
            canonical = seen_base_ids[base_id]
            canonical.setdefault("alias_ids", []).append(inc["id"])
            if _impact_rank.get(inc.get("impact"), 0) > _impact_rank.get(
                canonical.get("impact"), 0
            ):
                canonical["impact"] = inc["impact"]
            continue
        if base_id:
            seen_base_ids[base_id] = inc
        inc["alias_ids"] = []
        date_str = inc["created_at"][:10]  # "2026-02-14"
        try:
            dt = datetime.fromisoformat(date_str)
            date_label = dt.strftime("%b %d, %Y")
        except ValueError:
            date_label = date_str
        incidents_by_date.setdefault(date_label, []).append(inc)

    if active_incidents:
        overall = "major_outage"
    elif all_operational:
        overall = "operational"
    else:
        overall = "degraded"

    return render_template(
        "index.html",
        page=PAGE,
        services=services_data,
        groups=groups_data,
        dns_bar=dns_bar_data,
        active_incidents=active_incidents,
        incidents_by_date=incidents_by_date,
        incidents=past_incidents,
        overall=overall,
    )


# --- API for managing incidents ---


@app.route("/api/incidents", methods=["POST"])
@login_required
def api_create_incident():
    data = request.json
    if not data or "title" not in data:
        return jsonify({"error": "Missing required field: title"}), 400
    incident_id = create_incident(
        title=data["title"],
        impact=data.get("impact", "minor"),
        message=data.get("message", "Investigating the issue."),
        service_name=data.get("service_name"),
    )
    return jsonify({"id": incident_id}), 201


@app.route("/api/incidents/<int:incident_id>", methods=["PATCH"])
@login_required
def api_update_incident(incident_id):
    data = request.json
    if not data or "status" not in data or "message" not in data:
        return jsonify({"error": "Missing required fields: status, message"}), 400
    update_incident(incident_id, status=data["status"], message=data["message"])
    return jsonify({"ok": True})


@app.route("/api/health")
def api_health():
    service_names = [s["name"] for s in all_services()]
    latest = get_latest_status(service_names)
    return jsonify(
        {
            name: {
                "status": info["status"],
                "response_time_ms": info["response_time_ms"],
            }
            if info
            else {"status": "unknown"}
            for name, info in latest.items()
        }
    )


# --- Admin panel ---


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        if not _check_csrf_token():
            flash("Invalid form submission. Please try again.")
            return render_template("admin_login.html")

        ip = request.remote_addr or "unknown"
        now = time.monotonic()

        with _login_lock:
            # Prune old attempts and check rate limit
            _login_failures[ip] = [
                t for t in _login_failures[ip] if now - t < _LOGIN_WINDOW_SECONDS
            ]
            if not _login_failures[ip]:
                _login_failures.pop(ip, None)
            if len(_login_failures.get(ip, [])) >= _LOGIN_MAX_ATTEMPTS:
                flash("Too many login attempts. Please try again later.")
                return render_template("admin_login.html"), 429

        user_ok = hmac.compare_digest(request.form.get("username", ""), ADMIN_USER)
        pass_ok = hmac.compare_digest(request.form.get("password", ""), ADMIN_PASS)
        if user_ok and pass_ok:
            with _login_lock:
                _login_failures.pop(ip, None)
            session["admin"] = True
            return redirect(url_for("admin_panel"))
        with _login_lock:
            _login_failures[ip].append(now)
        flash("Invalid credentials")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin", None)
    return redirect(url_for("index"))


@app.route("/admin")
@login_required
def admin_panel():
    active = get_active_incidents()
    recent = get_recent_incidents(limit=20)
    svc_names = [s["name"] for s in all_services()]
    integrations = {
        "SLACK_WEBHOOK_URL": os.environ.get("SLACK_WEBHOOK_URL"),
        "TEAMS_WEBHOOK_URL": os.environ.get("TEAMS_WEBHOOK_URL"),
        "JIRA_URL": os.environ.get("JIRA_URL"),
    }
    # Build feed coverage info for the backfill section
    feed_coverage = []
    for feed in STATUS_FEEDS:
        svc_names_feed = set()
        svc_names_feed.update(feed.get("components", {}).values())
        svc_names_feed.update(feed.get("covered_services", []))
        stats = get_feed_incident_stats(svc_names_feed)
        date_from = stats["oldest"][:10] if stats["oldest"] else None
        date_to = stats["newest"][:10] if stats["newest"] else None
        days_span = None
        if date_from and date_to:
            d0 = datetime.strptime(date_from, "%Y-%m-%d")
            d1 = datetime.strptime(date_to, "%Y-%m-%d")
            days_span = (d1 - d0).days
        feed_coverage.append(
            {
                "name": feed["name"],
                "incident_count": stats["cnt"],
                "date_from": date_from,
                "date_to": date_to,
                "days_span": days_span,
            }
        )

    return render_template(
        "admin.html",
        active_incidents=active,
        recent_incidents=recent,
        services=svc_names,
        config=integrations,
        feed_coverage=feed_coverage,
    )


@app.route("/admin/declare", methods=["POST"])
@login_required
def admin_declare_incident():
    if not _check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin_panel"))
    title = request.form["title"]
    impact = request.form.get("impact", "partial")
    message = request.form.get("message", "Investigating the issue.")
    service = request.form.get("service") or None

    incident_id = create_incident(
        title=title, impact=impact, message=message, service_name=service
    )

    # Send alerts (Slack, email, Jira — configured via env vars)
    from alerts import send_alerts

    send_alerts(
        incident_id=incident_id,
        title=title,
        impact=impact,
        message=message,
        service=service,
    )

    flash(f"Incident declared: {title}")
    return redirect(url_for("admin_panel"))


@app.route("/admin/metrics")
@login_required
def admin_metrics():
    try:
        days = int(request.args.get("days", 30))
    except (ValueError, TypeError):
        days = 30
    days = max(1, min(days, 365))
    stats = get_page_view_stats(days=days)
    return render_template("admin_metrics.html", stats=stats, days=days)


@app.route("/admin/update/<int:incident_id>", methods=["POST"])
@login_required
def admin_update_incident(incident_id):
    if not _check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin_panel"))
    status = request.form["status"]
    message = request.form["message"]
    update_incident(incident_id, status=status, message=message)

    if status == "resolved":
        from alerts import send_resolution

        send_resolution(incident_id=incident_id, message=message)

    flash(f"Incident updated to: {status}")
    return redirect(url_for("admin_panel"))


@app.route("/admin/backfill", methods=["POST"])
@login_required
def admin_backfill():
    """Trigger a manual backfill of real incident data from all configured status feeds."""
    if not _check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin_panel"))
    # Count incidents before the backfill
    with get_db() as db:
        before_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    for feed in STATUS_FEEDS:
        try:
            poll_status_feed(feed)
        except Exception as e:
            logger.error("Backfill failed for feed %s: %s", feed.get("name"), e)

    # Count incidents after the backfill
    with get_db() as db:
        after_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    imported = after_count - before_count
    flash(
        f"Backfill complete: {imported} new incident(s) imported from {len(STATUS_FEEDS)} feed(s)."
    )
    return redirect(url_for("admin_panel"))


@app.route("/admin/feed-coverage")
@login_required
def admin_feed_coverage():
    """Return JSON showing feed coverage: incident count and date range per feed."""
    coverage = []
    for feed in STATUS_FEEDS:
        svc_names = set()
        svc_names.update(feed.get("components", {}).values())
        svc_names.update(feed.get("covered_services", []))
        stats = get_feed_incident_stats(svc_names)
        coverage.append(
            {
                "feed": feed.get("name"),
                "services": sorted(svc_names),
                "incident_count": stats["cnt"],
                "oldest": stats["oldest"],
                "newest": stats["newest"],
            }
        )
    return jsonify(coverage)


def _startup():
    """Initialise DB, clean orphans, backfill gaps, and start scheduler."""
    if ADMIN_PASS == "changeme":
        logger.warning(
            "*** Admin password is the default 'changeme'. "
            "Set ADMIN_PASS env var for production. ***"
        )
    init_db()

    valid_names = [svc["name"] for svc in all_services()]
    if DNS_BAR:
        valid_names.append(DNS_BAR.get("name", "DNS Resolution"))
    orphan_result = cleanup_orphan_services(valid_names)
    if orphan_result:
        logger.info("Startup cleanup: removed %d orphan rows", orphan_result)
    gap_days = backfill_check_gaps(valid_names)
    if gap_days:
        logger.info(
            "Startup: %d service-days with no check data (shown as no-data)", gap_days
        )

    return start_scheduler()


# Run startup when the module loads (works with both `flask run` and `python app.py`)
# Guard against double-init from Flask's debug reloader
if os.environ.get("DISABLE_SCHEDULER"):
    # E2E test mode: init DB but skip scheduler to avoid background writes
    init_db()
    _scheduler = None
elif os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    _scheduler = _startup()
    if _scheduler is not None:
        atexit.register(_scheduler.shutdown)
else:
    # First reloader process — skip init, the child will handle it
    _scheduler = None


if __name__ == "__main__":
    if _scheduler is None and not os.environ.get("DISABLE_SCHEDULER"):
        _scheduler = _startup()
    try:
        app.run(host="0.0.0.0", port=5555, debug=False)
    finally:
        if _scheduler is not None:
            _scheduler.shutdown()

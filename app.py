import atexit
import hmac
import logging
import os
import secrets
import threading
import time
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

from alerts import send_alerts, send_resolution
from checker import check_dns_bar, run_check
from feed_importer import poll_status_feed
from database import (
    backfill_check_gaps,
    cleanup_old_checks,
    cleanup_orphan_services,
    close_request_db,
    create_incident,
    get_db,
    get_feed_incident_stats,
    get_active_incident_for_service,
    get_active_incidents,
    get_incidents_by_day,
    get_incident,
    get_latest_status,
    get_recent_checks,
    get_recent_incidents,
    get_uptime_days,
    get_uptime_percentage,
    init_db,
    record_check,
    set_incident_jira_key,
    update_incident,
)

# Number of consecutive failures before auto-creating an incident
INCIDENT_THRESHOLD = 3

# Valid incident status values
_VALID_STATUSES = {"investigating", "identified", "monitoring", "resolved"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)
_SECRET_KEY_ENV = os.environ.get("SECRET_KEY")
app.secret_key = _SECRET_KEY_ENV or secrets.token_hex(32)
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SECURE"] = os.environ.get(
    "SESSION_COOKIE_SECURE", "true"
).lower() not in ("0", "false")

# Admin credentials (set via env vars in production)
ADMIN_USER = os.environ.get("ADMIN_USER", "admin")
ADMIN_PASS = os.environ.get("ADMIN_PASS", "changeme")

# Brute-force protection: track failed login attempts per IP
_login_failures = {}  # ip -> [timestamp, ...]
_login_lock = threading.Lock()
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300  # 5 minutes


def _prune_login_failures():
    now = time.monotonic()
    with _login_lock:
        # Remove expired entries
        stale = [
            ip
            for ip, ts in _login_failures.items()
            if all(now - t >= _LOGIN_WINDOW_SECONDS for t in ts)
        ]
        for ip in stale:
            del _login_failures[ip]
        # Hard cap: if still over 10k IPs, evict oldest entries
        if len(_login_failures) > 10000:
            by_age = sorted(_login_failures.items(), key=lambda kv: max(kv[1]))
            for ip, _ in by_age[: len(_login_failures) - 10000]:
                del _login_failures[ip]


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
app.teardown_appcontext(close_request_db)


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        "script-src 'self' 'unsafe-inline'; img-src 'self' data:"
    )
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    return response


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("admin"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Authentication required"}), 401
            return redirect(url_for("admin_login"))
        return f(*args, **kwargs)

    return decorated


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


_APP_DIR = os.path.dirname(os.path.abspath(__file__))


def load_config(path="config.yaml"):
    config_path = os.path.join(_APP_DIR, path) if not os.path.isabs(path) else path
    with open(config_path) as f:
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


def start_scheduler():
    scheduler = BackgroundScheduler()

    def _run_with_app_context(fn, *args, **kwargs):
        with app.app_context():
            return fn(*args, **kwargs)

    # Schedule DNS bar checks
    if DNS_BAR:
        interval = DNS_BAR.get("interval", 60)
        scheduler.add_job(
            _run_with_app_context,
            "interval",
            seconds=interval,
            args=[run_dns_bar_check],
            id="dns_bar",
            replace_existing=True,
        )
        scheduler.add_job(
            _run_with_app_context, args=[run_dns_bar_check], id="dns_bar_init"
        )

    for svc in all_services():
        interval = svc.get("interval", 60)
        scheduler.add_job(
            _run_with_app_context,
            "interval",
            seconds=interval,
            args=[run_service_check, svc],
            id=svc["name"],
            replace_existing=True,
        )
        # Run first check immediately
        scheduler.add_job(
            _run_with_app_context,
            args=[run_service_check, svc],
            id=f"{svc['name']}_init",
        )

    # Schedule external status feed polling
    for feed in STATUS_FEEDS:
        interval = feed.get("interval", 300)
        scheduler.add_job(
            _run_with_app_context,
            "interval",
            seconds=interval,
            args=[poll_status_feed, feed],
            id=f"feed_{feed['name']}",
            replace_existing=True,
        )
        # Run first poll immediately
        scheduler.add_job(
            _run_with_app_context,
            args=[poll_status_feed, feed],
            id=f"feed_{feed['name']}_init",
        )

    scheduler.add_job(
        _run_with_app_context,
        "interval",
        minutes=10,
        args=[_prune_login_failures],
        id="login_cleanup",
        replace_existing=True,
    )

    # Daily cleanup of old check data (runs at 3am UTC)
    def _run_cleanup():
        deleted = cleanup_old_checks(retention_days=90)
        if deleted:
            logger.info("Cleaned up %d old check records", deleted)

    scheduler.add_job(
        _run_with_app_context,
        "cron",
        hour=3,
        minute=0,
        args=[_run_cleanup],
        id="db_cleanup",
        replace_existing=True,
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


def build_service_data(svc_list, latest, incidents_by_day=None):
    """Build template-ready data for a list of services."""
    all_operational = True
    services_data = []
    today = datetime.now(timezone.utc).date()
    if incidents_by_day is None:
        incidents_by_day = {}

    for svc in svc_list:
        name = svc["name"]
        status_info = latest.get(name)
        uptime_days = get_uptime_days(name)
        uptime_pct = get_uptime_percentage(name) if len(uptime_days) >= 3 else None
        interval_sec = svc.get("interval", 60)

        day_map = {d["day"]: d for d in uptime_days}
        # Need at least 3 days of data before showing colored bars
        has_history = len(uptime_days) >= 3
        days_array = []
        for i in range(89, -1, -1):
            day = (today - timedelta(days=i)).isoformat()

            # Always look up incidents for this day
            day_incidents = _filter_incidents_for_service(
                incidents_by_day.get(day, []), name
            )

            if day in day_map:
                d = day_map[day]
                raw_pct = 100.0 * d["up_count"] / d["total"] if d["total"] > 0 else None
                # Never infer green from feed coverage alone: no checks means no data.
                pct = raw_pct if has_history else None
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
                        "severity": severity if day_incidents else None,
                        "incidents": day_incidents,
                    }
                )
            else:
                severity = _incident_severity(day_incidents)
                days_array.append(
                    {
                        "date": day,
                        "uptime_pct": None,
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
            }
        )

    return services_data, all_operational


def _build_group_aggregate(group, latest, all_incidents_by_day):
    """Build aggregated data for a service group."""
    group_svcs, group_operational = build_service_data(
        group.get("services", []), latest, all_incidents_by_day
    )

    uptimes = [s["uptime_pct"] for s in group_svcs if s["uptime_pct"] is not None]
    group_uptime = round(sum(uptimes) / len(uptimes), 2) if uptimes else None

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

    resp_times = [
        s["response_time_ms"] for s in group_svcs if s["response_time_ms"] is not None
    ]
    avg_resp = round(sum(resp_times) / len(resp_times), 0) if resp_times else None

    return {
        "name": group["name"],
        "services": group_svcs,
        "operational": group_operational,
        "uptime_pct": group_uptime,
        "days": days_array,
        "response_time_ms": avg_resp,
    }


_IMPACT_RANK = {"major": 3, "partial": 2, "minor": 1, "none": 0}


def _deduplicate_past_incidents(past_incidents):
    """Group past incidents by date, deduplicating per-service copies."""
    incidents_by_date = {}
    seen_base_ids = {}
    for inc in past_incidents:
        ext_id = inc.get("external_id") or ""
        base_id = ext_id.rsplit(":", 1)[0] if ":" in ext_id else ext_id
        if base_id and base_id in seen_base_ids:
            canonical = seen_base_ids[base_id]
            canonical.setdefault("alias_ids", []).append(inc["id"])
            if _IMPACT_RANK.get(inc.get("impact"), 0) > _IMPACT_RANK.get(
                canonical.get("impact"), 0
            ):
                canonical["impact"] = inc["impact"]
            continue
        if base_id:
            seen_base_ids[base_id] = inc
        inc["alias_ids"] = []
        date_str = inc["created_at"][:10]
        try:
            dt = datetime.fromisoformat(date_str)
            date_label = dt.strftime("%b %d, %Y")
        except ValueError:
            date_label = date_str
        incidents_by_date.setdefault(date_label, []).append(inc)
    return incidents_by_date


@app.route("/")
def index():
    all_svc_names = [s["name"] for s in all_services()]
    latest = get_latest_status(all_svc_names)
    all_incidents_by_day = get_incidents_by_day()
    services_data, top_ok = build_service_data(SERVICES, latest, all_incidents_by_day)

    groups_data = []
    groups_ok = True
    for group in GROUPS:
        gdata = _build_group_aggregate(group, latest, all_incidents_by_day)
        if not gdata["operational"]:
            groups_ok = False
        groups_data.append(gdata)

    all_operational = top_ok and groups_ok
    active_incidents = get_active_incidents()
    past_incidents = get_recent_incidents(limit=200)

    # DNS bar
    dns_bar_data = None
    if DNS_BAR:
        dns_name = DNS_BAR.get("name", "DNS Resolution")
        dns_svc_list = [{"name": dns_name, "interval": DNS_BAR.get("interval", 60)}]
        dns_latest = get_latest_status([dns_name])
        dns_services, dns_ok = build_service_data(dns_svc_list, dns_latest, all_incidents_by_day)
        if not dns_ok:
            all_operational = False
        dns_bar_data = dns_services[0] if dns_services else None

    incidents_by_date = _deduplicate_past_incidents(past_incidents)

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


def _check_api_csrf():
    """Validate CSRF token from X-CSRF-Token header for JSON API endpoints."""
    token = request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf_token", "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        return False
    return True


@app.route("/api/incidents", methods=["POST"])
@login_required
def api_create_incident():
    if not _check_api_csrf():
        return jsonify({"error": "Missing or invalid CSRF token"}), 403
    data = request.json
    if not data or "title" not in data:
        return jsonify({"error": "Missing required field: title"}), 400
    impact = data.get("impact", "minor")
    if impact not in {"major", "partial", "minor"}:
        return jsonify(
            {"error": "Invalid impact. Must be one of: major, minor, partial"}
        ), 400
    incident_id = create_incident(
        title=data["title"][:255],
        impact=impact,
        message=data.get("message", "Investigating the issue.")[:2000],
        service_name=data.get("service_name"),
    )
    return jsonify({"id": incident_id}), 201


@app.route("/api/incidents/<int:incident_id>", methods=["PATCH"])
@login_required
def api_update_incident(incident_id):
    if not _check_api_csrf():
        return jsonify({"error": "Missing or invalid CSRF token"}), 403
    data = request.json
    if not data or "status" not in data or "message" not in data:
        return jsonify({"error": "Missing required fields: status, message"}), 400
    if data["status"] not in _VALID_STATUSES:
        return jsonify(
            {
                "error": f"Invalid status. Must be one of: {', '.join(sorted(_VALID_STATUSES))}"
            }
        ), 400
    updated = update_incident(
        incident_id, status=data["status"], message=data["message"][:2000]
    )
    if not updated:
        return jsonify({"error": "Incident not found"}), 404
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
            attempts = [
                t
                for t in _login_failures.get(ip, [])
                if now - t < _LOGIN_WINDOW_SECONDS
            ]
            if attempts:
                _login_failures[ip] = attempts
            else:
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
            _login_failures.setdefault(ip, []).append(now)
            # Bound memory growth between scheduled prune cycles.
            if len(_login_failures) > 10000:
                by_age = sorted(_login_failures.items(), key=lambda kv: max(kv[1]))
                for old_ip, _ in by_age[: len(_login_failures) - 10000]:
                    del _login_failures[old_ip]
        flash("Invalid credentials")
    return render_template("admin_login.html")


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    if not _check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin_panel"))
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
    title = request.form.get("title", "").strip()[:255]
    if not title:
        flash("Incident title is required.")
        return redirect(url_for("admin_panel"))
    impact = request.form.get("impact", "partial")
    if impact not in ("major", "partial", "minor"):
        impact = "partial"
    message = request.form.get("message", "Investigating the issue.").strip()[:2000]
    service = request.form.get("service") or None

    incident_id = create_incident(
        title=title, impact=impact, message=message, service_name=service
    )

    jira_key = send_alerts(
        incident_id=incident_id,
        title=title,
        impact=impact,
        message=message,
        service=service,
    )
    if jira_key:
        set_incident_jira_key(incident_id, jira_key)

    flash(f"Incident declared: {title}")
    return redirect(url_for("admin_panel"))


@app.route("/admin/update/<int:incident_id>", methods=["POST"])
@login_required
def admin_update_incident(incident_id):
    if not _check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin_panel"))
    status = request.form["status"]
    message = request.form["message"][:2000]
    if status not in _VALID_STATUSES:
        flash("Invalid status value.")
        return redirect(url_for("admin_panel"))
    incident = get_incident(incident_id)
    if not incident:
        flash("Incident not found.")
        return redirect(url_for("admin_panel"))
    updated = update_incident(incident_id, status=status, message=message)
    if not updated:
        flash("Incident not found.")
        return redirect(url_for("admin_panel"))

    if status == "resolved":
        send_resolution(
            incident_id=incident_id,
            message=message,
            jira_key=incident.get("jira_key"),
        )

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

    failed_feeds = []
    succeeded_feeds = 0
    for feed in STATUS_FEEDS:
        try:
            poll_status_feed(feed)
            succeeded_feeds += 1
        except Exception as e:
            logger.error("Backfill failed for feed %s: %s", feed.get("name"), e)
            failed_feeds.append(feed.get("name", "unknown"))

    # Count incidents after the backfill
    with get_db() as db:
        after_count = db.execute(
            "SELECT COUNT(*) FROM incidents WHERE external_id IS NOT NULL"
        ).fetchone()[0]

    imported = after_count - before_count
    if failed_feeds:
        flash(
            "Backfill partially completed: "
            f"{imported} new incident(s), {succeeded_feeds}/{len(STATUS_FEEDS)} feed(s) succeeded. "
            f"Failed feed(s): {', '.join(failed_feeds)}."
        )
    else:
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
        if os.environ.get("ALLOW_DEFAULT_PASSWORD"):
            logger.warning(
                "*** Admin password is the default 'changeme'. "
                "Set ADMIN_PASS env var for production. ***"
            )
        else:
            raise RuntimeError(
                "ADMIN_PASS is still 'changeme'. "
                "Set the ADMIN_PASS environment variable before running in production. "
                "For local development, set ALLOW_DEFAULT_PASSWORD=1."
            )
    if not _SECRET_KEY_ENV:
        if os.environ.get("ALLOW_DEFAULT_PASSWORD"):
            logger.warning(
                "*** SECRET_KEY not set — sessions will not survive restarts. "
                "Set SECRET_KEY env var for production. ***"
            )
        else:
            raise RuntimeError(
                "SECRET_KEY is not set. "
                "Set the SECRET_KEY environment variable before running in production. "
                "For local development, set ALLOW_DEFAULT_PASSWORD=1."
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

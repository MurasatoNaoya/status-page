import atexit
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import wraps

import yaml
from flask import (
    Flask,
    g,
    jsonify,
    render_template,
    request,
    redirect,
    url_for,
    session,
    flash,
)

from status_page.checker import check_dns_bar, run_check
from status_page.config_schema import validate_config
from status_page.feed_importer import poll_status_feed
from status_page.status_feeds import get_feed_backfill_capability
from status_page.alerts import send_test_email
from status_page.incident_service import (
    declare_incident_with_alerts,
    resolve_incident_with_alerts,
)
from status_page.scheduler_jobs import (
    create_scheduler,
    get_scheduler_health,
    mark_scheduler_disabled,
)
from status_page.status_view import (
    build_coverage_map,
    build_feed_coverage,
    build_group_aggregate,
    build_service_data,
    deduplicate_past_incidents,
    filter_incidents_for_service as status_view_filter_incidents_for_service,
    incident_severity as status_view_incident_severity,
)
from status_page.telemetry import incr, snapshot
from status_page.database import (
    backfill_check_gaps,
    cleanup_orphan_services,
    close_request_db,
    create_incident,
    get_active_incident_for_service,
    get_feed_incident_stats,
    get_db,
    get_active_incidents,
    get_incidents_by_day,
    get_latest_status,
    get_recent_checks,
    get_recent_incidents,
    init_db,
    record_check,
    update_incident,
)

# Number of consecutive failures before auto-creating an incident
INCIDENT_THRESHOLD = 3

# Valid incident status values
_VALID_STATUSES = {"investigating", "identified", "monitoring", "resolved"}

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_APP_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(
    __name__,
    template_folder=os.path.join(_APP_DIR, "templates"),
    static_folder=os.path.join(_APP_DIR, "static"),
    static_url_path="/static",
)
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


@app.before_request
def _set_csp_nonce():
    g._csp_nonce = secrets.token_urlsafe(16)


def _csp_nonce():
    return getattr(g, "_csp_nonce", "")


app.jinja_env.globals["csp_nonce"] = _csp_nonce


@app.after_request
def set_security_headers(response):
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    nonce = getattr(g, "_csp_nonce", "")
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; "
        f"script-src 'self' 'nonce-{nonce}'; img-src 'self' data:"
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


def _mask_email(email):
    if not email or "@" not in email:
        return ""
    local, domain = email.split("@", 1)
    keep = min(2, len(local))
    return f"{local[:keep]}***@{domain}"


def _mask_email_list(value):
    if not value:
        return ""
    emails = [v.strip() for v in value.split(",") if v.strip()]
    return ", ".join(_mask_email(e) for e in emails if "@" in e)


def load_config(path="config.yaml"):
    config_path = os.path.join(_APP_DIR, path) if not os.path.isabs(path) else path
    with open(config_path) as f:
        loaded = yaml.safe_load(f) or {}
    return validate_config(loaded)


CONFIG = load_config()
SERVICES = CONFIG.get("services") or []
GROUPS = CONFIG.get("groups") or []
DNS_BAR = CONFIG.get("dns_bar", None)
PAGE = CONFIG.get("page") or {}
STATUS_FEEDS = CONFIG.get("status_feeds") or []


def all_services():
    """Flatten all services from top-level and groups."""
    svcs = list(SERVICES)
    for group in GROUPS:
        svcs.extend(group.get("services", []))
    return svcs


def _incident_severity(incidents):
    """Backward-compatible alias for tests and external imports."""
    return status_view_incident_severity(incidents)


def _filter_incidents_for_service(incidents_list, service_name):
    """Backward-compatible alias for tests and external imports."""
    return status_view_filter_incidents_for_service(incidents_list, service_name)


def run_service_check(service):
    """Backward-compatible wrapper; scheduler uses extracted module jobs."""
    status, response_time_ms, error = run_check(service)
    if status == "skip":
        logger.info("%s: skipped (%s)", service["name"], error or "no reason")
        incr("checks.skipped")
        return
    record_check(service["name"], status, response_time_ms, error)
    incr("checks.total")
    incr(f"checks.status.{status}")
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
        recent = get_recent_checks(name, limit=INCIDENT_THRESHOLD + 1)
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
            declare_incident_with_alerts(
                title=error_msg,
                impact="minor",
                message=f"Automated detection: {error_msg}",
                service_name=name,
            )
            logger.warning("Auto-created incident for %s: %s", name, error_msg)
    elif active:
        resolved = resolve_incident_with_alerts(
            incident_id=active["id"],
            message="Service has recovered. Automatically resolved.",
        )
        if resolved:
            logger.info("Auto-resolved incident #%d for %s", active["id"], name)


def run_dns_bar_check():
    """Backward-compatible wrapper; scheduler uses extracted module jobs."""
    if not DNS_BAR:
        return
    start = time.monotonic()
    results = check_dns_bar(DNS_BAR.get("targets", []))
    active_results = [r for r in results if r["status"] != "skip"]
    skipped = len(results) - len(active_results)
    if not active_results:
        logger.info("DNS bar check skipped: all targets gated by env")
        incr("checks.skipped")
        return
    elapsed_ms = (time.monotonic() - start) * 1000
    total = len(active_results)
    failed = [r for r in active_results if r["status"] != "up"]
    failed_count = len(failed)
    name = DNS_BAR.get("name", "DNS Resolution")
    if failed_count == 0:
        msg = None if skipped == 0 else f"{skipped} target(s) skipped by env gating"
        record_check(name, "up", elapsed_ms, msg)
        incr("checks.total")
        incr("checks.status.up")
        active = get_active_incident_for_service(name)
        if active:
            resolved = resolve_incident_with_alerts(
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
        record_check(name, "down", elapsed_ms, error_msg)
        incr("checks.total")
        incr("checks.status.down")
        recent = get_recent_checks(name, limit=INCIDENT_THRESHOLD)
        if (
            len(recent) >= INCIDENT_THRESHOLD
            and all(r["status"] != "up" for r in recent)
            and not get_active_incident_for_service(name)
        ):
            declare_incident_with_alerts(
                title="DNS Resolution Failures Detected",
                impact="partial",
                message=f"Automated detection: {error_msg}",
                service_name=name,
            )
            logger.warning("Auto-created DNS incident: %s", error_msg)


@app.route("/")
def index():
    all_svc_names = [s["name"] for s in all_services()]
    latest = get_latest_status(all_svc_names)
    all_incidents_by_day = get_incidents_by_day()
    coverage = build_coverage_map(all_svc_names, STATUS_FEEDS)

    services_data, top_ok = build_service_data(
        SERVICES, latest, all_incidents_by_day, coverage=coverage
    )

    groups_data = []
    groups_ok = True
    for group in GROUPS:
        gdata = build_group_aggregate(
            group, latest, all_incidents_by_day, coverage=coverage
        )
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
        dns_services, dns_ok = build_service_data(
            dns_svc_list, dns_latest, all_incidents_by_day, coverage=coverage
        )
        if not dns_ok:
            all_operational = False
        dns_bar_data = dns_services[0] if dns_services else None

    incidents_by_date = deduplicate_past_incidents(past_incidents)

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


@app.route("/api/health/scheduler")
def api_scheduler_health():
    health = get_scheduler_health()
    code = 200 if health["status"] in {"healthy", "disabled"} else 503
    return jsonify(health), code


@app.route("/api/metrics")
@login_required
def api_metrics():
    return jsonify(snapshot())


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
        "ALERT_EMAIL_TO": os.environ.get("ALERT_EMAIL_TO"),
        "ALERT_EMAIL_TO_MASKED": _mask_email_list(os.environ.get("ALERT_EMAIL_TO")),
        "SMTP_HOST": os.environ.get("SMTP_HOST"),
        "RESEND_API_KEY": bool(os.environ.get("RESEND_API_KEY")),
        "RESEND_FROM": os.environ.get("RESEND_FROM"),
    }

    return render_template(
        "admin.html",
        active_incidents=active,
        recent_incidents=recent,
        services=svc_names,
        config=integrations,
        feed_coverage=build_feed_coverage(STATUS_FEEDS),
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

    declare_incident_with_alerts(
        title=title, impact=impact, message=message, service_name=service
    )

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
    if status == "resolved":
        resolved = resolve_incident_with_alerts(
            incident_id=incident_id, message=message
        )
        if not resolved:
            flash("Incident not found.")
            return redirect(url_for("admin_panel"))
    else:
        updated = update_incident(incident_id, status=status, message=message)
        if not updated:
            flash("Incident not found.")
            return redirect(url_for("admin_panel"))

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
            incr("backfill.feed.success")
        except Exception as e:
            logger.error("Backfill failed for feed %s: %s", feed.get("name"), e)
            failed_feeds.append(feed.get("name", "unknown"))
            incr("backfill.feed.error")

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


@app.route("/admin/test-email", methods=["POST"])
@login_required
def admin_test_email():
    if not _check_csrf_token():
        flash("Invalid form submission. Please try again.")
        return redirect(url_for("admin_panel"))
    sent = send_test_email()
    if sent:
        flash("Test email sent.")
    else:
        flash("Test email failed. Check email configuration and logs.")
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
        capability = get_feed_backfill_capability(feed)
        coverage.append(
            {
                "feed": feed.get("name"),
                "feed_type": capability["feed_type"],
                "ingestion": capability["ingestion"],
                "known_limit_days": capability["known_limit_days"],
                "cap_type": capability["cap_type"],
                "cap_summary": capability["cap_summary"],
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
    cleanup_enabled = os.environ.get("CLEANUP_ORPHANS_ON_STARTUP", "").lower() in (
        "1",
        "true",
        "yes",
    )
    if cleanup_enabled:
        if not valid_names:
            logger.warning(
                "Startup cleanup: skipped orphan cleanup because valid service list is empty"
            )
        else:
            orphan_result = cleanup_orphan_services(valid_names)
            if orphan_result:
                logger.info("Startup cleanup: removed %d orphan rows", orphan_result)
    else:
        logger.info(
            "Startup cleanup: skipped orphan cleanup (set CLEANUP_ORPHANS_ON_STARTUP=1 to enable)"
        )
    gap_days = backfill_check_gaps(valid_names)
    if gap_days:
        logger.info(
            "Startup: %d service-days with no check data (shown as no-data)", gap_days
        )

    return create_scheduler(
        app=app,
        services=all_services(),
        dns_bar=DNS_BAR,
        status_feeds=STATUS_FEEDS,
        prune_login_failures=_prune_login_failures,
        logger=logger,
        incident_threshold=INCIDENT_THRESHOLD,
    )


# Run startup when the module loads (works with both `flask run` and `python app.py`)
# Guard against double-init from Flask's debug reloader
if os.environ.get("DISABLE_SCHEDULER"):
    # E2E test mode: init DB but skip scheduler to avoid background writes
    init_db()
    mark_scheduler_disabled()
    _scheduler = None
elif os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
    _scheduler = _startup()
    if _scheduler is not None:
        atexit.register(_scheduler.shutdown)
else:
    # First reloader process — skip init, the child will handle it
    _scheduler = None


def main():
    global _scheduler
    if _scheduler is None and not os.environ.get("DISABLE_SCHEDULER"):
        _scheduler = _startup()
    try:
        app.run(host="0.0.0.0", port=5555, debug=False)
    finally:
        if _scheduler is not None:
            _scheduler.shutdown()


if __name__ == "__main__":
    main()

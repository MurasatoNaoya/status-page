import atexit
import hmac
import logging
import os
import secrets
import threading
import time
from datetime import datetime

import yaml
from flask import (
    Flask,
    g,
    render_template,
    request,
    session,
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
    run_dns_bar_check as scheduler_run_dns_bar_check,
    run_service_check as scheduler_run_service_check,
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
from status_page.telemetry import incr, observe, snapshot
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
from status_page.routes.admin import admin_bp
from status_page.routes.api import api_bp
from status_page.routes.public import public_bp
from status_page.runtime import set_runtime_context_provider

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
_ENABLE_HSTS = os.environ.get("ENABLE_HSTS", "false").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
_HSTS_VALUE = os.environ.get("HSTS_VALUE", "max-age=31536000; includeSubDomains")

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
    if _ENABLE_HSTS:
        forwarded_proto = request.headers.get("X-Forwarded-Proto", "").lower()
        if request.is_secure or forwarded_proto == "https":
            response.headers["Strict-Transport-Security"] = _HSTS_VALUE
    return response


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
_scheduler_lock = threading.Lock()
_scheduler = None


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
    """Backward-compatible wrapper around scheduler job implementation."""
    result = scheduler_run_service_check(
        service=service,
        logger=logger,
        incident_threshold=INCIDENT_THRESHOLD,
        run_check_fn=run_check,
        record_check_fn=record_check,
        incr_fn=incr,
        observe_fn=observe,
        get_active_incident_for_service_fn=get_active_incident_for_service,
        get_recent_checks_fn=get_recent_checks,
        declare_incident_fn=declare_incident_with_alerts,
        resolve_incident_fn=resolve_incident_with_alerts,
    )
    _invalidate_index_cache("service_check")
    return result


def run_dns_bar_check():
    """Backward-compatible wrapper around scheduler job implementation."""
    result = scheduler_run_dns_bar_check(
        dns_bar=DNS_BAR,
        logger=logger,
        incident_threshold=INCIDENT_THRESHOLD,
        check_dns_bar_fn=check_dns_bar,
        record_check_fn=record_check,
        incr_fn=incr,
        observe_fn=observe,
        get_active_incident_for_service_fn=get_active_incident_for_service,
        get_recent_checks_fn=get_recent_checks,
        declare_incident_fn=declare_incident_with_alerts,
        resolve_incident_fn=resolve_incident_with_alerts,
    )
    _invalidate_index_cache("dns_bar_check")
    return result


_index_cache_lock = threading.Lock()
_index_cache = {"expires_at": 0.0, "html": None}


def _index_cache_ttl_seconds():
    raw = os.environ.get("INDEX_CACHE_TTL_SECONDS", "15")
    try:
        ttl = int(raw)
    except (TypeError, ValueError):
        ttl = 15
    return max(0, ttl)


def _cache_enabled():
    return not app.config.get("TESTING") and _index_cache_ttl_seconds() > 0


def _invalidate_index_cache(_reason=None):
    with _index_cache_lock:
        _index_cache["expires_at"] = 0.0
        _index_cache["html"] = None


def _render_index_uncached():
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


def _inject_csp_nonce(html):
    """Inject per-request CSP nonce into cached HTML template output."""
    return html.replace("__CSP_NONCE__", getattr(g, "_csp_nonce", ""))


def _render_index_cached():
    if not _cache_enabled():
        return _inject_csp_nonce(_render_index_uncached())
    now = time.time()
    with _index_cache_lock:
        if _index_cache["html"] is not None and now < _index_cache["expires_at"]:
            return _inject_csp_nonce(_index_cache["html"])
    html = _render_index_uncached()
    with _index_cache_lock:
        _index_cache["html"] = html
        _index_cache["expires_at"] = now + _index_cache_ttl_seconds()
    return _inject_csp_nonce(html)


# --- API for managing incidents ---


def _check_api_csrf():
    """Validate CSRF token from X-CSRF-Token header for JSON API endpoints."""
    token = request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf_token", "")
    if not token or not expected or not hmac.compare_digest(token, expected):
        return False
    return True


def _apply_loaded_config(new_config):
    global CONFIG, SERVICES, GROUPS, DNS_BAR, PAGE, STATUS_FEEDS
    CONFIG = new_config
    SERVICES = CONFIG.get("services") or []
    GROUPS = CONFIG.get("groups") or []
    DNS_BAR = CONFIG.get("dns_bar", None)
    PAGE = CONFIG.get("page") or {}
    STATUS_FEEDS = CONFIG.get("status_feeds") or []


def _restart_scheduler():
    global _scheduler
    if os.environ.get("DISABLE_SCHEDULER"):
        mark_scheduler_disabled()
        _scheduler = None
        return
    with _scheduler_lock:
        old = _scheduler
        if old is not None:
            old.shutdown()
        _scheduler = create_scheduler(
            app=app,
            services=all_services(),
            dns_bar=DNS_BAR,
            status_feeds=STATUS_FEEDS,
            prune_login_failures=_prune_login_failures,
            logger=logger,
            incident_threshold=INCIDENT_THRESHOLD,
            on_data_change=_invalidate_index_cache,
        )


def reload_runtime_config():
    try:
        loaded = load_config()
    except Exception as e:
        logger.error("Config reload failed: %s", e)
        return False, "Config reload failed. Check logs for details."
    _apply_loaded_config(loaded)
    _invalidate_index_cache("config_reload")
    try:
        _restart_scheduler()
    except Exception as e:
        logger.error("Scheduler restart failed after config reload: %s", e)
        return False, "Config loaded but scheduler restart failed. Check logs."
    return True, "Config reloaded successfully."


def _runtime_context_provider():
    return {
        "admin_user": ADMIN_USER,
        "admin_pass": ADMIN_PASS,
        "login_failures": _login_failures,
        "login_lock": _login_lock,
        "login_window_seconds": _LOGIN_WINDOW_SECONDS,
        "login_max_attempts": _LOGIN_MAX_ATTEMPTS,
        "check_form_csrf": _check_csrf_token,
        "check_api_csrf": _check_api_csrf,
        "valid_statuses": _VALID_STATUSES,
        "mask_email_list": _mask_email_list,
        "all_services": all_services,
        "status_feeds": lambda: STATUS_FEEDS,
        "get_active_incidents": get_active_incidents,
        "get_recent_incidents": get_recent_incidents,
        "build_feed_coverage": build_feed_coverage,
        "declare_incident_with_alerts": declare_incident_with_alerts,
        "resolve_incident_with_alerts": resolve_incident_with_alerts,
        "update_incident": update_incident,
        "poll_status_feed": poll_status_feed,
        "incr": incr,
        "logger": logger,
        "get_db": get_db,
        "send_test_email": send_test_email,
        "get_feed_incident_stats": get_feed_incident_stats,
        "get_feed_backfill_capability": get_feed_backfill_capability,
        "reload_runtime_config": reload_runtime_config,
        "render_index_cached": _render_index_cached,
        "create_incident": create_incident,
        "invalidate_index_cache": _invalidate_index_cache,
        "get_latest_status": get_latest_status,
        "get_scheduler_health": get_scheduler_health,
        "snapshot": snapshot,
    }


set_runtime_context_provider(_runtime_context_provider)


app.register_blueprint(public_bp)
app.register_blueprint(api_bp)
app.register_blueprint(admin_bp)


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

    _restart_scheduler()
    return _scheduler


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

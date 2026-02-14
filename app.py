import logging
from datetime import datetime, timedelta, timezone

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

from checker import check_dns_bar, run_check
from social import search_outage_chatter
from database import (
    create_incident,
    get_active_incidents,
    get_latest_status,
    get_recent_incidents,
    get_uptime_days,
    get_uptime_percentage,
    init_db,
    record_check,
    update_incident,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


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


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


CONFIG = load_config()
SERVICES = CONFIG.get("services", [])
GROUPS = CONFIG.get("groups", [])
DNS_BAR = CONFIG.get("dns_bar", None)
PAGE = CONFIG.get("page", {})

# Cache for DNS bar results (updated by scheduler)
_dns_bar_results = []


def run_service_check(service):
    status, response_time_ms, error = run_check(service)
    record_check(service["name"], status, response_time_ms, error)
    level = logging.DEBUG if status == "up" else logging.WARNING
    logger.log(level, "%s: %s (%.0fms)" if response_time_ms else "%s: %s%s",
               service["name"], status, response_time_ms or "")


def all_services():
    """Flatten all services from top-level and groups."""
    svcs = list(SERVICES)
    for group in GROUPS:
        svcs.extend(group.get("services", []))
    return svcs


def run_dns_bar_check():
    """Run all DNS bar checks and cache results."""
    global _dns_bar_results
    if DNS_BAR:
        _dns_bar_results = check_dns_bar(DNS_BAR.get("targets", []))


def start_scheduler():
    scheduler = BackgroundScheduler()

    # Schedule DNS bar checks
    if DNS_BAR:
        interval = DNS_BAR.get("interval", 60)
        scheduler.add_job(run_dns_bar_check, "interval", seconds=interval,
                          id="dns_bar", replace_existing=True)
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
    scheduler.start()
    return scheduler


def build_service_data(svc_list, latest):
    """Build template-ready data for a list of services."""
    all_operational = True
    services_data = []
    today = datetime.now(timezone.utc).date()

    for svc in svc_list:
        name = svc["name"]
        status_info = latest.get(name)
        uptime_pct = get_uptime_percentage(name)
        uptime_days = get_uptime_days(name)

        day_map = {d["day"]: d for d in uptime_days}
        days_array = []
        for i in range(89, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            if day in day_map:
                d = day_map[day]
                pct = 100.0 * d["up_count"] / d["total"] if d["total"] > 0 else None
                # Get unique errors for the day (deduplicated)
                errors_raw = d.get("errors") or ""
                errors = list(dict.fromkeys(e.strip() for e in errors_raw.split("|") if e.strip()))[:3]
                days_array.append({
                    "date": day,
                    "uptime_pct": pct,
                    "down_count": d.get("down_count", 0),
                    "total": d.get("total", 0),
                    "errors": errors,
                })
            else:
                days_array.append({"date": day, "uptime_pct": None, "down_count": 0, "total": 0, "errors": []})

        current_status = "operational"
        social_posts = []
        if status_info and status_info["status"] != "up":
            current_status = "major_outage"
            all_operational = False
            social_posts = search_outage_chatter(
                name, keywords=svc.get("social_keywords")
            )

        services_data.append({
            "name": name,
            "status": current_status,
            "uptime_pct": uptime_pct,
            "response_time_ms": status_info["response_time_ms"] if status_info else None,
            "error": status_info["error_message"] if status_info else None,
            "days": days_array,
            "social_posts": social_posts,
        })

    return services_data, all_operational


@app.route("/")
def index():
    all_svc_names = [s["name"] for s in all_services()]
    latest = get_latest_status(all_svc_names)

    services_data, top_ok = build_service_data(SERVICES, latest)

    groups_data = []
    groups_ok = True
    for group in GROUPS:
        group_svcs, group_operational = build_service_data(group.get("services", []), latest)
        if not group_operational:
            groups_ok = False
        groups_data.append({
            "name": group["name"],
            "services": group_svcs,
            "operational": group_operational,
        })

    all_operational = top_ok and groups_ok
    active_incidents = get_active_incidents()
    past_incidents = get_recent_incidents(limit=50)

    # Check DNS bar health
    dns_bar_data = None
    if DNS_BAR and _dns_bar_results:
        all_dns_up = all(r["status"] == "up" for r in _dns_bar_results)
        if not all_dns_up:
            all_operational = False
        dns_bar_data = {
            "name": DNS_BAR.get("name", "DNS Resolution"),
            "targets": _dns_bar_results,
            "all_up": all_dns_up,
        }

    # Group past incidents by date
    incidents_by_date = {}
    for inc in past_incidents:
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
        overall = "major_outage"

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
def api_create_incident():
    data = request.json
    incident_id = create_incident(
        title=data["title"],
        impact=data.get("impact", "minor"),
        message=data.get("message", "Investigating the issue."),
    )
    return jsonify({"id": incident_id}), 201


@app.route("/api/incidents/<int:incident_id>", methods=["PATCH"])
def api_update_incident(incident_id):
    data = request.json
    update_incident(incident_id, status=data["status"], message=data["message"])
    return jsonify({"ok": True})


@app.route("/api/health")
def api_health():
    service_names = [s["name"] for s in all_services()]
    latest = get_latest_status(service_names)
    return jsonify({
        name: {"status": info["status"], "response_time_ms": info["response_time_ms"]}
        if info else {"status": "unknown"}
        for name, info in latest.items()
    })


if __name__ == "__main__":
    init_db()
    scheduler = start_scheduler()
    try:
        app.run(host="0.0.0.0", port=5555, debug=False)
    finally:
        scheduler.shutdown()

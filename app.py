import logging
from datetime import datetime, timedelta, timezone

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, render_template, request

from checker import run_check
from database import (
    create_incident,
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


def load_config(path="config.yaml"):
    with open(path) as f:
        return yaml.safe_load(f)


CONFIG = load_config()
SERVICES = CONFIG.get("services", [])
PAGE = CONFIG.get("page", {})


def run_service_check(service):
    status, response_time_ms, error = run_check(service)
    record_check(service["name"], status, response_time_ms, error)
    level = logging.DEBUG if status == "up" else logging.WARNING
    logger.log(level, "%s: %s (%.0fms)" if response_time_ms else "%s: %s%s",
               service["name"], status, response_time_ms or "")


def start_scheduler():
    scheduler = BackgroundScheduler()
    for svc in SERVICES:
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


@app.route("/")
def index():
    service_names = [s["name"] for s in SERVICES]
    latest = get_latest_status(service_names)

    services_data = []
    all_operational = True
    for svc in SERVICES:
        name = svc["name"]
        status_info = latest.get(name)
        uptime_pct = get_uptime_percentage(name)
        uptime_days = get_uptime_days(name)

        # Build 90-day array (fill missing days)
        today = datetime.now(timezone.utc).date()
        day_map = {d["day"]: d for d in uptime_days}
        days_array = []
        for i in range(89, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            if day in day_map:
                d = day_map[day]
                pct = 100.0 * d["up_count"] / d["total"] if d["total"] > 0 else None
                days_array.append({"date": day, "uptime_pct": pct})
            else:
                days_array.append({"date": day, "uptime_pct": None})

        current_status = "operational"
        if status_info and status_info["status"] != "up":
            current_status = "major_outage"
            all_operational = False

        services_data.append({
            "name": name,
            "status": current_status,
            "uptime_pct": uptime_pct,
            "response_time_ms": status_info["response_time_ms"] if status_info else None,
            "days": days_array,
        })

    incidents = get_recent_incidents(limit=20)

    overall = "operational" if all_operational else "major_outage"

    return render_template(
        "index.html",
        page=PAGE,
        services=services_data,
        incidents=incidents,
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
    service_names = [s["name"] for s in SERVICES]
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

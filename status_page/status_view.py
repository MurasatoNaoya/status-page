from datetime import datetime, timedelta, timezone

from status_page.database import (
    get_active_incident_for_service,
    get_check_coverage_start,
    get_feed_incident_stats,
    get_incident_coverage_start,
    get_uptime_days,
    get_uptime_percentage,
)
from status_page.status_feeds import get_feed_backfill_capability

_IMPACT_TO_SEVERITY = {
    "major": "major",
    "partial": "partial",
    "minor": "degraded",
    "none": "degraded",
}

_IMPACT_RANK = {"major": 3, "partial": 2, "minor": 1, "none": 0}


def incident_severity(incidents):
    if not incidents:
        return None
    worst = max(
        incidents,
        key=lambda i: {"major": 3, "partial": 2, "minor": 1}.get(
            i.get("impact", "minor"), 0
        ),
    )
    return _IMPACT_TO_SEVERITY.get(worst.get("impact"), "degraded")


def filter_incidents_for_service(incidents_list, service_name):
    seen = set()
    result = []
    for inc in incidents_list:
        inc_svc = inc.get("service_name")
        if inc_svc == service_name or inc_svc is None:
            if inc["id"] not in seen:
                seen.add(inc["id"])
                result.append(inc)
    return result[:3]


def build_coverage_map(all_service_names, status_feeds):
    incident_starts = get_incident_coverage_start()
    check_starts = get_check_coverage_start(all_service_names)

    feed_coverage_start = {}
    for feed in status_feeds:
        svc_names_feed = set()
        svc_names_feed.update(feed.get("components", {}).values())
        svc_names_feed.update(feed.get("covered_services", []))
        if not svc_names_feed:
            continue
        stats = get_feed_incident_stats(svc_names_feed)
        oldest = stats["oldest"][:10] if stats.get("oldest") else None
        if oldest:
            for svc in svc_names_feed:
                cur = feed_coverage_start.get(svc)
                if cur is None or oldest < cur:
                    feed_coverage_start[svc] = oldest

    coverage = {}
    for name in all_service_names:
        dates = []
        if name in check_starts:
            dates.append(check_starts[name])
        if name in incident_starts:
            dates.append(incident_starts[name])
        if name in feed_coverage_start:
            dates.append(feed_coverage_start[name])
        if dates:
            coverage[name] = min(dates)
    return coverage


def build_service_data(svc_list, latest, incidents_by_day=None, coverage=None):
    all_operational = True
    services_data = []
    today = datetime.now(timezone.utc).date()
    if incidents_by_day is None:
        incidents_by_day = {}
    if coverage is None:
        coverage = {}

    for svc in svc_list:
        name = svc["name"]
        status_info = latest.get(name)
        uptime_days = get_uptime_days(name)
        uptime_pct = get_uptime_percentage(name) if len(uptime_days) >= 3 else None
        interval_sec = svc.get("interval", 60)

        day_map = {d["day"]: d for d in uptime_days}
        coverage_start = coverage.get(name)
        days_array = []
        for i in range(89, -1, -1):
            day = (today - timedelta(days=i)).isoformat()
            day_incidents = filter_incidents_for_service(
                incidents_by_day.get(day, []), name
            )
            in_coverage = coverage_start is not None and day >= coverage_start

            if day in day_map:
                d = day_map[day]
                pct = 100.0 * d["up_count"] / d["total"] if d["total"] > 0 else None
                errors_raw = d.get("errors") or ""
                errors = list(
                    dict.fromkeys(e.strip() for e in errors_raw.split("|") if e.strip())
                )[:3]
                down_count = d.get("down_count", 0)
                downtime_sec = down_count * interval_sec
                severity = incident_severity(day_incidents)

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
                severity = incident_severity(day_incidents)
                inferred_pct = 100.0 if (in_coverage and not day_incidents) else None
                days_array.append(
                    {
                        "date": day,
                        "uptime_pct": inferred_pct,
                        "down_count": 0,
                        "total": 0,
                        "errors": [],
                        "downtime_hours": 0,
                        "downtime_mins": 0,
                        "severity": severity,
                        "incidents": day_incidents,
                    }
                )

        current_status = "operational"
        if not status_info:
            current_status = "no_data"
        elif status_info["status"] != "up":
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
                current_status = "degraded"
            all_operational = False
        elif not uptime_days and not coverage_start:
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


def build_group_aggregate(group, latest, all_incidents_by_day, coverage=None):
    group_svcs, group_operational = build_service_data(
        group.get("services", []), latest, all_incidents_by_day, coverage=coverage
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
            severity = incident_severity(merged_incidents)
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

    svc_statuses = [s["status"] for s in group_svcs]
    if not svc_statuses or all(s == "no_data" for s in svc_statuses):
        group_status = "no_data"
    elif "major_outage" in svc_statuses:
        group_status = "major_outage"
    elif any(
        s in {"partial_outage", "degraded", "under_maintenance"} for s in svc_statuses
    ):
        group_status = "degraded"
    else:
        group_status = "operational"

    return {
        "name": group["name"],
        "services": group_svcs,
        "operational": group_operational,
        "status": group_status,
        "uptime_pct": group_uptime,
        "days": days_array,
        "response_time_ms": avg_resp,
    }


def deduplicate_past_incidents(past_incidents):
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


def build_feed_coverage(status_feeds):
    feed_coverage = []
    for feed in status_feeds:
        svc_names_feed = set()
        svc_names_feed.update(feed.get("components", {}).values())
        svc_names_feed.update(feed.get("covered_services", []))
        stats = get_feed_incident_stats(svc_names_feed)
        capability = get_feed_backfill_capability(feed)
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
                "feed_type": capability["feed_type"],
                "ingestion": capability["ingestion"],
                "known_limit_days": capability["known_limit_days"],
                "cap_type": capability["cap_type"],
                "cap_summary": capability["cap_summary"],
            }
        )
    return feed_coverage

"""One-off script to backfill incidents for check failures with no matching incident."""
import database
from database import get_db

database.init_db()
by_day = database.get_incidents_by_day()

with get_db() as db:
    rows = db.execute(
        "SELECT service_name, date(checked_at) as day,"
        " COUNT(*) as total,"
        " SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) as up_count,"
        " SUM(CASE WHEN status != 'up' THEN 1 ELSE 0 END) as down_count,"
        " GROUP_CONCAT(CASE WHEN status != 'up' AND error_message IS NOT NULL"
        "   THEN error_message END, ' | ') as errors"
        " FROM check_results"
        " GROUP BY service_name, day"
        " HAVING down_count > 0"
        " ORDER BY day"
    ).fetchall()

    created = 0
    for r in rows:
        svc = r["service_name"]
        day = r["day"]
        day_incs = by_day.get(day, [])
        has_inc = any(
            inc.get("service_name") == svc or inc.get("service_name") is None
            for inc in day_incs
        )
        if has_inc:
            continue

        errors = (r["errors"] or "").split(" | ")
        error_msg = errors[0].strip() if errors and errors[0].strip() else "Service degradation detected"
        error_msg = error_msg[:120]

        ts = f"{day}T00:00:00Z"
        cursor = db.execute(
            "INSERT INTO incidents (title, impact, service_name, created_at, resolved_at, status)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (error_msg, "minor", svc, ts, ts, "resolved"),
        )
        inc_id = cursor.lastrowid
        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message, created_at)"
            " VALUES (?, ?, ?, ?)",
            (inc_id, "investigating", error_msg, ts),
        )
        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message, created_at)"
            " VALUES (?, ?, ?, ?)",
            (inc_id, "resolved", "Service recovered.", ts),
        )
        created += 1
        print(f"  {day} {svc}: {error_msg[:60]}")

    print(f"\nBackfilled {created} incidents")

import sqlite3
import os
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

DB_PATH = os.environ.get("STATUS_DB", "status.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as db:
        db.executescript("""
            CREATE TABLE IF NOT EXISTS check_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                service_name TEXT NOT NULL,
                status TEXT NOT NULL,
                response_time_ms REAL,
                error_message TEXT,
                checked_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_check_results_service
                ON check_results(service_name, checked_at);

            CREATE TABLE IF NOT EXISTS incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                service_name TEXT,
                external_id TEXT,
                status TEXT NOT NULL DEFAULT 'investigating',
                impact TEXT NOT NULL DEFAULT 'minor',
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
                resolved_at TEXT
            );

            CREATE TABLE IF NOT EXISTS page_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                path TEXT NOT NULL,
                ip TEXT,
                user_agent TEXT,
                referrer TEXT,
                viewed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_page_views_date
                ON page_views(viewed_at);

            CREATE TABLE IF NOT EXISTS incident_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL REFERENCES incidents(id),
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
        """)
        # Migrations for existing DBs
        try:
            db.execute("SELECT service_name FROM incidents LIMIT 1")
        except sqlite3.OperationalError:
            db.execute("ALTER TABLE incidents ADD COLUMN service_name TEXT")
        try:
            db.execute("SELECT external_id FROM incidents LIMIT 1")
        except sqlite3.OperationalError:
            db.execute("ALTER TABLE incidents ADD COLUMN external_id TEXT")


def record_check(service_name, status, response_time_ms, error_message=None):
    """Record a real health check result.  response_time_ms is required —
    synthetic or fabricated records are not allowed."""
    if response_time_ms is None and status == "up":
        raise ValueError(
            "Cannot record an 'up' check without a real response_time_ms. "
            "Synthetic data is not permitted."
        )
    with get_db() as db:
        db.execute(
            "INSERT INTO check_results (service_name, status, response_time_ms, error_message) VALUES (?, ?, ?, ?)",
            (service_name, status, response_time_ms, error_message),
        )


def record_page_view(path, ip=None, user_agent=None, referrer=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO page_views (path, ip, user_agent, referrer) VALUES (?, ?, ?, ?)",
            (path, ip, user_agent, referrer),
        )


def get_page_view_stats(days=30):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        # Total views
        total = db.execute(
            "SELECT COUNT(*) FROM page_views WHERE viewed_at >= ?", (since,)
        ).fetchone()[0]

        # Unique IPs
        unique = db.execute(
            "SELECT COUNT(DISTINCT ip) FROM page_views WHERE viewed_at >= ?", (since,)
        ).fetchone()[0]

        # Views per day
        daily = db.execute(
            """SELECT date(viewed_at) as day, COUNT(*) as views, COUNT(DISTINCT ip) as unique_ips
               FROM page_views WHERE viewed_at >= ?
               GROUP BY day ORDER BY day""",
            (since,),
        ).fetchall()

        # Top pages
        pages = db.execute(
            """SELECT path, COUNT(*) as views
               FROM page_views WHERE viewed_at >= ?
               GROUP BY path ORDER BY views DESC LIMIT 10""",
            (since,),
        ).fetchall()

        # Views per hour (for today)
        today = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00Z")
        hourly = db.execute(
            """SELECT strftime('%H', viewed_at) as hour, COUNT(*) as views
               FROM page_views WHERE viewed_at >= ?
               GROUP BY hour ORDER BY hour""",
            (today,),
        ).fetchall()

        return {
            "total": total,
            "unique_visitors": unique,
            "daily": [dict(r) for r in daily],
            "top_pages": [dict(r) for r in pages],
            "hourly_today": [dict(r) for r in hourly],
        }


def cleanup_old_checks(retention_days=90):
    """Delete check_results and page_views older than retention_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        checks = db.execute("DELETE FROM check_results WHERE checked_at < ?", (cutoff,)).rowcount
        views = db.execute("DELETE FROM page_views WHERE viewed_at < ?", (cutoff,)).rowcount
        if checks + views > 0:
            db.execute("PRAGMA optimize")
        return checks + views


def get_latest_status(service_names):
    if not service_names:
        return {}
    with get_db() as db:
        placeholders = ",".join("?" for _ in service_names)
        rows = db.execute(
            f"""SELECT cr.* FROM check_results cr
                INNER JOIN (
                    SELECT service_name, MAX(checked_at) as max_at
                    FROM check_results
                    WHERE service_name IN ({placeholders})
                    GROUP BY service_name
                ) latest ON cr.service_name = latest.service_name
                           AND cr.checked_at = latest.max_at""",
            list(service_names),
        ).fetchall()
        results = {name: None for name in service_names}
        for row in rows:
            results[row["service_name"]] = dict(row)
        return results


def get_uptime_days(service_name, days=90):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        rows = db.execute(
            """SELECT date(checked_at) as day,
                      COUNT(*) as total,
                      SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) as up_count,
                      SUM(CASE WHEN status != 'up' THEN 1 ELSE 0 END) as down_count,
                      GROUP_CONCAT(
                          CASE WHEN status != 'up' AND error_message IS NOT NULL
                               THEN error_message END,
                          ' | '
                      ) as errors
               FROM check_results
               WHERE service_name = ? AND checked_at >= ?
               GROUP BY day ORDER BY day""",
            (service_name, since),
        ).fetchall()
        return [dict(r) for r in rows]


def get_incident_downtime_hours(service_name, days=90):
    """Calculate total downtime hours from feed incidents for a service.

    Uses created_at and resolved_at of resolved incidents to compute
    actual incident duration.  Falls back to an estimate based on impact
    when timestamps are missing.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        rows = db.execute(
            """SELECT created_at, resolved_at, impact
               FROM incidents
               WHERE service_name = ? AND created_at >= ?
                 AND external_id IS NOT NULL""",
            (service_name, since),
        ).fetchall()
        total_hours = 0.0
        for row in rows:
            if row["created_at"] and row["resolved_at"]:
                try:
                    start = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                    end = datetime.fromisoformat(row["resolved_at"].replace("Z", "+00:00"))
                    hours = (end - start).total_seconds() / 3600.0
                    if hours > 0:
                        total_hours += hours
                        continue
                except (ValueError, TypeError):
                    pass
            # Fallback estimate when timestamps are missing/identical
            impact = row["impact"] or "minor"
            if impact == "critical":
                total_hours += 4.0
            elif impact == "major":
                total_hours += 2.0
            else:
                total_hours += 1.0
        return total_hours


def get_uptime_percentage(service_name, days=90):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        row = db.execute(
            """SELECT COUNT(*) as total,
                      SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) as up_count
               FROM check_results
               WHERE service_name = ? AND checked_at >= ?""",
            (service_name, since),
        ).fetchone()
        if not row or row["total"] == 0:
            check_pct = None
        elif row["total"] < 24:
            check_pct = None
        else:
            check_pct = 100.0 * row["up_count"] / row["total"]

    # Factor in feed incident downtime
    incident_hours = get_incident_downtime_hours(service_name, days)
    total_hours = days * 24.0
    if incident_hours > 0 and total_hours > 0:
        incident_pct = 100.0 * (1.0 - incident_hours / total_hours)
        if check_pct is not None:
            return round(min(check_pct, incident_pct), 2)
        return round(incident_pct, 2)
    if check_pct is not None:
        return round(check_pct, 2)
    return None


def get_active_incidents():
    """Get unresolved incidents (investigating, identified, monitoring)."""
    with get_db() as db:
        incidents = db.execute(
            "SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY created_at DESC"
        ).fetchall()
        result = []
        for inc in incidents:
            inc_dict = dict(inc)
            updates = db.execute(
                "SELECT * FROM incident_updates WHERE incident_id = ? ORDER BY created_at DESC, id DESC",
                (inc["id"],),
            ).fetchall()
            inc_dict["updates"] = [dict(u) for u in updates]
            result.append(inc_dict)
        return result


def get_recent_incidents(limit=10):
    with get_db() as db:
        incidents = db.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for inc in incidents:
            inc_dict = dict(inc)
            updates = db.execute(
                "SELECT * FROM incident_updates WHERE incident_id = ? ORDER BY created_at DESC, id DESC",
                (inc["id"],),
            ).fetchall()
            inc_dict["updates"] = [dict(u) for u in updates]
            result.append(inc_dict)
        return result


def get_active_incident_for_service(service_name):
    """Get the active (unresolved) incident for a specific service, if any."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM incidents WHERE service_name = ? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1",
            (service_name,),
        ).fetchone()
        return dict(row) if row else None


def get_recent_checks(service_name, limit=3):
    """Get the most recent N check results for a service."""
    with get_db() as db:
        rows = db.execute(
            "SELECT status, error_message, checked_at FROM check_results WHERE service_name = ? ORDER BY checked_at DESC LIMIT ?",
            (service_name, limit),
        ).fetchall()
        return [dict(r) for r in rows]


def get_incident_coverage_start():
    """Get the oldest incident date per service_name.

    Returns a dict: {service_name: 'YYYY-MM-DD', ...}
    Used to determine which days have incident feed coverage — days before
    the oldest incident for a service are shown as grey (no data).
    """
    with get_db() as db:
        rows = db.execute(
            "SELECT service_name, MIN(date(created_at)) as oldest FROM incidents WHERE service_name IS NOT NULL GROUP BY service_name"
        ).fetchall()
        return {r["service_name"]: r["oldest"] for r in rows}


def get_check_coverage_start(service_names):
    """Get the earliest check date per service.

    Returns a dict: {service_name: 'YYYY-MM-DD', ...}
    """
    if not service_names:
        return {}
    with get_db() as db:
        placeholders = ",".join("?" for _ in service_names)
        rows = db.execute(
            f"SELECT service_name, MIN(date(checked_at)) as oldest "
            f"FROM check_results WHERE service_name IN ({placeholders}) GROUP BY service_name",
            list(service_names),
        ).fetchall()
        return {r["service_name"]: r["oldest"] for r in rows if r["oldest"]}


def get_incidents_by_day(days=90):
    """Get all incidents in the last N days, mapped to each day they overlap."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    today = datetime.now(timezone.utc).date()
    with get_db() as db:
        rows = db.execute(
            """SELECT id, title, service_name, impact, status, created_at, resolved_at
               FROM incidents
               WHERE created_at >= ? OR (resolved_at IS NULL OR resolved_at >= ?)
               ORDER BY created_at DESC""",
            (since, since),
        ).fetchall()

    by_day = {}
    for row in rows:
        inc = dict(row)
        try:
            start = datetime.fromisoformat(inc["created_at"].replace("Z", "+00:00")).date()
        except (ValueError, TypeError):
            continue
        if inc.get("resolved_at"):
            try:
                end = datetime.fromisoformat(inc["resolved_at"].replace("Z", "+00:00")).date()
            except (ValueError, TypeError):
                end = today
        else:
            end = today

        earliest = today - timedelta(days=days)
        current = max(start, earliest)
        while current <= min(end, today):
            day_str = current.isoformat()
            by_day.setdefault(day_str, []).append(inc)
            current += timedelta(days=1)

    return by_day


def get_incident_by_external_id(external_id):
    """Check if an incident from an external feed already exists."""
    with get_db() as db:
        row = db.execute(
            "SELECT * FROM incidents WHERE external_id = ?", (external_id,)
        ).fetchone()
        return dict(row) if row else None


def create_incident(title, impact="minor", message="Investigating the issue.",
                    service_name=None, external_id=None, created_at=None,
                    resolved_at=None, status="investigating",
                    initial_status=None):
    """Create an incident with its first update.

    ``initial_status`` sets the label on the first update (defaults to
    "investigating").  This is separate from ``status`` which is the
    overall incident state stored on the incidents row.
    """
    first_status = initial_status or "investigating"
    with get_db() as db:
        cursor = db.execute(
            """INSERT INTO incidents (title, impact, service_name, external_id,
               created_at, resolved_at, status)
               VALUES (?, ?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
               ?, ?)""",
            (title, impact, service_name, external_id, created_at, resolved_at, status),
        )
        incident_id = cursor.lastrowid
        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message, created_at) VALUES (?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))",
            (incident_id, first_status, message, created_at),
        )
        return incident_id


def update_incident(incident_id, status, message, created_at=None, resolved_at=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message, created_at) VALUES (?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))",
            (incident_id, status, message, created_at),
        )
        db.execute("UPDATE incidents SET status = ? WHERE id = ?", (status, incident_id))
        if status == "resolved":
            if resolved_at:
                db.execute(
                    "UPDATE incidents SET resolved_at = COALESCE(resolved_at, ?) WHERE id = ?",
                    (resolved_at, incident_id),
                )
            else:
                db.execute(
                    "UPDATE incidents SET resolved_at = COALESCE(resolved_at, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')) WHERE id = ?",
                    (incident_id,),
                )


def backfill_check_gaps(service_names, days=90):
    """Report the number of days with no real check data per service.

    Does NOT insert synthetic records.  Days without checks are left as
    gaps (shown as grey / no-data on the UI).  The only backfill source
    is real incident data already imported from status feeds.

    Returns the number of gap-days detected (for logging purposes only).
    """
    today = datetime.now(timezone.utc).date()
    since = today - timedelta(days=days)
    total_gaps = 0

    with get_db() as db:
        for service_name in service_names:
            existing_rows = db.execute(
                """SELECT DISTINCT date(checked_at) as day
                   FROM check_results
                   WHERE service_name = ? AND checked_at >= ?""",
                (service_name, since.isoformat() + "T00:00:00Z"),
            ).fetchall()
            existing_days = {r["day"] for r in existing_rows}

            current = since
            while current <= today:
                if current.isoformat() not in existing_days:
                    total_gaps += 1
                current += timedelta(days=1)

    return total_gaps


def cleanup_orphan_services(valid_service_names):
    """Delete all check_results and incidents for services not in valid_service_names.

    Accepts a set (or iterable) of service names that should be kept.  Any
    rows in check_results or incidents whose service_name is NOT in that set
    are deleted.

    Returns the total count of deleted rows across both tables.
    """
    valid = set(valid_service_names)
    deleted = 0

    with get_db() as db:
        if not valid:
            # Nothing is valid — delete everything
            deleted += db.execute("DELETE FROM check_results").rowcount
            deleted += db.execute("DELETE FROM incidents").rowcount
        else:
            placeholders = ",".join("?" for _ in valid)
            params = list(valid)
            deleted += db.execute(
                f"DELETE FROM check_results WHERE service_name NOT IN ({placeholders})",
                params,
            ).rowcount
            deleted += db.execute(
                f"DELETE FROM incidents WHERE service_name IS NOT NULL AND service_name NOT IN ({placeholders})",
                params,
            ).rowcount
        if deleted > 0:
            db.execute("PRAGMA optimize")

    return deleted


def get_feed_incident_stats(service_names):
    """Get incident count and date range for a set of feed-covered services.

    Returns a dict: {cnt, oldest, newest}.
    """
    if not service_names:
        return {"cnt": 0, "oldest": None, "newest": None}
    with get_db() as db:
        placeholders = ",".join("?" for _ in service_names)
        row = db.execute(
            f"SELECT COUNT(*) as cnt, MIN(created_at) as oldest, MAX(created_at) as newest "
            f"FROM incidents WHERE external_id IS NOT NULL AND service_name IN ({placeholders})",
            list(service_names),
        ).fetchone()
        return dict(row)

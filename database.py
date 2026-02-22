import logging
import sqlite3
import os
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

DB_PATH = os.environ.get("STATUS_DB", "status.db")


def _make_connection():
    """Create a new SQLite connection with standard settings."""
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    # Enforce relational integrity for incident_updates -> incidents.
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def get_db():
    """Open a short-lived connection (for background tasks and scheduler).

    For request-scoped work, use ``get_request_db()`` instead so all
    queries in a single HTTP request share one connection.
    """
    conn = _make_connection()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def get_request_db():
    """Return a request-scoped connection (stored on Flask ``g``).

    Falls back to a fresh connection when called outside a Flask request
    context (e.g. from the scheduler or CLI scripts).
    """
    try:
        from flask import g, has_app_context

        if has_app_context():
            if "_db" not in g:
                g._db = _make_connection()
            return g._db
    except ImportError:
        pass
    # Outside Flask — return a one-off connection (caller must close)
    return _make_connection()


def close_request_db(exception=None):
    """Teardown handler — close the request-scoped connection."""
    try:
        from flask import g

        db = g.pop("_db", None)
        if db is not None:
            db.commit()
            db.close()
    except ImportError:
        pass


def init_db():
    with get_db() as db:
        try:
            db.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError:
            pass  # WAL already set or DB momentarily locked
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

            CREATE TABLE IF NOT EXISTS incident_updates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                incident_id INTEGER NOT NULL REFERENCES incidents(id),
                status TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            );
            CREATE INDEX IF NOT EXISTS idx_incident_updates_incident
                ON incident_updates(incident_id);
            CREATE INDEX IF NOT EXISTS idx_incidents_external_id
                ON incidents(external_id);
            CREATE INDEX IF NOT EXISTS idx_incidents_service_name
                ON incidents(service_name, resolved_at);
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
        # Schema version tracking for one-time migrations
        db.execute("""
            CREATE TABLE IF NOT EXISTS schema_migrations (
                migration TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
            )
        """)
        # Migrate legacy impact values: "critical" -> "major", "major" -> "partial"
        # Only run once — order matters to avoid critical->major->partial cascade
        row = db.execute(
            "SELECT 1 FROM schema_migrations WHERE migration = 'rename_impact_levels'"
        ).fetchone()
        if not row:
            db.execute("UPDATE incidents SET impact = 'partial' WHERE impact = 'major'")
            db.execute(
                "UPDATE incidents SET impact = 'major' WHERE impact = 'critical'"
            )
            db.execute(
                "INSERT INTO schema_migrations (migration) VALUES ('rename_impact_levels')"
            )


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


def cleanup_old_checks(retention_days=90):
    """Delete check_results older than retention_days."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=retention_days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    with get_db() as db:
        checks = db.execute(
            "DELETE FROM check_results WHERE checked_at < ?", (cutoff,)
        ).rowcount
        if checks > 0:
            db.execute("PRAGMA optimize")
        return checks


def get_latest_status(service_names):
    if not service_names:
        return {}
    db = get_request_db()
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
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db = get_request_db()
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
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db = get_request_db()
    rows = db.execute(
        """SELECT created_at, resolved_at, impact
           FROM incidents
           WHERE service_name = ? AND created_at >= ?
             AND external_id IS NOT NULL""",
        (service_name, since),
    ).fetchall()
    total_hours = 0.0
    intervals = []
    for row in rows:
        if row["created_at"] and row["resolved_at"]:
            try:
                start = datetime.fromisoformat(row["created_at"].replace("Z", "+00:00"))
                end = datetime.fromisoformat(row["resolved_at"].replace("Z", "+00:00"))
                if end > start:
                    intervals.append((start, end))
                    continue
            except (ValueError, TypeError):
                pass
        # Fallback estimate when timestamps are missing/identical
        impact = row["impact"] or "minor"
        if impact == "major":
            total_hours += 4.0
        elif impact == "partial":
            total_hours += 2.0
        else:
            total_hours += 1.0
    if intervals:
        intervals.sort(key=lambda x: x[0])
        merged = [intervals[0]]
        for start, end in intervals[1:]:
            last_start, last_end = merged[-1]
            if start <= last_end:
                merged[-1] = (last_start, max(last_end, end))
            else:
                merged.append((start, end))
        total_hours += sum((end - start).total_seconds() / 3600.0 for start, end in merged)
    return total_hours


def get_uptime_percentage(service_name, days=90):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    db = get_request_db()
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


def _attach_updates(db, incidents):
    """Decorate incident rows with their updates (single batched query)."""
    if not incidents:
        return []
    ids = [inc["id"] for inc in incidents]
    placeholders = ",".join("?" for _ in ids)
    all_updates = db.execute(
        f"SELECT * FROM incident_updates WHERE incident_id IN ({placeholders}) "
        "ORDER BY created_at DESC, id DESC",
        ids,
    ).fetchall()
    updates_by_id = {}
    for u in all_updates:
        updates_by_id.setdefault(u["incident_id"], []).append(dict(u))
    result = []
    for inc in incidents:
        inc_dict = dict(inc)
        inc_dict["updates"] = updates_by_id.get(inc["id"], [])
        result.append(inc_dict)
    return result


def get_active_incidents():
    """Get unresolved incidents (investigating, identified, monitoring)."""
    db = get_request_db()
    incidents = db.execute(
        "SELECT * FROM incidents WHERE resolved_at IS NULL ORDER BY created_at DESC"
    ).fetchall()
    return _attach_updates(db, incidents)


def get_recent_incidents(limit=10):
    db = get_request_db()
    incidents = db.execute(
        "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
    ).fetchall()
    return _attach_updates(db, incidents)


def get_active_incident_for_service(service_name):
    """Get the active (unresolved) incident for a specific service, if any."""
    db = get_request_db()
    row = db.execute(
        "SELECT * FROM incidents WHERE service_name = ? AND resolved_at IS NULL ORDER BY created_at DESC LIMIT 1",
        (service_name,),
    ).fetchone()
    return dict(row) if row else None


def get_recent_checks(service_name, limit=3):
    """Get the most recent N check results for a service."""
    db = get_request_db()
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
    db = get_request_db()
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
    db = get_request_db()
    placeholders = ",".join("?" for _ in service_names)
    rows = db.execute(
        f"SELECT service_name, MIN(date(checked_at)) as oldest "
        f"FROM check_results WHERE service_name IN ({placeholders}) GROUP BY service_name",
        list(service_names),
    ).fetchall()
    return {r["service_name"]: r["oldest"] for r in rows if r["oldest"]}


def get_incidents_by_day(days=90):
    """Get all incidents in the last N days, mapped to each day they overlap."""
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    today = datetime.now(timezone.utc).date()
    db = get_request_db()
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
            start = datetime.fromisoformat(
                inc["created_at"].replace("Z", "+00:00")
            ).date()
        except (ValueError, TypeError):
            continue
        if inc.get("resolved_at"):
            try:
                end = datetime.fromisoformat(
                    inc["resolved_at"].replace("Z", "+00:00")
                ).date()
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
    db = get_request_db()
    row = db.execute(
        "SELECT * FROM incidents WHERE external_id = ?", (external_id,)
    ).fetchone()
    return dict(row) if row else None


def create_incident(
    title,
    impact="minor",
    message="Investigating the issue.",
    service_name=None,
    external_id=None,
    created_at=None,
    resolved_at=None,
    status="investigating",
    initial_status=None,
):
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
        exists = db.execute(
            "SELECT 1 FROM incidents WHERE id = ?", (incident_id,)
        ).fetchone()
        if not exists:
            return False

        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message, created_at) VALUES (?, ?, ?, COALESCE(?, strftime('%Y-%m-%dT%H:%M:%SZ', 'now')))",
            (incident_id, status, message, created_at),
        )
        db.execute(
            "UPDATE incidents SET status = ? WHERE id = ?", (status, incident_id)
        )
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
        return True


_VALID_IMPACTS = {"major", "partial", "minor", "none"}


def update_incident_impact(incident_id, impact):
    """Update an incident's impact level (used when feeds re-classify)."""
    if impact not in _VALID_IMPACTS:
        logger.warning(
            "Rejected invalid impact %r for incident #%s", impact, incident_id
        )
        return
    with get_db() as db:
        db.execute(
            "UPDATE incidents SET impact = ? WHERE id = ?", (impact, incident_id)
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
            db.execute("DELETE FROM incident_updates")
            deleted += db.execute("DELETE FROM incidents").rowcount
        else:
            placeholders = ",".join("?" for _ in valid)
            params = list(valid)
            orphan_ids = [
                r[0]
                for r in db.execute(
                    f"SELECT id FROM incidents WHERE service_name IS NOT NULL AND service_name NOT IN ({placeholders})",
                    params,
                ).fetchall()
            ]
            if orphan_ids:
                upd_placeholders = ",".join("?" for _ in orphan_ids)
                db.execute(
                    f"DELETE FROM incident_updates WHERE incident_id IN ({upd_placeholders})",
                    orphan_ids,
                )
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
    db = get_request_db()
    placeholders = ",".join("?" for _ in service_names)
    row = db.execute(
        f"SELECT COUNT(*) as cnt, MIN(created_at) as oldest, MAX(created_at) as newest "
        f"FROM incidents WHERE external_id IS NOT NULL AND service_name IN ({placeholders})",
        list(service_names),
    ).fetchone()
    return dict(row)

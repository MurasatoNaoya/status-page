import sqlite3
import os
from datetime import datetime, timedelta, timezone
from contextlib import contextmanager

DB_PATH = os.environ.get("STATUS_DB", "status.db")


@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
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
        """)


def record_check(service_name, status, response_time_ms=None, error_message=None):
    with get_db() as db:
        db.execute(
            "INSERT INTO check_results (service_name, status, response_time_ms, error_message) VALUES (?, ?, ?, ?)",
            (service_name, status, response_time_ms, error_message),
        )


def get_latest_status(service_names):
    with get_db() as db:
        results = {}
        for name in service_names:
            row = db.execute(
                "SELECT * FROM check_results WHERE service_name = ? ORDER BY checked_at DESC LIMIT 1",
                (name,),
            ).fetchone()
            results[name] = dict(row) if row else None
        return results


def get_uptime_days(service_name, days=90):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with get_db() as db:
        rows = db.execute(
            """SELECT date(checked_at) as day,
                      COUNT(*) as total,
                      SUM(CASE WHEN status = 'up' THEN 1 ELSE 0 END) as up_count
               FROM check_results
               WHERE service_name = ? AND checked_at >= ?
               GROUP BY day ORDER BY day""",
            (service_name, since),
        ).fetchall()
        return [dict(r) for r in rows]


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
            return None
        return round(100.0 * row["up_count"] / row["total"], 2)


def get_recent_incidents(limit=10):
    with get_db() as db:
        incidents = db.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        result = []
        for inc in incidents:
            inc_dict = dict(inc)
            updates = db.execute(
                "SELECT * FROM incident_updates WHERE incident_id = ? ORDER BY created_at DESC",
                (inc["id"],),
            ).fetchall()
            inc_dict["updates"] = [dict(u) for u in updates]
            result.append(inc_dict)
        return result


def create_incident(title, impact="minor", message="Investigating the issue."):
    with get_db() as db:
        cursor = db.execute(
            "INSERT INTO incidents (title, impact) VALUES (?, ?)", (title, impact)
        )
        incident_id = cursor.lastrowid
        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message) VALUES (?, 'investigating', ?)",
            (incident_id, message),
        )
        return incident_id


def update_incident(incident_id, status, message):
    with get_db() as db:
        db.execute(
            "INSERT INTO incident_updates (incident_id, status, message) VALUES (?, ?, ?)",
            (incident_id, status, message),
        )
        db.execute("UPDATE incidents SET status = ? WHERE id = ?", (status, incident_id))
        if status == "resolved":
            db.execute(
                "UPDATE incidents SET resolved_at = strftime('%Y-%m-%dT%H:%M:%SZ', 'now') WHERE id = ?",
                (incident_id,),
            )

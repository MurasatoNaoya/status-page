"""Shared runtime context for blueprints.

Routes consume this instead of importing `status_page.app` directly.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from typing import Any, Callable, TypedDict


class RuntimeContext(TypedDict):
    """Typed contract between app.py and blueprint routes."""

    # Admin credentials
    admin_user: str
    admin_pass: str

    # Brute-force protection state
    login_failures: dict[str, list[float]]
    login_lock: threading.Lock
    login_window_seconds: int
    login_max_attempts: int

    # CSRF helpers (call with no args, return bool)
    check_form_csrf: Callable[[], bool]
    check_api_csrf: Callable[[], bool]

    # Incident status values
    valid_statuses: set[str]

    # Utility
    mask_email_list: Callable[[str | None], str]
    logger: logging.Logger

    # Data accessors
    all_services: Callable[[], list[dict[str, Any]]]
    status_feeds: Callable[[], list[dict[str, Any]]]
    get_active_incidents: Callable[[], list[dict[str, Any]]]
    get_recent_incidents: Callable[..., list[dict[str, Any]]]
    get_latest_status: Callable[[list[str]], dict[str, Any]]
    get_feed_incident_stats: Callable[..., dict[str, Any]]
    get_feed_backfill_capability: Callable[[dict[str, Any]], dict[str, Any]]
    get_scheduler_health: Callable[[], dict[str, Any]]
    get_db: Callable[[], sqlite3.Connection]
    snapshot: Callable[[], dict[str, Any]]

    # Actions
    build_feed_coverage: Callable[[list[dict[str, Any]]], list[dict[str, Any]]]
    declare_incident_with_alerts: Callable[..., int]
    resolve_incident_with_alerts: Callable[..., bool | None]
    update_incident: Callable[..., bool | None]
    create_incident: Callable[..., int]
    poll_status_feed: Callable[[dict[str, Any]], None]
    send_test_email: Callable[[], bool]
    reload_runtime_config: Callable[[], tuple[bool, str]]
    invalidate_index_cache: Callable[[str | None], None]
    incr: Callable[[str], None]

    # Rendering
    render_index_cached: Callable[[], str]


def _default_runtime_context_provider() -> RuntimeContext:
    return {}  # type: ignore[return-value]


_runtime_context_provider: Callable[[], RuntimeContext] = (
    _default_runtime_context_provider
)


def set_runtime_context_provider(provider: Callable[[], RuntimeContext]) -> None:
    global _runtime_context_provider
    _runtime_context_provider = provider


def get_runtime_context() -> RuntimeContext:
    return _runtime_context_provider()

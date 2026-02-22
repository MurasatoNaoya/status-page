"""Shared utilities for status feed adapters."""

import logging
import re
import threading
from datetime import datetime, timedelta, timezone
from html import unescape

import requests

logger = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers["User-Agent"] = "status-page/1.0"
TIMEOUT = 15

# Backfill capability profile by feed type.
# These describe how far we can import *today* with the current adapter,
# not a guaranteed provider retention contract.
FEED_BACKFILL_CAPS = {
    "statuspage": {
        "ingestion": "Statuspage API (/incidents.json, paginated)",
        "cap_type": "implementation_bounded",
        "known_limit_days": None,
        "cap_summary": (
            "Walks incidents pages up to a configured max page count. "
            "Range varies by provider/account and incident volume."
        ),
    },
    "statusio": {
        "ingestion": "Status.io API + history page scrape",
        "cap_type": "page_limited",
        "known_limit_days": None,
        "cap_summary": (
            "Active incidents via API plus one history page scrape pass. "
            "Range limited by rendered history content."
        ),
    },
    "azure_rss": {
        "ingestion": "Azure RSS + history page scrape",
        "cap_type": "page_limited",
        "known_limit_days": None,
        "cap_summary": (
            "Active incidents via RSS plus one history page scrape pass. "
            "Range limited by Azure history page content."
        ),
    },
    "azure_service_health": {
        "ingestion": "Azure Service Health API",
        "cap_type": "query_limited",
        "known_limit_days": 365,
        "cap_summary": "API supports querying up to 1 year of events per request window.",
    },
}


def get_feed_backfill_capability(feed_config):
    """Return a normalized backfill capability profile for a configured feed."""
    from status_page.feeds.statuspage import _statuspage_max_pages

    feed_type = feed_config.get("type", "statuspage")
    profile = dict(
        FEED_BACKFILL_CAPS.get(
            feed_type,
            {
                "ingestion": "Unknown",
                "cap_type": "unknown",
                "known_limit_days": None,
                "cap_summary": "Unknown feed type; no capability profile available.",
            },
        )
    )
    # Allow explicit per-feed override in config.yaml when operators know
    # a stronger/clearer contractual range for a specific provider.
    if "backfill_cap_days" in feed_config:
        profile["known_limit_days"] = feed_config.get("backfill_cap_days")
    if "backfill_cap_summary" in feed_config:
        profile["cap_summary"] = str(feed_config.get("backfill_cap_summary"))
    if feed_type == "statuspage":
        profile["max_incident_pages"] = _statuspage_max_pages(feed_config)
        if "backfill_cap_summary" not in feed_config:
            profile["cap_summary"] = (
                f"Walks up to {profile['max_incident_pages']} incidents page(s). "
                "Range varies by provider/account and incident volume."
            )
    profile["feed_type"] = feed_type
    return profile


def _strip_html(value):
    """Remove HTML tags and decode entities."""
    if not value:
        return ""
    text = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", unescape(text)).strip()


# Track when each feed's history page was last scraped.
# First run: always scrape. After that: once per day.
_history_last_scraped = {}
_history_lock = threading.Lock()
_HISTORY_SCRAPE_INTERVAL = timedelta(hours=24)


def _should_scrape_history(feed_name):
    """Return True if we should scrape the history page for this feed."""
    with _history_lock:
        last = _history_last_scraped.get(feed_name)
        if last is None:
            return True  # First run — always scrape
        return (datetime.now(timezone.utc) - last) >= _HISTORY_SCRAPE_INTERVAL

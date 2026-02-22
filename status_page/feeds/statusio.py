"""Status.io API adapter (Docker Hub, etc.)."""

import logging
import re
from datetime import datetime, timezone

from status_page.feeds.common import (
    SESSION,
    TIMEOUT,
    _history_last_scraped,
    _history_lock,
    _should_scrape_history,
    _strip_html,
)

logger = logging.getLogger(__name__)


def _statusio_code_to_impact(status_code):
    """Map Status.io status codes to our impact levels."""
    if status_code >= 500:
        return "major"
    elif status_code >= 400:
        return "partial"
    elif status_code >= 300:
        return "minor"
    return "none"


# Map Status.io status codes to Atlassian-style component status strings
_STATUSIO_STATUS_MAP = {
    100: "operational",
    300: "degraded_performance",
    400: "partial_outage",
    500: "major_outage",
    600: "major_outage",
}

_STATUSIO_SEVERITY_TEXT_MAP = {
    "full service disruption": "major",
    "service disruption": "major",
    "security issue": "major",
    "partial service disruption": "partial",
    "degraded performance": "minor",
    "operational": "none",
}

_STATUSIO_UPDATE_STATUS_MAP = {
    "resolved": "resolved",
    "monitoring": "monitoring",
    "identified": "investigating",
    "investigating": "investigating",
    "update": "investigating",
}


def _parse_statusio_history(html, component_map, source_name="Status.io"):
    """Parse incidents from a Status.io history page HTML.

    Returns list of incident dicts in our standard feed format.
    """
    incidents = []
    blocks = re.split(
        r'<div[^>]*class\s*=\s*["\']row incident["\']',
        html,
        flags=re.IGNORECASE,
    )
    if len(blocks) <= 1:
        logger.warning("Status.io history parser found no incident blocks")

    for block in blocks[1:]:
        # ID
        id_m = re.search(r'id="statusio_incident_([a-fA-F0-9]+)"', block)
        inc_id = id_m.group(1) if id_m else None
        if not inc_id:
            continue

        # Title
        title_m = re.search(r"panel-title.*?<a[^>]*>(.*?)</a>", block, re.DOTALL)
        title = _strip_html(title_m.group(1)) if title_m else "Unknown"

        # Severity text
        sev_m = re.search(r'status_description">(.*?)<', block)
        sev_text = sev_m.group(1).strip().lower() if sev_m else "minor"
        impact = _STATUSIO_SEVERITY_TEXT_MAP.get(sev_text, "minor")

        # Components
        comp_m = re.search(
            r'>Components\s*</p>.*?incident_section event_inner_text">(.*?)</p>',
            block,
            re.DOTALL,
        )
        affected = set()
        if comp_m:
            for comp_name in comp_m.group(1).split(","):
                comp_name = comp_name.strip()
                if comp_name in component_map:
                    affected.add(component_map[comp_name])

        # Updates (newest first in HTML)
        updates = []
        update_pattern = (
            r'incident_time">(.*?)</strong>'
            r".*?incident_update_status.*?>(.*?)</strong>"
            r".*?incident_message_details[^>]*>(.*?)</span>"
        )
        for time_html, status_html, msg_html in re.findall(
            update_pattern, block, re.DOTALL
        ):
            # Extract UTC time (second line of the timestamp pair)
            times = re.findall(r"(\w+ \d+, \d{4} \d+:\d+ \w+)", time_html)
            utc_time = times[1] if len(times) > 1 else (times[0] if times else "")
            # Parse to ISO format
            created_at = utc_time
            try:
                dt = datetime.strptime(utc_time, "%B %d, %Y %H:%M %Z")
                created_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                pass

            status_text = re.sub(r"<[^>]+>", "", status_html).strip().lower()
            status = _STATUSIO_UPDATE_STATUS_MAP.get(status_text, "investigating")
            message = _strip_html(msg_html)

            updates.append(
                {
                    "status": status,
                    "message": message[:500],
                    "created_at": created_at,
                }
            )

        # Keep newest-first order (matching Statuspage API convention).
        # poll_status_feed expects updates[-1] = oldest, updates[0] = newest.

        # Determine overall status from updates
        is_resolved = any(u["status"] == "resolved" for u in updates)
        resolved_at = None
        if is_resolved:
            for u in updates:
                if u["status"] == "resolved":
                    resolved_at = u["created_at"]
                    break

        # Oldest update is the created_at (last in newest-first list)
        created_at = updates[-1]["created_at"] if updates else None

        incidents.append(
            {
                "services": list(affected) if affected else None,
                "title": title,
                "status": "resolved" if is_resolved else "investigating",
                "impact": impact,
                "created_at": created_at,
                "resolved_at": resolved_at,
                "external_id": inc_id,
                "source": source_name,
                "updates": updates,
            }
        )

    return incidents


def poll_statusio_api(feed_config):
    """Poll a Status.io API (used by Docker Hub, etc.).

    Returns list of dicts matching our standard feed format.
    Endpoint: https://api.status.io/1.0/status/{page_id}
    """
    url = feed_config["url"]
    component_map = feed_config.get("components", {})
    results = []

    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        data = resp.json().get("result", {})

        # Component statuses
        for comp in data.get("status", []):
            if comp["name"] in component_map:
                our_name = component_map[comp["name"]]
                status_str = _STATUSIO_STATUS_MAP.get(
                    comp["status_code"], "operational"
                )
                results.append(
                    {
                        "type": "component_status",
                        "service_name": our_name,
                        "status": status_str,
                        "source": feed_config["name"],
                    }
                )

        # Active incidents
        for inc in data.get("incidents", []):
            affected = set()
            for comp in inc.get("components_affected", []) or []:
                if comp.get("name") in component_map:
                    affected.add(component_map[comp["name"]])

            # Determine worst impact from messages
            worst_code = 100
            updates = []
            for msg in inc.get("messages", []):
                code = msg.get("status", 100)
                if code > worst_code:
                    worst_code = code
                updates.append(
                    {
                        "status": "resolved" if code == 100 else "investigating",
                        "message": msg.get("details", ""),
                        "created_at": msg.get("datetime"),
                    }
                )

            is_resolved = bool(inc.get("datetime_close"))
            impact = _statusio_code_to_impact(worst_code)

            results.append(
                {
                    "services": list(affected) if affected else None,
                    "title": inc.get("name", "Unknown incident"),
                    "status": "resolved" if is_resolved else "investigating",
                    "impact": impact,
                    "created_at": inc.get("datetime_open"),
                    "resolved_at": inc.get("datetime_close") or None,
                    "external_id": inc.get("_id"),
                    "source": feed_config["name"],
                    "updates": updates,
                }
            )

        # Scrape incident history page for resolved incidents.
        # Status.io's API only returns active incidents, so history page is
        # needed to catch resolved ones. Scraped on first run then once daily.
        history_url = feed_config.get("history_url")
        if history_url and _should_scrape_history(feed_config["name"]):
            try:
                hist_resp = SESSION.get(history_url, timeout=TIMEOUT)
                hist_resp.raise_for_status()
                history_incidents = _parse_statusio_history(
                    hist_resp.text, component_map, feed_config["name"]
                )
                # Don't duplicate incidents already in the API response
                seen_ids = {r.get("external_id") for r in results}
                for inc in history_incidents:
                    if inc["external_id"] not in seen_ids:
                        results.append(inc)
                with _history_lock:
                    _history_last_scraped[feed_config["name"]] = datetime.now(
                        timezone.utc
                    )
                logger.info(
                    "Scraped %d incidents from %s history page",
                    len(history_incidents),
                    feed_config["name"],
                )
            except Exception as e:
                logger.error(
                    "Failed to scrape %s history page: %s", feed_config["name"], e
                )

    except Exception as e:
        logger.error("Failed to poll %s Status.io API: %s", feed_config["name"], e)

    return results

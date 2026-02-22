"""Atlassian Statuspage API adapter (GitHub, Red Hat, etc.)."""

import logging

from status_page.feeds.common import SESSION, TIMEOUT

logger = logging.getLogger(__name__)


def _statuspage_max_pages(feed_config):
    """Return bounded page count for Statuspage incident pagination."""
    raw = feed_config.get("max_incident_pages", 10)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 10
    return max(1, min(100, value))


def _fetch_statuspage_incidents(base_url, max_pages):
    """Fetch paginated Statuspage incidents with duplicate-page protection."""
    incidents = []
    seen_ids = set()
    prev_page_ids = None
    for page in range(1, max_pages + 1):
        resp = SESSION.get(
            f"{base_url}/incidents.json", params={"page": page}, timeout=TIMEOUT
        )
        resp.raise_for_status()
        page_incidents = resp.json().get("incidents", [])
        if not page_incidents:
            break
        page_ids = tuple(inc.get("id") for inc in page_incidents)
        if page_ids and page_ids == prev_page_ids:
            logger.warning(
                "Statuspage incidents page %d repeated; stopping pagination",
                page,
            )
            break
        prev_page_ids = page_ids
        added = 0
        for inc in page_incidents:
            inc_id = inc.get("id")
            if inc_id and inc_id in seen_ids:
                continue
            if inc_id:
                seen_ids.add(inc_id)
            incidents.append(inc)
            added += 1
        if added == 0:
            break
    return incidents


def poll_statuspage_api(feed_config):
    """Poll an Atlassian Statuspage API (GitHub, Red Hat, etc.).

    Returns list of dicts: {service_name, status, incidents: [{title, status, impact, created_at, updates}]}
    """
    base_url = feed_config["url"].rstrip("/")
    component_map = feed_config.get("components", {})
    results = []

    def _match_component(name):
        """Match a component name against our map.

        Handles both exact matches (e.g. "Actions") and prefixed
        sub-components (e.g. "Quay.io - API" matches "Quay.io").
        """
        if name in component_map:
            return component_map[name]
        for ext_name, our_name in component_map.items():
            if name.startswith(ext_name + " "):
                return our_name
        return None

    try:
        # Fetch components for current status
        resp = SESSION.get(f"{base_url}/components.json", timeout=TIMEOUT)
        resp.raise_for_status()
        components = resp.json().get("components", [])

        component_status = {}
        for comp in components:
            matched = _match_component(comp["name"])
            if matched:
                # Statuspage statuses: operational, degraded_performance,
                # partial_outage, major_outage, under_maintenance
                component_status[matched] = comp["status"]

        # Fetch incidents with bounded pagination.
        incidents = _fetch_statuspage_incidents(
            base_url, _statuspage_max_pages(feed_config)
        )

        # Filter to incidents affecting our mapped components
        for inc in incidents:
            affected_components = set()
            for update in inc.get("incident_updates", []):
                for ac in update.get("affected_components", []) or []:
                    matched = _match_component(ac.get("name", ""))
                    if matched:
                        affected_components.add(matched)

            # Also check top-level components field
            for comp in inc.get("components", []) or []:
                matched = _match_component(comp.get("name", ""))
                if matched:
                    affected_components.add(matched)

            if not affected_components:
                # Check if the incident name mentions any of our services
                # Use the base name (before any dot suffix) for broader matching
                inc_name_lower = inc.get("name", "").lower()
                for ext_name, our_name in component_map.items():
                    base_name = ext_name.split(".")[0].lower()
                    if ext_name.lower() in inc_name_lower or (
                        len(base_name) >= 4 and base_name in inc_name_lower
                    ):
                        affected_components.add(our_name)

            if affected_components:
                updates = []
                for upd in inc.get("incident_updates", []):
                    updates.append(
                        {
                            "status": upd["status"],
                            "message": upd.get("body", ""),
                            "created_at": upd["created_at"],
                        }
                    )

                # Map Statuspage.io's impact values to our scheme
                _sp_impact = {"critical": "major", "major": "partial"}.get(
                    inc.get("impact", "minor"), inc.get("impact", "minor")
                )
                results.append(
                    {
                        "services": list(affected_components),
                        "title": inc["name"],
                        "status": inc["status"],
                        "impact": _sp_impact,
                        "created_at": inc["created_at"],
                        "resolved_at": inc.get("resolved_at"),
                        "external_id": inc["id"],
                        "source": feed_config["name"],
                        "updates": updates,
                    }
                )

        # Also return current component status
        for our_name, status in component_status.items():
            results.append(
                {
                    "type": "component_status",
                    "service_name": our_name,
                    "status": status,
                    "source": feed_config["name"],
                }
            )

    except Exception as e:
        logger.error("Failed to poll %s status API: %s", feed_config["name"], e)

    return results

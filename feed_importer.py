"""Import incidents from external status feeds into the database."""

import logging

from database import (
    create_incident,
    get_incident_by_external_id,
    update_incident,
    update_incident_impact,
)
from status_feeds import poll_feed

logger = logging.getLogger(__name__)


def poll_status_feed(feed_config):
    """Poll an external status feed and import incidents."""
    results = poll_feed(feed_config)
    for item in results:
        if item.get("type") == "component_status":
            continue

        ext_id = item.get("external_id")
        if not ext_id:
            continue

        # Determine which services this incident affects
        affected = item.get("services") or []
        if not affected:
            if feed_config.get("components"):
                continue
            affected = [None]

        updates = item.get("updates", [])
        first_msg = updates[-1]["message"] if updates else item["title"]
        first_update_status = (
            updates[-1].get("status", "investigating") if updates else "investigating"
        )

        for svc_name in affected:
            svc_ext_id = f"{ext_id}:{svc_name}" if svc_name else ext_id
            existing = get_incident_by_external_id(svc_ext_id)

            if existing:
                if existing["status"] != item["status"]:
                    update_incident(
                        existing["id"],
                        status=item["status"],
                        message=f"Status changed to {item['status']} (via {item['source']} status page).",
                        resolved_at=item.get("resolved_at"),
                    )
                    logger.info(
                        "Updated incident #%d (%s) to %s",
                        existing["id"],
                        svc_ext_id,
                        item["status"],
                    )
                new_impact = item.get("impact", "minor")
                if existing["impact"] != new_impact:
                    update_incident_impact(existing["id"], new_impact)
                continue

            inc_id = create_incident(
                title=item["title"],
                impact=item.get("impact", "minor"),
                message=first_msg,
                service_name=svc_name,
                external_id=svc_ext_id,
                created_at=item.get("created_at"),
                resolved_at=item.get("resolved_at"),
                status=item.get("status", "investigating"),
                initial_status=first_update_status,
            )

            for upd in reversed(updates[:-1]):
                update_incident(
                    inc_id,
                    status=upd["status"],
                    message=upd["message"],
                    created_at=upd.get("created_at"),
                )

            if item.get("status") == "resolved":
                initial_resolved = (
                    updates[-1]["status"] == "resolved" if updates else False
                )
                has_resolved = initial_resolved or any(
                    u["status"] == "resolved" for u in updates[:-1]
                )
                if not has_resolved:
                    update_incident(
                        inc_id,
                        status="resolved",
                        message=f"Resolved (via {item.get('source', 'external')} status page).",
                        created_at=item.get("resolved_at"),
                        resolved_at=item.get("resolved_at"),
                    )

            logger.info(
                "Imported incident from %s: %s for %s (id=%d)",
                item["source"],
                item["title"],
                svc_name,
                inc_id,
            )

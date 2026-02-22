"""Poll external status page APIs for real incidents and component status."""

import hashlib
import logging
import re
import threading
from html import unescape
import defusedxml.ElementTree as ET
from datetime import datetime, timedelta, timezone

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


# Map keywords in Azure incident titles/descriptions to our service names.
# Covers both currently configured services and common Azure resources
# that may be added in future.
AZURE_SERVICE_KEYWORDS = {
    # Kubernetes / Containers
    "aks": "Azure Kubernetes Service (AKS)",
    "kubernetes": "Azure Kubernetes Service (AKS)",
    "container instance": "Azure Container Instances",
    "container app": "Azure Container Apps",
    "container registry": "Azure Container Registry (ACR)",
    "acr": "Azure Container Registry (ACR)",
    # Identity
    "entra": "Azure AD / Entra ID",
    "managed identity": "Azure AD / Entra ID",
    "active directory": "Azure AD / Entra ID",
    "azure ad": "Azure AD / Entra ID",
    "conditional access": "Azure AD / Entra ID",
    "mfa": "Azure AD / Entra ID",
    # Portal & Management
    "portal": "Azure Portal",
    "arm": "Azure ARM API",
    "resource manager": "Azure ARM API",
    # Storage
    "blob": "Azure Blob Storage (UK South)",
    "storage account": "Azure Blob Storage (UK South)",
    "file share": "Azure Files",
    "table storage": "Azure Table Storage",
    "queue storage": "Azure Queue Storage",
    "data lake": "Azure Data Lake Storage",
    # Databases
    "sql database": "Azure SQL (UK South)",
    "sql server": "Azure SQL (UK South)",
    "cosmos": "Azure Cosmos DB",
    "redis": "Azure Cache for Redis",
    "postgresql": "Azure Database for PostgreSQL",
    "mysql": "Azure Database for MySQL",
    # Networking
    "virtual network": "Azure Virtual Network",
    "vnet": "Azure Virtual Network",
    "load balancer": "Azure Load Balancer",
    "application gateway": "Azure Application Gateway",
    "front door": "Azure Front Door",
    "cdn": "Azure CDN",
    "dns zone": "Azure DNS",
    "traffic manager": "Azure Traffic Manager",
    "firewall": "Azure Firewall",
    "vpn": "Azure VPN Gateway",
    "expressroute": "Azure ExpressRoute",
    "private link": "Azure Private Link",
    "private endpoint": "Azure Private Link",
    # Compute
    "virtual machine": "Azure Virtual Machines",
    "azure vm": "Azure Virtual Machines",
    "app service": "Azure App Service",
    "function app": "Azure Functions",
    "azure functions": "Azure Functions",
    "batch": "Azure Batch",
    # DevOps & CI/CD
    "devops": "Azure DevOps",
    "azure pipelines": "Azure DevOps",
    # Monitoring & Management
    "monitor": "Azure Monitor",
    "log analytics": "Azure Monitor",
    "application insights": "Azure Monitor",
    "key vault": "Azure Key Vault",
    "service bus": "Azure Service Bus",
    "event hub": "Azure Event Hubs",
    "event grid": "Azure Event Grid",
    "logic app": "Azure Logic Apps",
    # AI & ML
    "cognitive": "Azure Cognitive Services",
    "openai": "Azure OpenAI Service",
    "machine learning": "Azure Machine Learning",
}


_AZURE_MAJOR_KEYWORDS = ["outage", "unavailable", "down", "loss of service"]
_AZURE_PARTIAL_KEYWORDS = ["failure", "disruption", "unable", "errors", "not working"]


def _classify_azure_impact(text):
    """Classify impact level from Azure incident text."""
    lower = text.lower()
    if any(w in lower for w in _AZURE_MAJOR_KEYWORDS):
        return "major"
    if any(w in lower for w in _AZURE_PARTIAL_KEYWORDS):
        return "partial"
    return "minor"


def _match_azure_services(title, description=""):
    """Match Azure incident text to our configured service names.

    Uses word-boundary matching for short keywords (<=4 chars) to avoid
    false positives like 'acr' matching 'across'.
    """
    combined = (title + " " + description).lower()
    matched = set()
    for keyword, svc_name in AZURE_SERVICE_KEYWORDS.items():
        if len(keyword) <= 4:
            if re.search(r"\b" + re.escape(keyword) + r"\b", combined):
                matched.add(svc_name)
        else:
            if keyword in combined:
                matched.add(svc_name)
    return list(matched) if matched else None


def poll_azure_rss(feed_config):
    """Poll Azure status RSS feed, filtered by region.

    Returns list of incident dicts.
    """
    url = feed_config["url"]
    exclude_regions = [r.lower() for r in feed_config.get("exclude_regions", [])]
    results = []

    try:
        resp = SESSION.get(url, timeout=TIMEOUT)
        resp.raise_for_status()
        root = ET.fromstring(resp.text)

        for item in root.iter("item"):
            title = item.findtext("title", "")
            description = item.findtext("description", "")
            pub_date = item.findtext("pubDate", "")

            # Skip incidents specific to excluded regions
            combined = (title + " " + description).lower()
            region_specific = any(
                r in combined and "uk south" not in combined for r in exclude_regions
            )
            if region_specific:
                continue

            # Parse date
            created_at = pub_date
            try:
                dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %z")
                created_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                pass

            # Determine impact from title/description keywords
            impact = _classify_azure_impact(combined)

            # Match to our configured Azure services by keywords
            matched_services = _match_azure_services(title, description)

            # Determine status from content — don't assume all RSS items are resolved
            is_resolved = any(
                w in combined
                for w in [
                    "resolved",
                    "mitigated",
                    "remediated",
                    "recovered",
                    "returned to normal",
                    "issue has been fixed",
                ]
            )
            inc_status = "resolved" if is_resolved else "investigating"

            updates = [
                {
                    "status": "investigating",
                    "message": description[:500] if description else title,
                    "created_at": created_at,
                }
            ]
            if is_resolved:
                updates.append(
                    {
                        "status": "resolved",
                        "message": "Incident resolved.",
                        "created_at": created_at,
                    }
                )

            results.append(
                {
                    "services": matched_services,
                    "title": title,
                    "status": inc_status,
                    "impact": impact,
                    "created_at": created_at,
                    "resolved_at": created_at if is_resolved else None,
                    "external_id": f"azure-rss-{hashlib.sha256((title + pub_date).encode()).hexdigest()[:16]}",
                    "source": "Azure",
                    "updates": updates,
                }
            )

    except Exception as e:
        logger.error("Failed to poll Azure RSS: %s", e)

    # Scrape history page for past incidents (initial import + daily refresh).
    # The RSS feed only contains active incidents, so resolved ones are lost.
    history_url = feed_config.get("history_url")
    if history_url and _should_scrape_history(feed_config["name"]):
        try:
            hist_resp = SESSION.get(history_url, timeout=TIMEOUT)
            hist_resp.raise_for_status()
            history_incidents = _parse_azure_history(hist_resp.text, exclude_regions)
            seen_ids = {r.get("external_id") for r in results}
            for inc in history_incidents:
                if inc["external_id"] not in seen_ids:
                    results.append(inc)
            with _history_lock:
                _history_last_scraped[feed_config["name"]] = datetime.now(timezone.utc)
            logger.info(
                "Scraped %d incidents from Azure history page", len(history_incidents)
            )
        except Exception as e:
            logger.error("Failed to scrape Azure history page: %s", e)

    return results


def _parse_azure_history(html, exclude_regions=None):
    """Parse incidents from the Azure status history page.

    Returns list of incident dicts in our standard feed format.
    Uses _match_azure_services() for service matching and applies
    the same region exclusion as the RSS feed.
    """
    if exclude_regions is None:
        exclude_regions = []
    exclude_lower = [r.lower() for r in exclude_regions]
    incidents = []

    blocks = re.split(
        r'class\s*=\s*["\']row incident-history-header["\']',
        html,
        flags=re.IGNORECASE,
    )
    if len(blocks) <= 1:
        logger.warning("Azure history parser found no incident blocks")

    for block in blocks[1:]:
        # Tracking ID
        tid_m = re.search(r"Tracking ID:\s*([^<\s]+)", block)
        tid = tid_m.group(1) if tid_m else None
        if not tid:
            continue

        # Title
        title_m = re.search(
            r"incident-history-title[^>]*>(.*?)</div>", block, re.DOTALL
        )
        title = _strip_html(title_m.group(1)) if title_m else "Unknown"

        # Body text
        body_m = re.search(r"card-body[^>]*>(.*?)</div>\s*</div>", block, re.DOTALL)
        body = _strip_html(body_m.group(1)) if body_m else ""

        combined = (title + " " + body).lower()
        title_lower = title.lower()

        # Region filtering: skip incidents specific to excluded regions
        # but keep global/multi-region incidents.
        # If the title itself mentions an excluded region (e.g. "affecting Azure Government"),
        # treat it as region-specific even if the body says "all regions".
        title_region_specific = any(r in title_lower for r in exclude_lower)
        if title_region_specific:
            continue
        is_global = any(
            kw in combined for kw in ["all regions", "multiple regions", "global"]
        )
        region_specific = any(r in combined for r in exclude_lower)
        if region_specific and not is_global:
            continue

        # Strip video link preamble (e.g. "Watch our 'Azure Incident
        # Retrospective' video about this incident: https://... What happened?")
        clean_body = re.sub(
            r"Watch our\s+.*?What happened\?\s*", "", body, count=1, flags=re.IGNORECASE
        )
        if not clean_body.strip():
            clean_body = body  # fallback if regex ate everything

        # Extract start and end times from PIR body.
        # Pattern 1: "Between HH:MM UTC on DD Month YYYY and HH:MM UTC on DD Month YYYY"
        # Pattern 2: "Between HH:MM UTC and HH:MM UTC on DD Month YYYY" (same day)
        created_at = None
        resolved_at = None
        span_m = re.search(
            r"[Bb]etween\s+(\d{1,2}:\d{2})\s*(?:\xa0)?UTC\s+on\s+(\d{1,2}\s+\w+\s+\d{4})"
            r"\s+and\s+(\d{1,2}:\d{2})\s*(?:\xa0)?UTC\s+on\s+(\d{1,2}\s+\w+\s+\d{4})",
            body,
        )
        if span_m:
            try:
                dt_start = datetime.strptime(
                    f"{span_m.group(2)} {span_m.group(1)}", "%d %B %Y %H:%M"
                )
                dt_end = datetime.strptime(
                    f"{span_m.group(4)} {span_m.group(3)}", "%d %B %Y %H:%M"
                )
                created_at = dt_start.strftime("%Y-%m-%dT%H:%M:%SZ")
                resolved_at = dt_end.strftime("%Y-%m-%dT%H:%M:%SZ")
            except (ValueError, TypeError):
                pass
        if not created_at:
            # Same-day: "Between HH:MM UTC and HH:MM UTC on DD Month YYYY"
            same_m = re.search(
                r"[Bb]etween\s+(\d{1,2}:\d{2})\s*(?:\xa0)?UTC\s+and\s+(\d{1,2}:\d{2})\s*(?:\xa0)?UTC"
                r"\s+on\s+(\d{1,2}\s+\w+\s+\d{4})",
                body,
            )
            if same_m:
                try:
                    dt_start = datetime.strptime(
                        f"{same_m.group(3)} {same_m.group(1)}", "%d %B %Y %H:%M"
                    )
                    dt_end = datetime.strptime(
                        f"{same_m.group(3)} {same_m.group(2)}", "%d %B %Y %H:%M"
                    )
                    created_at = dt_start.strftime("%Y-%m-%dT%H:%M:%SZ")
                    resolved_at = dt_end.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    pass
        if not created_at:
            # Fallback: single timestamp "HH:MM UTC on DD Month YYYY"
            date_m = re.search(
                r"(\d{1,2}:\d{2})\s*(?:\xa0)?UTC\s+on\s+(\d{1,2}\s+\w+\s+\d{4})", body
            )
            if date_m:
                try:
                    dt = datetime.strptime(
                        f"{date_m.group(2)} {date_m.group(1)}", "%d %B %Y %H:%M"
                    )
                    created_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    pass
        if not created_at:
            # Last resort: bare date "08 December 2025"
            date_m2 = re.search(r"(\d{1,2}\s+\w+\s+\d{4})", body)
            if date_m2:
                try:
                    dt = datetime.strptime(date_m2.group(1), "%d %B %Y")
                    created_at = dt.strftime("%Y-%m-%dT%H:%M:%SZ")
                except (ValueError, TypeError):
                    pass
        if not resolved_at:
            resolved_at = created_at

        # Determine impact from keywords
        impact = _classify_azure_impact(combined)

        # Match to configured Azure services
        matched_services = _match_azure_services(title, body)

        # Single resolved update with the PIR summary.
        # poll_status_feed uses updates[-1] as the initial message.
        summary = clean_body[:500].strip()
        incidents.append(
            {
                "services": matched_services,
                "title": title,
                "status": "resolved",
                "impact": impact,
                "created_at": created_at,
                "resolved_at": resolved_at,
                "external_id": f"azure-pir-{tid}",
                "source": "Azure",
                "updates": [
                    {
                        "status": "resolved",
                        "message": summary,
                        "created_at": created_at,
                    }
                ],
            }
        )

    return incidents


# ---------------------------------------------------------------------------
# Azure Service Health API (commented out — requires Azure SPN credentials)
# ---------------------------------------------------------------------------
# This replaces the RSS feed with proper Azure Service Health events, giving:
#   - Real severity levels (Critical, Error, Warning, Informational)
#   - Lifecycle updates with timestamps (investigating → mitigated → resolved)
#   - Region-filtered incidents (e.g. only "UK South")
#   - Impact start/end times
#
# Incidents are region-based, not subscription-based. You only need Reader
# on ONE subscription in your tenant — the same regional incidents appear
# regardless of which subscription you query from.
#
# Prerequisites:
#   1. pip install azure-identity
#   2. Create a service principal with Reader on any one subscription:
#        az ad sp create-for-rbac --name "status-page-reader" --role Reader \
#            --scopes /subscriptions/<ANY_SUBSCRIPTION_ID>
#   3. Set environment variables:
#        AZURE_TENANT_ID=<tenant-id>
#        AZURE_CLIENT_ID=<client-id>
#        AZURE_CLIENT_SECRET=<client-secret>
#   4. In config.yaml, change the Azure feed to:
#        - name: "Azure"
#          type: azure_service_health
#          subscription_id: "<any-subscription-id>"
#          region: "UK South"
#          interval: 300
#
# _AZURE_API_VERSION = "2025-05-01"
# _AZURE_BASE = "https://management.azure.com"
#
# # Maps Azure eventLevel to our impact scale
# _AZURE_LEVEL_TO_IMPACT = {
#     "Critical": "major",
#     "Error": "partial",
#     "Warning": "minor",
#     "Informational": "minor",
# }
#
#
# def poll_azure_service_health(feed_config):
#     """Poll Azure Service Health API for incidents affecting your subscription.
#
#     Returns list of incident dicts matching our standard format.
#     Requires azure-identity package and SPN environment variables.
#     """
#     from azure.identity import DefaultAzureCredential
#
#     subscription_id = feed_config["subscription_id"]
#     region_filter = feed_config.get("region")  # e.g. "UK South"
#     results = []
#
#     try:
#         credential = DefaultAzureCredential()
#         token = credential.get_token("https://management.azure.com/.default").token
#         headers = {"Authorization": f"Bearer {token}"}
#
#         # Query events from the last 90 days (max 1 year)
#         from datetime import datetime, timedelta
#         start = (datetime.utcnow() - timedelta(days=90)).strftime("%-m/%-d/%Y")
#         url = (
#             f"{_AZURE_BASE}/subscriptions/{subscription_id}"
#             f"/providers/Microsoft.ResourceHealth/events"
#         )
#         params = {
#             "api-version": _AZURE_API_VERSION,
#             "queryStartTime": start,
#             # Only fetch service issues (not maintenance or advisories)
#             "$filter": "properties/eventType eq 'ServiceIssue'",
#         }
#
#         all_events = []
#         while url:
#             resp = SESSION.get(url, headers=headers, params=params, timeout=TIMEOUT)
#             resp.raise_for_status()
#             data = resp.json()
#             all_events.extend(data.get("value", []))
#             url = data.get("nextLink")
#             params = {}  # nextLink already includes query params
#
#         for event in all_events:
#             props = event.get("properties", {})
#
#             # Filter by region if configured
#             affected_regions = []
#             matched_services = []
#             for svc in props.get("impact", []):
#                 for rgn in svc.get("impactedRegions", []):
#                     rgn_name = rgn.get("impactedRegion", "")
#                     if region_filter and region_filter.lower() not in rgn_name.lower():
#                         continue
#                     affected_regions.append(rgn)
#                     matched_services.append(svc.get("impactedService", ""))
#
#             if region_filter and not affected_regions:
#                 continue  # incident doesn't affect our region
#
#             # Map severity
#             impact = _AZURE_LEVEL_TO_IMPACT.get(props.get("eventLevel", ""), "minor")
#
#             # Map status
#             is_resolved = props.get("status") == "Resolved"
#
#             # Build lifecycle updates from the region-level updates
#             updates = []
#             for rgn in affected_regions:
#                 for upd in rgn.get("updates", []):
#                     updates.append({
#                         "status": "resolved" if "mitigated" in upd.get("summary", "").lower()
#                                   or "resolved" in upd.get("summary", "").lower()
#                                   else "investigating",
#                         "message": upd.get("summary", ""),
#                         "created_at": upd.get("updateDateTime"),
#                     })
#
#             # Deduplicate and sort updates by time
#             seen_msgs = set()
#             unique_updates = []
#             for u in sorted(updates, key=lambda x: x.get("created_at", "")):
#                 msg_key = u["message"][:100]
#                 if msg_key not in seen_msgs:
#                     seen_msgs.add(msg_key)
#                     unique_updates.append(u)
#
#             # If resolved but no resolved update, add one
#             if is_resolved and not any(u["status"] == "resolved" for u in unique_updates):
#                 unique_updates.append({
#                     "status": "resolved",
#                     "message": "Incident resolved.",
#                     "created_at": props.get("impactMitigationTime"),
#                 })
#
#             # Match to our configured service names
#             svc_names = _match_azure_services(
#                 props.get("title", ""),
#                 props.get("summary", ""),
#             )
#
#             tracking_id = event.get("name", "")  # e.g. "BC_1-FXZ"
#
#             results.append({
#                 "services": svc_names or matched_services or None,
#                 "title": props.get("title", "Unknown Azure incident"),
#                 "status": "resolved" if is_resolved else "investigating",
#                 "impact": impact,
#                 "created_at": props.get("impactStartTime"),
#                 "resolved_at": props.get("impactMitigationTime"),
#                 "external_id": f"azure-health-{tracking_id}",
#                 "source": "Azure",
#                 "updates": unique_updates or [{
#                     "status": "investigating",
#                     "message": props.get("summary", props.get("title", "")),
#                     "created_at": props.get("impactStartTime"),
#                 }],
#             })
#
#     except ImportError:
#         logger.error("azure-identity package not installed. Run: pip install azure-identity")
#     except Exception as e:
#         logger.error("Failed to poll Azure Service Health API: %s", e)
#
#     return results


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


def poll_feed(feed_config):
    """Poll a single status feed based on its type."""
    feed_type = feed_config.get("type", "statuspage")
    if feed_type == "azure_rss":
        return poll_azure_rss(feed_config)
    elif feed_type == "statusio":
        return poll_statusio_api(feed_config)
    # elif feed_type == "azure_service_health":
    #     return poll_azure_service_health(feed_config)
    else:
        return poll_statuspage_api(feed_config)

"""Azure status feed adapters (RSS + history page scraping)."""

import hashlib
import logging
import re
from datetime import datetime, timezone

import defusedxml.ElementTree as ET

from status_page.feeds.common import (
    SESSION,
    TIMEOUT,
    _history_last_scraped,
    _history_lock,
    _should_scrape_history,
    _strip_html,
)

logger = logging.getLogger(__name__)

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
#   - Lifecycle updates with timestamps (investigating -> mitigated -> resolved)
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

"""Backward-compatible re-exports from status_page.feeds.

All feed adapter logic now lives in the status_page/feeds/ package.
This module re-exports the public API so existing imports keep working.
"""

# Public API
from status_page.feeds import poll_feed  # noqa: F401
from status_page.feeds.common import (  # noqa: F401
    FEED_BACKFILL_CAPS,
    SESSION,
    TIMEOUT,
    _should_scrape_history,
    _strip_html,
    get_feed_backfill_capability,
)

# Statuspage adapter
from status_page.feeds.statuspage import (  # noqa: F401
    _fetch_statuspage_incidents,
    _statuspage_max_pages,
    poll_statuspage_api,
)

# Azure adapter
from status_page.feeds.azure import (  # noqa: F401
    AZURE_SERVICE_KEYWORDS,
    _classify_azure_impact,
    _match_azure_services,
    _parse_azure_history,
    poll_azure_rss,
)

# Status.io adapter
from status_page.feeds.statusio import (  # noqa: F401
    _STATUSIO_SEVERITY_TEXT_MAP,
    _STATUSIO_STATUS_MAP,
    _STATUSIO_UPDATE_STATUS_MAP,
    _parse_statusio_history,
    _statusio_code_to_impact,
    poll_statusio_api,
)

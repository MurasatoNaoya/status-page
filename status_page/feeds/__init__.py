"""Status feed adapters — poll external status pages for incidents."""

from status_page.feeds.azure import poll_azure_rss
from status_page.feeds.common import get_feed_backfill_capability
from status_page.feeds.statusio import poll_statusio_api
from status_page.feeds.statuspage import poll_statuspage_api


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


__all__ = [
    "poll_feed",
    "get_feed_backfill_capability",
    "poll_azure_rss",
    "poll_statusio_api",
    "poll_statuspage_api",
]

"""Search Reddit and X/Twitter for outage chatter when a service goes down."""

import logging
import os
import time
from datetime import datetime, timezone

import requests

logger = logging.getLogger(__name__)

REDDIT_HEADERS = {"User-Agent": "status-page/1.0"}
REDDIT_TIMEOUT = 10

# Optional: set TWITTER_BEARER_TOKEN env var for X/Twitter search
TWITTER_BEARER = os.environ.get("TWITTER_BEARER_TOKEN")


def search_reddit(query, limit=5, timeframe="day"):
    """Search Reddit for recent posts matching query. Free, no auth needed."""
    try:
        resp = requests.get(
            "https://www.reddit.com/search.json",
            params={"q": query, "sort": "new", "t": timeframe, "limit": limit},
            headers=REDDIT_HEADERS,
            timeout=REDDIT_TIMEOUT,
        )
        if resp.status_code != 200:
            return []

        posts = resp.json().get("data", {}).get("children", [])
        results = []
        for p in posts:
            d = p["data"]
            results.append({
                "source": "reddit",
                "subreddit": d.get("subreddit", ""),
                "title": d.get("title", ""),
                "url": f"https://reddit.com{d.get('permalink', '')}",
                "score": d.get("score", 0),
                "num_comments": d.get("num_comments", 0),
                "created": datetime.fromtimestamp(
                    d.get("created_utc", 0), tz=timezone.utc
                ).strftime("%Y-%m-%d %H:%M UTC"),
            })
        return results
    except Exception as e:
        logger.warning("Reddit search failed: %s", e)
        return []


def search_twitter(query, limit=5):
    """Search X/Twitter for recent tweets. Requires TWITTER_BEARER_TOKEN env var."""
    if not TWITTER_BEARER:
        return []
    try:
        resp = requests.get(
            "https://api.twitter.com/2/tweets/search/recent",
            params={"query": f"{query} -is:retweet", "max_results": min(limit, 100)},
            headers={"Authorization": f"Bearer {TWITTER_BEARER}"},
            timeout=10,
        )
        if resp.status_code != 200:
            return []

        tweets = resp.json().get("data", [])
        return [
            {
                "source": "twitter",
                "title": t.get("text", "")[:200],
                "url": f"https://twitter.com/i/web/status/{t['id']}",
            }
            for t in tweets
        ]
    except Exception as e:
        logger.warning("Twitter search failed: %s", e)
        return []


def search_outage_chatter(service_name, keywords=None):
    """Search social media for outage reports related to a service.

    Args:
        service_name: The service that's down (e.g. "Azure Blob Storage")
        keywords: Optional list of extra search terms. If not provided,
                  derives from service_name.
    Returns:
        List of social media posts about the outage.
    """
    if keywords:
        query = " OR ".join(keywords)
    else:
        # Build a search query from the service name
        # e.g. "Azure Blob Storage (UK South)" -> "azure blob storage outage"
        clean = service_name.split("(")[0].strip()
        query = f"{clean} outage OR down OR issue"

    results = []
    results.extend(search_reddit(query, limit=5))
    results.extend(search_twitter(query, limit=5))

    # Sort by relevance (score for reddit, recency)
    results.sort(key=lambda r: r.get("score", 0), reverse=True)
    return results

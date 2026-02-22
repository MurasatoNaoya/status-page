"""Tests for status_feeds.py — external status feed polling."""

from unittest.mock import patch, MagicMock

from status_page.status_feeds import (
    poll_statuspage_api,
    poll_azure_rss,
    poll_feed,
    get_feed_backfill_capability,
    _parse_azure_history,
    _match_azure_services,
    _parse_statusio_history,
)


MOCK_COMPONENTS_JSON = {
    "components": [
        {"name": "Actions", "status": "operational"},
        {"name": "Git Operations", "status": "operational"},
        {"name": "API Requests", "status": "degraded_performance"},
    ]
}

MOCK_INCIDENTS_JSON = {
    "incidents": [
        {
            "id": "abc123",
            "name": "Incident with Actions",
            "status": "resolved",
            "impact": "partial",
            "created_at": "2026-02-01T10:00:00Z",
            "resolved_at": "2026-02-01T12:00:00Z",
            "components": [{"name": "Actions"}],
            "incident_updates": [
                {
                    "status": "resolved",
                    "body": "This has been resolved.",
                    "created_at": "2026-02-01T12:00:00Z",
                    "affected_components": [{"name": "Actions"}],
                },
                {
                    "status": "investigating",
                    "body": "We are investigating.",
                    "created_at": "2026-02-01T10:00:00Z",
                    "affected_components": [{"name": "Actions"}],
                },
            ],
        }
    ]
}


class TestPollStatuspageAPI:
    def test_parses_incidents(self):
        feed = {
            "name": "GitHub",
            "url": "https://www.githubstatus.com/api/v2",
            "components": {"Actions": "GitHub Actions"},
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                if "components.json" in url:
                    resp.json.return_value = MOCK_COMPONENTS_JSON
                elif "incidents.json" in url:
                    resp.json.return_value = MOCK_INCIDENTS_JSON
                resp.raise_for_status = MagicMock()
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 1
        assert incidents[0]["title"] == "Incident with Actions"
        assert incidents[0]["external_id"] == "abc123"
        assert "GitHub Actions" in incidents[0]["services"]
        assert len(incidents[0]["updates"]) == 2

    def test_returns_component_status(self):
        feed = {
            "name": "GitHub",
            "url": "https://www.githubstatus.com/api/v2",
            "components": {"Actions": "GitHub Actions"},
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                if "components.json" in url:
                    resp.json.return_value = MOCK_COMPONENTS_JSON
                else:
                    resp.json.return_value = {"incidents": []}
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        statuses = [r for r in results if r.get("type") == "component_status"]
        assert len(statuses) == 1
        assert statuses[0]["service_name"] == "GitHub Actions"

    def test_skips_unmapped_components(self):
        feed = {
            "name": "GitHub",
            "url": "https://www.githubstatus.com/api/v2",
            "components": {"SomethingElse": "Mapped"},
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                if "components.json" in url:
                    resp.json.return_value = MOCK_COMPONENTS_JSON
                else:
                    resp.json.return_value = MOCK_INCIDENTS_JSON
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 0

    def test_handles_api_error(self):
        feed = {
            "name": "GitHub",
            "url": "https://www.githubstatus.com/api/v2",
            "components": {"Actions": "GitHub Actions"},
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.side_effect = Exception("Network error")
            results = poll_statuspage_api(feed)

        assert results == []

    def test_paginates_incidents_pages(self):
        feed = {
            "name": "GitHub",
            "url": "https://www.githubstatus.com/api/v2",
            "components": {"Actions": "GitHub Actions"},
            "max_incident_pages": 3,
        }
        calls = {"incidents": 0}

        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.status_code = 200
                resp.raise_for_status = MagicMock()
                if "components.json" in url:
                    resp.json.return_value = MOCK_COMPONENTS_JSON
                    return resp
                if "incidents.json" in url:
                    calls["incidents"] += 1
                    page = kwargs.get("params", {}).get("page")
                    if page == 1:
                        resp.json.return_value = {
                            "incidents": [
                                {
                                    "id": "p1",
                                    "name": "Incident one",
                                    "status": "resolved",
                                    "impact": "minor",
                                    "created_at": "2026-02-01T10:00:00Z",
                                    "components": [{"name": "Actions"}],
                                    "incident_updates": [],
                                }
                            ]
                        }
                    elif page == 2:
                        resp.json.return_value = {
                            "incidents": [
                                {
                                    "id": "p2",
                                    "name": "Incident two",
                                    "status": "resolved",
                                    "impact": "minor",
                                    "created_at": "2026-01-01T10:00:00Z",
                                    "components": [{"name": "Actions"}],
                                    "incident_updates": [],
                                }
                            ]
                        }
                    else:
                        resp.json.return_value = {"incidents": []}
                    return resp
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 2
        assert calls["incidents"] == 3


class TestFeedBackfillCapability:
    def test_statuspage_profile_defaults(self):
        profile = get_feed_backfill_capability({"name": "GitHub"})
        assert profile["feed_type"] == "statuspage"
        assert profile["cap_type"] == "implementation_bounded"
        assert profile["known_limit_days"] is None
        assert profile["max_incident_pages"] == 10

    def test_override_profile_from_config(self):
        profile = get_feed_backfill_capability(
            {
                "name": "Docker",
                "type": "statusio",
                "backfill_cap_days": 180,
                "backfill_cap_summary": "Provider advertises 180-day history.",
            }
        )
        assert profile["feed_type"] == "statusio"
        assert profile["known_limit_days"] == 180
        assert profile["cap_summary"] == "Provider advertises 180-day history."

    def test_matches_prefixed_subcomponents(self):
        """Components like 'Quay.io - API' should match config key 'Quay.io'."""
        feed = {
            "name": "Red Hat",
            "url": "https://status.redhat.com/api/v2",
            "components": {"Quay.io": "Red Hat Quay.io"},
        }
        mock_incidents = {
            "incidents": [
                {
                    "id": "abc456",
                    "name": "Intermittent Pull & Push Failure",
                    "status": "resolved",
                    "impact": "major",
                    "created_at": "2026-01-05T18:00:00Z",
                    "resolved_at": "2026-01-05T22:00:00Z",
                    "components": [{"name": "API"}, {"name": "Registry"}],
                    "incident_updates": [
                        {
                            "status": "resolved",
                            "body": "Resolved.",
                            "created_at": "2026-01-05T22:00:00Z",
                            "affected_components": [
                                {"name": "Quay.io - API"},
                                {"name": "Quay.io - Registry"},
                            ],
                        }
                    ],
                }
            ]
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                if "components.json" in url:
                    resp.json.return_value = {"components": []}
                else:
                    resp.json.return_value = mock_incidents
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 1
        assert "Red Hat Quay.io" in incidents[0]["services"]

    def test_name_fallback_matches_base_name(self):
        """Incident mentioning 'Quay' should match config key 'Quay.io'."""
        feed = {
            "name": "Red Hat",
            "url": "https://status.redhat.com/api/v2",
            "components": {"Quay.io": "Red Hat Quay.io"},
        }
        mock_incidents = {
            "incidents": [
                {
                    "id": "def789",
                    "name": "Cascading failures that depend on Quay and AWS",
                    "status": "resolved",
                    "impact": "major",
                    "created_at": "2025-10-20T08:00:00Z",
                    "resolved_at": "2025-10-20T22:00:00Z",
                    "components": [{"name": "OpenShift Cluster Manager"}],
                    "incident_updates": [],
                }
            ]
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                if "components.json" in url:
                    resp.json.return_value = {"components": []}
                else:
                    resp.json.return_value = mock_incidents
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 1
        assert "Red Hat Quay.io" in incidents[0]["services"]

    def test_short_base_name_does_not_match(self):
        """Base names shorter than 4 chars should not false-positive match."""
        feed = {
            "name": "Test",
            "url": "https://example.com/api/v2",
            "components": {"AB.io": "Short Service"},
        }
        mock_incidents = {
            "incidents": [
                {
                    "id": "short1",
                    "name": "Absolute connectivity breakdown",
                    "status": "resolved",
                    "impact": "minor",
                    "created_at": "2026-01-01T00:00:00Z",
                    "resolved_at": "2026-01-01T01:00:00Z",
                    "components": [],
                    "incident_updates": [],
                }
            ]
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:

            def mock_response(url, **kwargs):
                resp = MagicMock()
                resp.raise_for_status = MagicMock()
                if "components.json" in url:
                    resp.json.return_value = {"components": []}
                else:
                    resp.json.return_value = mock_incidents
                return resp

            mock_get.side_effect = mock_response
            results = poll_statuspage_api(feed)

        incidents = [r for r in results if r.get("type") != "component_status"]
        assert len(incidents) == 0


MOCK_AZURE_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
<channel>
    <item>
        <title>Azure VM and AKS Failures - UK South</title>
        <description>Multiple services affected in UK South region</description>
        <pubDate>Sun, 02 Feb 2026 08:00:00 +0000</pubDate>
    </item>
    <item>
        <title>Storage outage - West US 2</title>
        <description>Blob storage unavailable in West US 2</description>
        <pubDate>Sun, 01 Feb 2026 08:00:00 +0000</pubDate>
    </item>
</channel>
</rss>"""


class TestPollAzureRSS:
    def test_parses_rss_items(self):
        feed = {
            "name": "Azure",
            "url": "https://azure.status.microsoft/feed",
            "type": "azure_rss",
            "exclude_regions": [],
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = MOCK_AZURE_RSS
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp
            results = poll_azure_rss(feed)

        assert len(results) == 2
        assert results[0]["source"] == "Azure"
        assert "AKS" in results[0]["title"]

    def test_excludes_regions(self):
        feed = {
            "name": "Azure",
            "url": "https://azure.status.microsoft/feed",
            "type": "azure_rss",
            "exclude_regions": ["West US 2"],
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = MOCK_AZURE_RSS
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp
            results = poll_azure_rss(feed)

        # West US 2 item should be excluded, UK South kept
        titles = [r["title"] for r in results]
        assert any("UK South" in t for t in titles)
        assert not any("West US 2" in t for t in titles)

    def test_determines_impact_from_title(self):
        feed = {
            "name": "Azure",
            "url": "https://azure.status.microsoft/feed",
            "type": "azure_rss",
            "exclude_regions": [],
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = MOCK_AZURE_RSS
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp
            results = poll_azure_rss(feed)

        # "outage" maps to "major" (most severe keyword match)
        outage_items = [r for r in results if "outage" in r["title"].lower()]
        assert all(r["impact"] == "major" for r in outage_items)

    def test_handles_rss_error(self):
        feed = {
            "name": "Azure",
            "url": "https://azure.status.microsoft/feed",
            "type": "azure_rss",
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            mock_get.side_effect = Exception("SSL error")
            results = poll_azure_rss(feed)

        assert results == []


class TestPollFeed:
    def test_dispatches_to_statuspage(self):
        with patch("status_page.status_feeds.poll_statuspage_api") as mock:
            mock.return_value = []
            poll_feed({"name": "GitHub", "url": "https://example.com"})
            mock.assert_called_once()

    def test_dispatches_to_azure_rss(self):
        with patch("status_page.status_feeds.poll_azure_rss") as mock:
            mock.return_value = []
            poll_feed(
                {"name": "Azure", "type": "azure_rss", "url": "https://example.com"}
            )
            mock.assert_called_once()


class TestMatchAzureServices:
    """Test keyword-based Azure service matching with word boundary logic."""

    def test_matches_aks_keyword(self):
        result = _match_azure_services("AKS cluster failures in UK South")
        assert "Azure Kubernetes Service (AKS)" in result

    def test_matches_long_keyword_substring(self):
        result = _match_azure_services("container registry unavailable")
        assert "Azure Container Registry (ACR)" in result

    def test_short_keyword_uses_word_boundary(self):
        """'acr' should NOT match 'across' — word boundary prevents it."""
        result = _match_azure_services("Issues across multiple regions")
        assert result is None or "Azure Container Registry (ACR)" not in (result or [])

    def test_short_keyword_matches_as_whole_word(self):
        """'acr' should match when it appears as a standalone word."""
        result = _match_azure_services("ACR push failures detected")
        assert result is not None
        assert "Azure Container Registry (ACR)" in result

    def test_matches_entra_id(self):
        result = _match_azure_services("Entra ID authentication delays")
        assert "Azure AD / Entra ID" in result

    def test_matches_multiple_services(self):
        result = _match_azure_services("AKS and Entra ID failures across UK South")
        assert "Azure Kubernetes Service (AKS)" in result
        assert "Azure AD / Entra ID" in result

    def test_returns_none_when_no_match(self):
        result = _match_azure_services("General network latency worldwide")
        assert result is None

    def test_case_insensitive(self):
        result = _match_azure_services("KUBERNETES cluster restarting")
        assert "Azure Kubernetes Service (AKS)" in result

    def test_matches_from_description_too(self):
        result = _match_azure_services(
            "Service issue detected", description="Cosmos DB experiencing throttling"
        )
        assert "Azure Cosmos DB" in result

    def test_dns_word_boundary(self):
        """'dns' should NOT match 'cdns' or other substrings."""
        result = _match_azure_services("CDNS provider issue")
        # 'dns' is 3 chars, uses word boundary — 'cdns' should not match
        assert result is None or "Azure DNS" not in (result or [])

    def test_arm_word_boundary(self):
        """'arm' should NOT match 'farming' or 'alarm'."""
        result = _match_azure_services("Server farming issues detected")
        assert result is None or "Azure ARM API" not in (result or [])

    def test_vpn_word_boundary(self):
        """'vpn' as a standalone word should match."""
        result = _match_azure_services("VPN gateway connectivity issues")
        assert result is not None
        assert "Azure VPN Gateway" in result


class TestAzureRSSServiceMatching:
    """Test that poll_azure_rss uses keyword matching to tag services."""

    def test_rss_items_tagged_with_matched_services(self):
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
            <item>
                <title>AKS cluster failures in UK South</title>
                <description>Kubernetes service disrupted</description>
                <pubDate>Mon, 03 Feb 2026 08:00:00 +0000</pubDate>
            </item>
        </channel></rss>"""
        feed = {
            "name": "Azure",
            "url": "https://example.com",
            "type": "azure_rss",
            "exclude_regions": [],
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            resp = MagicMock()
            resp.status_code = 200
            resp.text = rss_xml
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp
            results = poll_azure_rss(feed)

        assert len(results) == 1
        assert results[0]["services"] is not None
        assert "Azure Kubernetes Service (AKS)" in results[0]["services"]

    def test_rss_item_with_no_keyword_match(self):
        rss_xml = """<?xml version="1.0" encoding="UTF-8"?>
        <rss version="2.0"><channel>
            <item>
                <title>General platform issue</title>
                <description>Something vague happened</description>
                <pubDate>Mon, 03 Feb 2026 08:00:00 +0000</pubDate>
            </item>
        </channel></rss>"""
        feed = {
            "name": "Azure",
            "url": "https://example.com",
            "type": "azure_rss",
            "exclude_regions": [],
        }
        with patch("status_page.status_feeds.SESSION.get") as mock_get:
            resp = MagicMock()
            resp.text = rss_xml
            resp.raise_for_status = MagicMock()
            mock_get.return_value = resp
            results = poll_azure_rss(feed)

        assert len(results) == 1
        assert results[0]["services"] is None


class TestHistoryParserHardening:
    def test_statusio_history_accepts_uppercase_id_and_decodes_entities(self):
        html = """
        <div class="row incident" id="statusio_incident_AB12CD">
          <div class="panel-title"><a>Registry &amp; Auth outage</a></div>
          <p>Components</p>
          <p class="incident_section event_inner_text">Docker Hub Registry</p>
          <strong class="incident_time">February 02, 2026 12:00 UTC<br>February 02, 2026 12:00 UTC</strong>
          <strong class="incident_update_status">resolved</strong>
          <span class="incident_message_details">Resolved &amp; recovered</span>
        </div>
        """
        parsed = _parse_statusio_history(
            html, {"Docker Hub Registry": "Docker Hub"}, source_name="Docker"
        )
        assert len(parsed) == 1
        assert parsed[0]["external_id"] == "AB12CD"
        assert "Registry & Auth outage" in parsed[0]["title"]
        assert "Resolved & recovered" in parsed[0]["updates"][0]["message"]

    def test_azure_history_handles_entity_decoding(self):
        html = """
        <div class="row incident-history-header">
          Tracking ID: TRK123
          <div class="incident-history-title">AKS &amp; ARM outage</div>
          <div class="card-body">Between 10:00 UTC and 11:00 UTC on 08 December 2025</div>
        </div>
        """
        parsed = _parse_azure_history(html, exclude_regions=[])
        assert len(parsed) == 1
        assert parsed[0]["external_id"] == "azure-pir-TRK123"
        assert "AKS & ARM outage" in parsed[0]["title"]

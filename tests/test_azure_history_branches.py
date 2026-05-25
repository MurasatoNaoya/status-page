"""Additional branch coverage for _parse_azure_history in feeds/azure.py.

Targets the date-parsing fallbacks (cross-day span, single timestamp, bare
date), the no-blocks warning, the missing-tracking-id skip, the
title-region-specific exclusion, and the video-preamble empty-fallback.
"""

import logging

from status_page.feeds.azure import _parse_azure_history


def _block(tracking_id, title, body):
    return f"""
    <div class="row incident-history-header">
      <div class="incident-history-title">{title}</div>
      <div>Tracking ID: {tracking_id}</div>
      <div class="card-body">{body}</div>
    </div>
    """


def test_no_incident_blocks_logs_warning(caplog):
    with caplog.at_level(logging.WARNING):
        incidents = _parse_azure_history("<html>nothing here</html>")
    assert incidents == []
    assert any("no incident blocks" in r.message for r in caplog.records)


def test_block_without_tracking_id_is_skipped():
    html = """
    <div class="row incident-history-header">
      <div class="incident-history-title">No tracking id here</div>
      <div class="card-body">Between 10:00 UTC and 11:00 UTC on 08 December 2025</div>
    </div>
    """
    assert _parse_azure_history(html) == []


def test_cross_day_span_times():
    html = _block(
        "X1",
        "AKS outage",
        "Between 23:00 UTC on 07 December 2025 and 01:30 UTC on 08 December 2025 there was an outage.",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["created_at"] == "2025-12-07T23:00:00Z"
    assert inc["resolved_at"] == "2025-12-08T01:30:00Z"


def test_single_timestamp_fallback():
    # No "between ... and ..." span; only one timestamp present.
    html = _block(
        "X2",
        "Portal disruption",
        "Starting at 09:15 UTC on 03 March 2026 the portal was disrupted.",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["created_at"] == "2026-03-03T09:15:00Z"
    # resolved_at defaults to created_at when no end time is found
    assert inc["resolved_at"] == "2026-03-03T09:15:00Z"


def test_bare_date_fallback():
    # No time component anywhere, only a bare date.
    html = _block(
        "X3",
        "Storage issue",
        "An incident occurred on 08 December 2025 affecting blob storage.",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["created_at"] == "2025-12-08T00:00:00Z"


def test_no_parseable_date_leaves_timestamps_none():
    html = _block("X4", "Vague incident", "Something went wrong recently.")
    inc = _parse_azure_history(html)[0]
    assert inc["created_at"] is None
    assert inc["resolved_at"] is None


def test_title_region_specific_excluded():
    html = _block(
        "X5",
        "Power event affecting West US region",
        "All regions impacted. Between 10:00 UTC and 11:00 UTC on 08 December 2025.",
    )
    # Even though body says "all regions", the title names an excluded region.
    assert _parse_azure_history(html, exclude_regions=["West US"]) == []


def test_video_preamble_only_falls_back_to_full_body():
    # Body is entirely the video preamble; clean_body would be empty, so it
    # falls back to the original body.
    html = _block(
        "X6",
        "Entra outage",
        "Watch our 'Azure Incident Retrospective' video about this incident: https://aka.ms/air/X6 What happened? ",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["updates"][-1]["message"].strip() != ""


def test_impact_classification_major():
    html = _block(
        "X7",
        "Service outage",
        "Between 10:00 UTC and 11:00 UTC on 08 December 2025 there was a complete outage.",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["impact"] == "major"


def test_impact_classification_minor_default():
    html = _block(
        "X8",
        "Maintenance notice",
        "Between 10:00 UTC and 11:00 UTC on 08 December 2025 a planned maintenance occurred.",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["impact"] == "minor"


def test_unmatched_services_is_none():
    html = _block(
        "X9",
        "Some unrelated thing",
        "Between 10:00 UTC and 11:00 UTC on 08 December 2025 a generic event happened.",
    )
    inc = _parse_azure_history(html)[0]
    assert inc["services"] is None

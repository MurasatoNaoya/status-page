from unittest.mock import patch

import status_page.status_view as status_view


def test_incident_severity_picks_worst():
    incidents = [{"impact": "minor"}, {"impact": "major"}, {"impact": "partial"}]
    assert status_view.incident_severity(incidents) == "major"


def test_filter_incidents_for_service_deduplicates_and_matches_global():
    incidents = [
        {"id": 1, "service_name": "A", "title": "a"},
        {"id": 1, "service_name": "A", "title": "a"},
        {"id": 2, "service_name": None, "title": "global"},
        {"id": 3, "service_name": "B", "title": "b"},
    ]
    result = status_view.filter_incidents_for_service(incidents, "A")
    assert [i["id"] for i in result] == [1, 2]


def test_build_coverage_map_merges_sources():
    all_names = ["A", "B", "C"]
    feeds = [{"components": {"x": "B"}, "covered_services": ["C"]}]

    with (
        patch.object(
            status_view, "get_incident_coverage_start", return_value={"A": "2026-01-10"}
        ),
        patch.object(
            status_view,
            "get_check_coverage_start",
            return_value={"A": "2026-01-05", "B": "2026-01-20"},
        ),
        patch.object(
            status_view,
            "get_feed_incident_stats",
            return_value={"oldest": "2026-01-01T00:00:00Z"},
        ),
    ):
        coverage = status_view.build_coverage_map(all_names, feeds)

    assert coverage["A"] == "2026-01-05"
    assert coverage["B"] == "2026-01-01"
    assert coverage["C"] == "2026-01-01"


def test_build_feed_coverage_shapes_rows():
    feeds = [{"name": "GitHub", "components": {"x": "Svc"}}]
    with (
        patch.object(
            status_view,
            "get_feed_incident_stats",
            return_value={
                "cnt": 2,
                "oldest": "2026-01-01T00:00:00Z",
                "newest": "2026-01-04T00:00:00Z",
            },
        ),
        patch.object(
            status_view,
            "get_feed_backfill_capability",
            return_value={
                "feed_type": "statuspage",
                "ingestion": "Statuspage API",
                "known_limit_days": None,
                "cap_type": "implementation_bounded",
                "cap_summary": "summary",
            },
        ),
    ):
        rows = status_view.build_feed_coverage(feeds)

    assert len(rows) == 1
    assert rows[0]["name"] == "GitHub"
    assert rows[0]["incident_count"] == 2
    assert rows[0]["date_from"] == "2026-01-01"
    assert rows[0]["date_to"] == "2026-01-04"
    assert rows[0]["days_span"] == 3

"""Tests for API routes (/api/*)."""

from unittest.mock import patch


class TestAPIRoutes:
    def test_health_endpoint(self, app_client):
        resp = app_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert isinstance(data, dict)

    def test_scheduler_health_endpoint_healthy(self, app_client):
        with patch("status_page.app.get_scheduler_health") as mock_health:
            mock_health.return_value = {"status": "healthy", "enabled": True}
            resp = app_client.get("/api/health/scheduler")
        assert resp.status_code == 200
        assert resp.get_json()["status"] == "healthy"

    def test_scheduler_health_endpoint_stale(self, app_client):
        with patch("status_page.app.get_scheduler_health") as mock_health:
            mock_health.return_value = {"status": "stale", "enabled": True}
            resp = app_client.get("/api/health/scheduler")
        assert resp.status_code == 503
        assert resp.get_json()["status"] == "stale"

    def test_create_incident_api_unauthenticated(self, app_client):
        resp = app_client.post(
            "/api/incidents",
            json={
                "title": "Test Incident",
                "impact": "partial",
                "message": "Testing",
            },
        )
        assert resp.status_code == 401

    def test_update_incident_api_unauthenticated(self, app_client):
        resp = app_client.patch(
            "/api/incidents/1", json={"status": "resolved", "message": "Fixed"}
        )
        assert resp.status_code == 401

    def test_create_incident_api(self, app_client):
        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token
        resp = app_client.post(
            "/api/incidents",
            json={
                "title": "Test Incident",
                "impact": "partial",
                "message": "Testing",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert "id" in data

    def test_create_incident_api_missing_csrf(self, app_client):
        with app_client.session_transaction() as sess:
            sess["admin"] = True
        resp = app_client.post(
            "/api/incidents",
            json={"title": "Test Incident"},
        )
        assert resp.status_code == 403

    def test_update_incident_api(self, app_client):
        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token
        # Create first
        resp = app_client.post(
            "/api/incidents",
            json={"title": "Update Test", "message": "init"},
            headers={"X-CSRF-Token": csrf_token},
        )
        inc_id = resp.get_json()["id"]
        # Update
        resp = app_client.patch(
            f"/api/incidents/{inc_id}",
            json={"status": "resolved", "message": "Fixed"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 200

    def test_update_incident_api_not_found(self, app_client):
        csrf_token = "test-csrf-token"
        with app_client.session_transaction() as sess:
            sess["admin"] = True
            sess["_csrf_token"] = csrf_token
        resp = app_client.patch(
            "/api/incidents/999999",
            json={"status": "resolved", "message": "Fixed"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 404

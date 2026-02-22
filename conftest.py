"""Shared test fixtures for the status-page test suite."""

import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

import pytest

# Use an in-memory / temp DB for all tests
os.environ["STATUS_DB"] = ""  # Will be overridden per-test


@pytest.fixture(autouse=True)
def _isolated_db(tmp_path, monkeypatch):
    """Give every test its own temporary SQLite database."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("STATUS_DB", db_path)
    # Re-import to pick up the new path
    import database

    database.DB_PATH = db_path
    database.init_db()
    yield db_path


@pytest.fixture
def app_client(monkeypatch):
    """Create a Flask test client with isolated DB."""
    monkeypatch.setenv("ADMIN_USER", "admin")
    monkeypatch.setenv("ADMIN_PASS", "testpass")
    # Prevent scheduler from starting during tests
    import app as app_module

    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as client:
        yield client


# ---------------------------------------------------------------------------
# Playwright E2E fixtures
# ---------------------------------------------------------------------------


def _free_port():
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def live_server():
    """Start the Flask app in a subprocess for E2E tests.

    Uses a temp DB, disables the scheduler, and waits for /api/health.
    """
    port = _free_port()
    tmp_dir = tempfile.mkdtemp()
    db_path = os.path.join(tmp_dir, "e2e_test.db")
    env = {
        **os.environ,
        "STATUS_DB": db_path,
        "ADMIN_USER": "admin",
        "ADMIN_PASS": "testpass",
        "SECRET_KEY": "e2e-test-secret",
        "SESSION_COOKIE_SECURE": "false",
        "DISABLE_SCHEDULER": "1",
        "FLASK_APP": "app",
    }
    # Use the same Python interpreter that's running pytest (avoids hardcoded .venv path)
    flask_bin = os.path.join(os.path.dirname(sys.executable), "flask")
    proc = subprocess.Popen(
        [flask_bin, "run", "--host", "127.0.0.1", "--port", str(port)],
        cwd=os.path.dirname(__file__),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    base_url = f"http://127.0.0.1:{port}"

    # Wait for server to be ready (max 10s)
    import urllib.request

    for _ in range(100):
        try:
            urllib.request.urlopen(f"{base_url}/api/health", timeout=1)
            break
        except Exception:
            if proc.poll() is not None:
                stdout = proc.stdout.read().decode()
                stderr = proc.stderr.read().decode()
                raise RuntimeError(
                    f"Flask server exited early (code {proc.returncode}):\n"
                    f"stdout: {stdout}\nstderr: {stderr}"
                )
            time.sleep(0.1)
    else:
        proc.kill()
        stdout = proc.stdout.read().decode()
        stderr = proc.stderr.read().decode()
        raise RuntimeError(
            f"Flask server failed to start on port {port}:\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )

    yield base_url

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)
    # Clean up temp directory and DB
    shutil.rmtree(tmp_dir, ignore_errors=True)


@pytest.fixture
def admin_session(page, live_server):
    """Log into the admin panel and return the authenticated page."""
    page.goto(f"{live_server}/admin/login")
    page.fill("#username", "admin")
    page.fill("#password", "testpass")
    page.click('button[type="submit"]')
    page.wait_for_url("**/admin")
    return page


@pytest.fixture
def seed_incidents(live_server, page):
    """Create test incidents across categories via the API.

    Logs in first so the API calls are authenticated.
    """
    import json

    # Authenticate so the session cookie is set on page.request
    page.goto(f"{live_server}/admin/login")
    page.fill("#username", "admin")
    page.fill("#password", "testpass")
    page.click('button[type="submit"]')
    page.wait_for_url("**/admin")

    # Extract CSRF token from the admin page form
    csrf_token = page.locator('input[name="_csrf_token"]').first.get_attribute("value")

    incidents = [
        {
            "title": "AKS cluster issue",
            "impact": "major",
            "service_name": "Azure Kubernetes Service (AKS)",
        },
        {
            "title": "GitHub Actions degraded",
            "impact": "partial",
            "service_name": "GitHub Actions",
        },
        {
            "title": "Docker Hub slow pulls",
            "impact": "minor",
            "service_name": "Docker Hub",
        },
        {
            "title": "Azure Portal outage",
            "impact": "major",
            "service_name": "Azure Portal",
        },
        {
            "title": "GitHub API errors",
            "impact": "partial",
            "service_name": "github.com",
        },
    ]
    for inc in incidents:
        resp = page.request.post(
            f"{live_server}/api/incidents",
            data=json.dumps(inc),
            headers={
                "Content-Type": "application/json",
                "X-CSRF-Token": csrf_token,
            },
        )
        assert resp.status == 200 or resp.status == 201, (
            f"Failed to create incident: {resp.status}"
        )
    return incidents

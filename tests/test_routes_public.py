"""Tests for public-facing routes (index page, theme, tooltips)."""

import re


class TestIndexPage:
    def test_index_returns_200(self, app_client):
        resp = app_client.get("/")
        assert resp.status_code == 200

    def test_index_contains_page_title(self, app_client):
        resp = app_client.get("/")
        assert b"Status" in resp.data

    def test_index_contains_theme_toggle(self, app_client):
        resp = app_client.get("/")
        assert b"theme-toggle" in resp.data
        assert b"icon-sun" in resp.data
        assert b"icon-moon" in resp.data

    def test_index_contains_uptime_lines(self, app_client):
        resp = app_client.get("/")
        assert b"uptime-line" in resp.data
        assert b"90 days ago" in resp.data

    def test_index_contains_filter_pills(self, app_client):
        resp = app_client.get("/")
        assert b"filter-pill" in resp.data

    def test_index_dark_mode_css_variables(self, app_client):
        resp = app_client.get("/static/style.css")
        assert resp.status_code == 200
        css = resp.data.decode()
        assert '[data-theme="dark"]' in css
        assert "--green: #4080cf" in css

    def test_csp_uses_nonce_for_scripts(self, app_client):
        resp = app_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert "script-src 'self' 'nonce-" in csp
        assert "script-src 'self' 'unsafe-inline'" not in csp

    def test_index_nonce_matches_csp_and_no_placeholder_leaks(self, app_client):
        resp = app_client.get("/")
        html = resp.data.decode()
        csp = resp.headers.get("Content-Security-Policy", "")
        m = re.search(r"script-src 'self' 'nonce-([^']+)'", csp)
        assert m, "CSP nonce not found in response header"
        nonce = m.group(1)
        assert "__CSP_NONCE__" not in html
        assert f'<script nonce="{nonce}">' in html

    def test_hsts_not_set_by_default(self, app_client):
        resp = app_client.get("/")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_set_when_enabled_and_forwarded_https(self, app_client, monkeypatch):
        import status_page.app as app_module

        monkeypatch.setattr(app_module, "_ENABLE_HSTS", True)
        monkeypatch.setattr(app_module, "_HSTS_VALUE", "max-age=123")
        resp = app_client.get("/", headers={"X-Forwarded-Proto": "https"})
        assert resp.headers.get("Strict-Transport-Security") == "max-age=123"


class TestThemeToggleJS:
    """Verify the shared theme JS is loaded on pages."""

    def test_index_loads_theme_js(self, app_client):
        resp = app_client.get("/")
        html = resp.data.decode()
        assert "/static/theme.js" in html

    def test_theme_js_serves(self, app_client):
        resp = app_client.get("/static/theme.js")
        assert resp.status_code == 200
        js = resp.data.decode()
        assert "toggleTheme" in js
        assert "localStorage.setItem" in js
        assert "prefers-color-scheme" in js
        assert "removeChild(_themeStyle)" in js


class TestTooltipLabels:
    """Test that tooltip severity labels are correct in the template source.

    These check the Jinja2 template directly since labels only render when
    matching data exists (no way to guarantee all severity levels at runtime).
    """

    def test_partial_severity_tooltip_text(self):
        """Orange bars should show 'Partial outage', not 'Outage reported'.
        Tooltips are rendered by JS using severity labels."""
        with open("status_page/templates/index.html") as f:
            template = f.read()
        assert "Partial outage" in template

    def test_tooltip_severity_labels_in_template(self):
        """Verify all three severity labels exist in the JS tooltip renderer."""
        with open("status_page/templates/index.html") as f:
            template = f.read()
        assert "Major outage" in template
        assert "Partial outage" in template
        assert "Degraded performance" in template

    def test_no_bare_outage_reported_label(self):
        """'Outage reported' without qualifier (Partial/Major) should not exist.
        Bug: template had just 'Outage reported' for partial severity."""
        with open("status_page/templates/index.html") as f:
            template = f.read()

        # Find all 'outage reported' that aren't prefixed by Major or Partial
        matches = re.findall(r"(?<!Major )(?<!Partial )Outage reported", template)
        assert len(matches) == 0, (
            f"Found bare 'Outage reported' without qualifier: {matches}"
        )

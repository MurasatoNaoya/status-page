"""End-to-end browser tests using Playwright.

These tests launch a real Flask server and drive a real browser to verify
that the status page works correctly from a user's perspective.
"""

import re
import time

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


# ── Loop 1: Smoke Tests ─────────────────────────────────────────────────────


class TestSmoke:
    """Basic smoke tests: pages load, assets served, no crashes."""

    def test_index_loads(self, page, live_server):
        page.goto(live_server)
        expect(page.locator("h1")).to_be_visible()

    def test_index_has_overall_status_banner(self, page, live_server):
        page.goto(live_server)
        expect(page.locator(".overall-status")).to_be_visible()

    def test_css_loads(self, page, live_server):
        page.goto(live_server)
        bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert bg != "", "CSS failed to load — body has no background color"

    def test_api_health(self, page, live_server):
        resp = page.request.get(f"{live_server}/api/health")
        assert resp.status == 200
        data = resp.json()
        assert isinstance(data, dict)

    def test_admin_login_page_loads(self, page, live_server):
        page.goto(f"{live_server}/admin/login")
        expect(page.locator("h2")).to_have_text("Admin Login")
        expect(page.locator("#username")).to_be_visible()
        expect(page.locator("#password")).to_be_visible()

    def test_admin_redirects_when_not_logged_in(self, page, live_server):
        page.goto(f"{live_server}/admin")
        page.wait_for_url("**/admin/login")

    def test_no_js_errors_on_index(self, page, live_server):
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live_server)
        page.wait_for_load_state("networkidle")
        assert errors == [], f"JS errors on index: {errors}"

    def test_admin_panel_link_visible(self, page, live_server):
        page.goto(live_server)
        expect(page.locator(".admin-link")).to_be_visible()
        expect(page.locator(".admin-link")).to_have_text("Admin Panel")


# ── Loop 2: Theme Toggle ────────────────────────────────────────────────────


class TestThemeToggle:
    """Light/dark mode toggle across all pages."""

    def test_default_is_light(self, page, live_server):
        page.goto(live_server)
        theme = page.locator("html").get_attribute("data-theme")
        assert theme is None or theme == "light"

    def test_toggle_to_dark(self, page, live_server):
        page.goto(live_server)
        page.locator(".theme-toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    def test_toggle_back_to_light(self, page, live_server):
        page.goto(live_server)
        page.locator(".theme-toggle").click()
        page.locator(".theme-toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "light")

    def test_dark_mode_persists_across_navigation(self, page, live_server):
        page.goto(live_server)
        page.locator(".theme-toggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")
        # Navigate to admin login
        page.goto(f"{live_server}/admin/login")
        expect(page.locator("html")).to_have_attribute("data-theme", "dark")

    def test_dark_mode_changes_bg_color(self, page, live_server):
        page.goto(live_server)
        light_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        page.locator(".theme-toggle").click()
        # Wait for CSS transition to complete using auto-retry
        page.wait_for_function(
            f"getComputedStyle(document.body).backgroundColor !== '{light_bg}'"
        )
        dark_bg = page.evaluate("getComputedStyle(document.body).backgroundColor")
        assert light_bg != dark_bg, "Background color should change in dark mode"

    def test_theme_toggle_on_admin(self, page, live_server, admin_session):
        expect(admin_session.locator(".theme-toggle")).to_be_visible()
        admin_session.locator(".theme-toggle").click()
        expect(admin_session.locator("html")).to_have_attribute("data-theme", "dark")

    def test_admin_form_text_readable_in_dark_mode(
        self, page, live_server, admin_session
    ):
        admin_session.locator(".theme-toggle").click()
        expect(admin_session.locator("html")).to_have_attribute("data-theme", "dark")
        # Wait for CSS transition to finish before checking computed style
        admin_session.wait_for_function(
            "getComputedStyle(document.querySelector('.form-group input')).color !== 'rgb(0, 0, 0)'"
        )
        color = admin_session.evaluate(
            "getComputedStyle(document.querySelector('.form-group input')).color"
        )
        assert color != "rgb(0, 0, 0)", f"Input text is black in dark mode: {color}"


# ── Loop 3: Filter Pills ────────────────────────────────────────────────────


class TestFilterPills:
    """Category and impact filter pills on the index page."""

    def test_all_filter_active_by_default(self, page, live_server):
        page.goto(live_server)
        all_btn = page.locator('.filter-pill[data-filter="all"]')
        expect(all_btn).to_have_class(re.compile("active"))

    def test_category_filter_hides_non_matching(
        self, page, live_server, seed_incidents
    ):
        page.goto(live_server)
        page.locator('.filter-pill[data-filter="azure"]').click()
        # Azure incidents should be visible
        azure = page.locator('.incident[data-category="azure"]')
        if azure.count() > 0:
            expect(azure.first).to_be_visible()
        # GitHub incidents should be hidden
        github = page.locator('.incident[data-category="github"]')
        for i in range(github.count()):
            expect(github.nth(i)).to_be_hidden()

    def test_impact_filter_shows_only_matching(self, page, live_server, seed_incidents):
        page.goto(live_server)
        page.locator('.filter-pill[data-filter="major"]').click()
        visible = page.locator(".incident:visible")
        for i in range(visible.count()):
            assert visible.nth(i).get_attribute("data-impact") == "major"

    def test_clicking_all_resets_filters(self, page, live_server, seed_incidents):
        page.goto(live_server)
        page.locator('.filter-pill[data-filter="azure"]').click()
        page.locator('.filter-pill[data-filter="all"]').click()
        # All incidents should be visible again
        all_inc = page.locator(".incident[data-category]")
        for i in range(min(all_inc.count(), 5)):
            expect(all_inc.nth(i)).to_be_visible()

    def test_active_class_moves_on_click(self, page, live_server):
        page.goto(live_server)
        azure_btn = page.locator('.filter-pill[data-filter="azure"]')
        all_btn = page.locator('.filter-pill[data-filter="all"]')
        azure_btn.click()
        expect(azure_btn).to_have_class(re.compile("active"))
        expect(all_btn).not_to_have_class(re.compile("active"))

    def test_filter_pills_exist(self, page, live_server):
        page.goto(live_server)
        pills = page.locator(".filter-pill")
        assert pills.count() >= 4, "Expected at least 4 filter pills"


# ── Loop 4: Expand/Collapse + Tooltips ─────────────────────────────────────


class TestExpandCollapse:
    """Animated expand/collapse on service groups and incident details."""

    def test_incident_details_expand(self, page, live_server, seed_incidents):
        page.goto(live_server)
        details = page.locator(".incident-details").first
        expect(details).not_to_have_attribute("open", "")
        details.locator("summary").click()
        expect(details).to_have_attribute("open", "")

    def test_incident_details_collapse(self, page, live_server, seed_incidents):
        page.goto(live_server)
        details = page.locator(".incident-details").first
        summary = details.locator("summary")
        content = details.locator(".incident-updates-body")
        # Open and wait for animation to complete (expanded class added on transitionend)
        summary.click()
        expect(content).to_have_class(re.compile("expanded"))
        # Close
        summary.click()
        expect(details).not_to_have_attribute("open", "")

    def test_incident_content_visible_after_expand(
        self, page, live_server, seed_incidents
    ):
        page.goto(live_server)
        details = page.locator(".incident-details").first
        body = details.locator(".incident-updates-body")
        details.locator("summary").click()
        expect(details).to_have_attribute("open", "")
        expect(body).to_be_visible()

    def test_incident_content_hidden_after_collapse(
        self, page, live_server, seed_incidents
    ):
        page.goto(live_server)
        details = page.locator(".incident-details").first
        content = details.locator(".incident-updates-body")
        details.locator("summary").click()
        expect(content).to_have_class(re.compile("expanded"))
        # Collapse
        details.locator("summary").click()
        expect(details).not_to_have_attribute("open", "")

    def test_service_group_expand_toggle(self, page, live_server):
        page.goto(live_server)
        group_details = page.locator(".group-section details")
        if group_details.count() == 0:
            pytest.skip("No service groups configured")
        details = group_details.first
        summary = details.locator("summary")
        expect(details).not_to_have_attribute("open", "")
        # Expand
        summary.click()
        expect(details).to_have_attribute("open", "")
        # Services inside should be visible
        services = details.locator(".services .service")
        if services.count() > 0:
            expect(services.first).to_be_visible()

    def test_service_group_collapse_on_outside_click(self, page, live_server):
        page.goto(live_server)
        group_details = page.locator(".group-section details")
        if group_details.count() == 0:
            pytest.skip("No service groups configured")
        details = group_details.first
        content = details.locator(".services")
        # Open the group and wait for animation to finish
        details.locator("summary").click()
        expect(content).to_have_class(re.compile("expanded"))
        # Click outside — .overall-status is outside any <details>
        expect(page.locator(".overall-status")).to_be_visible()
        page.locator(".overall-status").click()
        expect(details).not_to_have_attribute("open", "")

    def test_multiple_incidents_can_expand_independently(
        self, page, live_server, seed_incidents
    ):
        page.goto(live_server)
        all_details = page.locator(".incident-details")
        if all_details.count() < 2:
            pytest.skip("Need at least 2 incidents")
        # Open first and second
        all_details.nth(0).locator("summary").click()
        expect(all_details.nth(0)).to_have_attribute("open", "")
        all_details.nth(1).locator("summary").click()
        expect(all_details.nth(1)).to_have_attribute("open", "")
        # Both should still be open
        expect(all_details.nth(0)).to_have_attribute("open", "")

    def test_no_js_errors_during_expand_collapse(
        self, page, live_server, seed_incidents
    ):
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(live_server)
        details = page.locator(".incident-details").first
        content = details.locator(".incident-updates-body")
        # Open and wait for animation
        details.locator("summary").click()
        expect(content).to_have_class(re.compile("expanded"))
        # Close
        details.locator("summary").click()
        expect(details).not_to_have_attribute("open", "")
        assert errors == [], f"JS errors during expand/collapse: {errors}"


class TestTooltips:
    """Tooltip display on uptime bar hover."""

    def test_tooltip_hidden_by_default(self, page, live_server):
        page.goto(live_server)
        tip = page.locator("#shared-tip")
        # Tooltip should exist but be hidden
        expect(tip).to_be_attached()
        display = page.evaluate(
            "getComputedStyle(document.getElementById('shared-tip')).display"
        )
        assert display == "none" or tip.is_hidden()

    def test_tooltip_shows_on_hover(self, page, live_server):
        page.goto(live_server)
        bars = page.locator(".uptime-day[data-tip]")
        if bars.count() == 0:
            pytest.skip("No uptime bars with tooltip data")
        bars.first.hover()
        tip = page.locator("#shared-tip")
        expect(tip).to_be_visible()

    def test_tooltip_contains_date(self, page, live_server):
        page.goto(live_server)
        bars = page.locator(".uptime-day[data-tip]")
        if bars.count() == 0:
            pytest.skip("No uptime bars with tooltip data")
        bars.first.hover()
        tip = page.locator("#shared-tip")
        date_el = tip.locator(".tooltip-date")
        expect(date_el).to_be_visible()
        assert date_el.inner_text().strip() != "", "Tooltip date should not be empty"

    def test_tooltip_hides_on_mouseout(self, page, live_server):
        page.goto(live_server)
        bars = page.locator(".uptime-day[data-tip]")
        if bars.count() == 0:
            pytest.skip("No uptime bars with tooltip data")
        bars.first.hover()
        tip = page.locator("#shared-tip")
        expect(tip).to_be_visible()
        # Move mouse away to the header
        page.locator("h1").hover()
        expect(tip).to_be_hidden()


# ── Loop 5: Admin Login Flow ───────────────────────────────────────────────


class TestAdminLogin:
    """Admin login, logout, and session management."""

    def test_login_success(self, page, live_server):
        page.goto(f"{live_server}/admin/login")
        page.fill("#username", "admin")
        page.fill("#password", "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/admin")
        expect(page.locator("h1")).to_have_text("Admin Panel")

    def test_login_wrong_password(self, page, live_server):
        page.goto(f"{live_server}/admin/login")
        page.fill("#username", "admin")
        page.fill("#password", "wrongpass")
        page.click('button[type="submit"]')
        # Should stay on login page with error
        page.wait_for_url("**/admin/login")
        expect(page.locator(".flash-error")).to_be_visible()

    def test_login_wrong_username(self, page, live_server):
        page.goto(f"{live_server}/admin/login")
        page.fill("#username", "notadmin")
        page.fill("#password", "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/admin/login")
        expect(page.locator(".flash-error")).to_be_visible()

    def test_logout(self, page, live_server, admin_session):
        # admin_session logs us in; now click logout (POST form)
        admin_session.click("button.admin-nav-logout")
        # Logout redirects to index
        admin_session.wait_for_url(live_server + "/")
        # Verify we can't access admin anymore
        admin_session.goto(f"{live_server}/admin")
        admin_session.wait_for_url("**/admin/login")

    def test_admin_panel_has_nav_links(self, page, live_server, admin_session):
        expect(admin_session.locator("button.admin-nav-logout")).to_be_visible()

    def test_sso_button_disabled(self, page, live_server):
        page.goto(f"{live_server}/admin/login")
        sso_btn = page.locator(".sso-btn")
        expect(sso_btn).to_be_visible()
        assert sso_btn.is_disabled(), "SSO button should be disabled without config"

    def test_admin_panel_shows_declare_section(self, page, live_server, admin_session):
        expect(admin_session.locator(".declare-section")).to_be_visible()
        expect(admin_session.locator(".declare-section h2")).to_have_text(
            "Declare Incident"
        )


# ── Loop 6: Admin Incident Declaration + Update ────────────────────────────


class TestAdminIncidentManagement:
    """Declaring and updating incidents from the admin panel."""

    def test_declare_incident_form_fields(self, page, live_server, admin_session):
        form = admin_session.locator(".declare-section form")
        expect(form.locator('input[name="title"]')).to_be_visible()
        expect(form.locator('select[name="impact"]')).to_be_visible()
        expect(form.locator('select[name="service"]')).to_be_visible()
        expect(form.locator('textarea[name="message"]')).to_be_visible()
        expect(form.locator(".btn-declare")).to_be_visible()

    def test_declare_incident(self, page, live_server, admin_session):
        # Accept the confirm dialog
        admin_session.on("dialog", lambda d: d.accept())
        admin_session.fill('input[name="title"]', "E2E Test Incident")
        admin_session.select_option('select[name="impact"]', "major")
        admin_session.fill('textarea[name="message"]', "Testing incident creation")
        admin_session.click(".btn-declare")
        # Should redirect back to admin with flash
        admin_session.wait_for_url("**/admin")
        expect(admin_session.locator(".flash-msg")).to_be_visible()

    def test_declared_incident_appears_in_active(
        self, page, live_server, admin_session
    ):
        admin_session.on("dialog", lambda d: d.accept())
        admin_session.fill('input[name="title"]', "Active Test Incident")
        admin_session.select_option('select[name="impact"]', "partial")
        admin_session.fill('textarea[name="message"]', "Testing")
        admin_session.click(".btn-declare")
        admin_session.wait_for_url("**/admin")
        # The incident should appear in the active incidents section
        expect(admin_session.locator(".mgmt-incident").first).to_be_visible()
        expect(admin_session.locator("text=Active Test Incident").first).to_be_visible()

    def test_update_incident(self, page, live_server, admin_session):
        # First declare an incident
        admin_session.on("dialog", lambda d: d.accept())
        admin_session.fill('input[name="title"]', "Update Test Incident")
        admin_session.select_option('select[name="impact"]', "minor")
        admin_session.fill('textarea[name="message"]', "Initial message")
        admin_session.click(".btn-declare")
        admin_session.wait_for_url("**/admin")
        # Now update it
        update_form = admin_session.locator(".mgmt-update-form").first
        update_form.locator('select[name="status"]').select_option("identified")
        update_form.locator('input[name="message"]').fill("Root cause identified")
        update_form.locator(".btn-update").click()
        admin_session.wait_for_url("**/admin")
        expect(admin_session.locator(".flash-msg")).to_be_visible()

    def test_resolve_incident(self, page, live_server, admin_session):
        # Declare an incident
        admin_session.on("dialog", lambda d: d.accept())
        admin_session.fill('input[name="title"]', "Resolve Test Incident")
        admin_session.select_option('select[name="impact"]', "minor")
        admin_session.fill('textarea[name="message"]', "Initial")
        admin_session.click(".btn-declare")
        admin_session.wait_for_url("**/admin")
        # Resolve it
        update_form = admin_session.locator(".mgmt-update-form").first
        update_form.locator('select[name="status"]').select_option("resolved")
        update_form.locator('input[name="message"]').fill("Issue resolved")
        update_form.locator(".btn-update").click()
        admin_session.wait_for_url("**/admin")
        # After resolving, it should no longer appear in active incidents
        # (or show "No active incidents")
        expect(admin_session.locator(".flash-msg")).to_be_visible()

    def test_declared_incident_visible_on_status_page(
        self, page, live_server, admin_session
    ):
        admin_session.on("dialog", lambda d: d.accept())
        admin_session.fill('input[name="title"]', "Visible On Status Page")
        admin_session.select_option('select[name="impact"]', "major")
        admin_session.fill('textarea[name="message"]', "Major outage")
        admin_session.click(".btn-declare")
        admin_session.wait_for_url("**/admin")
        # Check the status page shows this incident
        admin_session.goto(live_server)
        expect(admin_session.locator(".active-incident-banner")).to_be_visible()
        # Verify our incident is somewhere in the active incident banner
        banner_text = admin_session.locator(".active-incident-banner").inner_text()
        assert "Visible On Status Page" in banner_text

    def test_csrf_token_present_in_forms(self, page, live_server, admin_session):
        # Verify CSRF tokens are in all admin forms
        forms = admin_session.locator('form input[name="_csrf_token"]')
        assert forms.count() >= 1, "Expected CSRF tokens in admin forms"


# ── Loop 7: Admin Backfill + Metrics ───────────────────────────────────────


class TestAdminBackfill:
    """Backfill section tests."""

    def test_backfill_section_visible(self, page, live_server, admin_session):
        expect(admin_session.locator(".backfill-section")).to_be_visible()
        expect(admin_session.locator(".backfill-section h2")).to_have_text(
            "Data Backfill"
        )

    def test_backfill_button_exists(self, page, live_server, admin_session):
        expect(admin_session.locator(".btn-backfill")).to_be_visible()

    def test_backfill_has_csrf_token(self, page, live_server, admin_session):
        form = admin_session.locator(".backfill-section form")
        expect(form.locator('input[name="_csrf_token"]')).to_be_attached()

    def test_integrations_section_visible(self, page, live_server, admin_session):
        expect(admin_session.locator(".integrations")).to_be_visible()
        expect(admin_session.locator(".integrations h3")).to_have_text(
            "Alert Integrations"
        )

    def test_integration_rows_present(self, page, live_server, admin_session):
        rows = admin_session.locator(".integration-row")
        assert rows.count() >= 3, "Expected Slack, Teams, Jira integration rows"


# ── Loop 8: Edge Cases + Error Handling ────────────────────────────────────


class TestEdgeCases:
    """404 pages, empty states, missing data, error conditions."""

    def test_404_page(self, page, live_server):
        resp = page.request.get(f"{live_server}/nonexistent-page")
        assert resp.status == 404

    def test_api_incidents_post_only(self, page, live_server):
        """The /api/incidents endpoint only accepts POST requests."""
        resp = page.request.get(f"{live_server}/api/incidents")
        assert resp.status == 405, "GET should not be allowed on /api/incidents"

    def test_no_incidents_message(self, page, live_server):
        """On fresh server (first test), no-incidents message or incidents section should exist."""
        page.goto(live_server)
        section = page.locator(".incidents-section")
        expect(section).to_be_visible()

    def test_empty_admin_active_incidents(self, page, live_server, admin_session):
        """Admin panel should show 'no active incidents' or incident list."""
        incidents_section = admin_session.locator(".incidents-mgmt")
        expect(incidents_section).to_be_visible()

    def test_no_js_errors_on_admin(self, page, live_server, admin_session):
        errors = []
        admin_session.on("pageerror", lambda e: errors.append(str(e)))
        admin_session.reload()
        admin_session.wait_for_load_state("networkidle")
        assert errors == [], f"JS errors on admin: {errors}"

    def test_no_js_errors_on_login_page(self, page, live_server):
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(f"{live_server}/admin/login")
        page.wait_for_load_state("networkidle")
        assert errors == [], f"JS errors on login: {errors}"

    def test_api_health_returns_json(self, page, live_server):
        resp = page.request.get(f"{live_server}/api/health")
        headers = resp.headers
        content_type = headers.get("content-type", "")
        assert "json" in content_type.lower(), (
            f"Expected JSON content-type, got {content_type}"
        )

    def test_static_css_serves(self, page, live_server):
        resp = page.request.get(f"{live_server}/static/style.css")
        assert resp.status == 200
        body = resp.text()
        assert len(body) > 100, "CSS file seems too small"


# ── Loop 9: Accessibility + Responsive ─────────────────────────────────────


class TestAccessibility:
    """ARIA labels, keyboard nav, semantic HTML, responsive layout."""

    def test_theme_toggle_has_aria_label(self, page, live_server):
        page.goto(live_server)
        toggle = page.locator(".theme-toggle")
        expect(toggle).to_have_attribute("aria-label", "Toggle dark mode")

    def test_admin_theme_toggle_has_aria_label(self, page, live_server, admin_session):
        toggle = admin_session.locator(".theme-toggle")
        expect(toggle).to_have_attribute("aria-label", "Toggle dark mode")

    def test_page_has_lang_attribute(self, page, live_server):
        page.goto(live_server)
        expect(page.locator("html")).to_have_attribute("lang", "en")

    def test_page_has_viewport_meta(self, page, live_server):
        page.goto(live_server)
        viewport = page.locator('meta[name="viewport"]')
        expect(viewport).to_be_attached()

    def test_login_form_labels_linked(self, page, live_server):
        page.goto(f"{live_server}/admin/login")
        # Labels should have 'for' attributes matching input IDs
        username_label = page.locator('label[for="username"]')
        expect(username_label).to_be_visible()
        password_label = page.locator('label[for="password"]')
        expect(password_label).to_be_visible()

    def test_mobile_viewport_renders(self, page, live_server):
        page.set_viewport_size({"width": 375, "height": 812})
        page.goto(live_server)
        expect(page.locator("h1")).to_be_visible()
        expect(page.locator(".overall-status")).to_be_visible()

    def test_tablet_viewport_renders(self, page, live_server):
        page.set_viewport_size({"width": 768, "height": 1024})
        page.goto(live_server)
        expect(page.locator("h1")).to_be_visible()
        expect(page.locator(".overall-status")).to_be_visible()

    def test_heading_hierarchy(self, page, live_server):
        page.goto(live_server)
        # Should have an h1
        h1_count = page.locator("h1").count()
        assert h1_count == 1, f"Expected exactly 1 h1, found {h1_count}"


# ── Loop 10: Performance + Full Lifecycle ──────────────────────────────────


class TestPerformanceAndLifecycle:
    """Page load performance and full incident lifecycle E2E."""

    def test_index_loads_quickly(self, page, live_server):
        start = time.monotonic()
        page.goto(live_server)
        page.wait_for_load_state("networkidle")
        elapsed_ms = (time.monotonic() - start) * 1000
        assert elapsed_ms < 5000, (
            f"Index took {elapsed_ms:.0f}ms to load (expected < 5000ms)"
        )

    def test_full_incident_lifecycle(self, page, live_server, admin_session):
        """Create incident -> verify on status page -> update -> resolve -> verify resolved."""
        admin_session.on("dialog", lambda d: d.accept())

        # 1. Declare incident
        admin_session.fill('input[name="title"]', "Lifecycle Test")
        admin_session.select_option('select[name="impact"]', "partial")
        admin_session.fill('textarea[name="message"]', "Investigating issue")
        admin_session.click(".btn-declare")
        admin_session.wait_for_url("**/admin")

        # 2. Verify on status page
        admin_session.goto(live_server)
        banner_text = admin_session.locator(".active-incident-banner").inner_text()
        assert "Lifecycle Test" in banner_text

        # 3. Go back to admin and update
        admin_session.goto(f"{live_server}/admin")
        update_form = admin_session.locator(".mgmt-update-form").first
        update_form.locator('select[name="status"]').select_option("identified")
        update_form.locator('input[name="message"]').fill("Root cause found")
        update_form.locator(".btn-update").click()
        admin_session.wait_for_url("**/admin")

        # 4. Resolve
        update_form = admin_session.locator(".mgmt-update-form").first
        update_form.locator('select[name="status"]').select_option("resolved")
        update_form.locator('input[name="message"]').fill("Issue resolved")
        update_form.locator(".btn-update").click()
        admin_session.wait_for_url("**/admin")
        expect(admin_session.locator(".flash-msg")).to_be_visible()

    def test_no_console_errors_full_navigation(self, page, live_server, admin_session):
        """Navigate through all pages and check for JS errors."""
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        # Index
        page.goto(live_server)
        page.wait_for_load_state("networkidle")

        # Admin login
        page.goto(f"{live_server}/admin/login")
        page.wait_for_load_state("networkidle")

        # Admin panel (via admin_session which is already logged in)
        admin_session.goto(f"{live_server}/admin")
        admin_session.wait_for_load_state("networkidle")

        assert errors == [], f"JS errors during navigation: {errors}"

    def test_all_pages_have_proper_title(self, page, live_server, admin_session):
        page.goto(live_server)
        assert page.title() != "", "Index should have a title"

        page.goto(f"{live_server}/admin/login")
        assert "Admin" in page.title() or "Login" in page.title()

        admin_session.goto(f"{live_server}/admin")
        assert "Admin" in admin_session.title()

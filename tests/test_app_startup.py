"""Tests for the _startup() sequence in status_page.app.

These exercise the credential-guard branches, the orphan-cleanup branch,
and the gap-backfill logging, which are the paths that drive _startup's
complexity. The scheduler is disabled via DISABLE_SCHEDULER (set in
conftest), so _restart_scheduler returns early without spawning threads.
"""

import pytest

import status_page.app as app_module


@pytest.fixture
def startup_env(monkeypatch):
    """Put the app module into a state where _startup() can run cleanly.

    Allows the default password and missing secret key (dev mode), gives a
    single valid service, and no DNS bar. The scheduler is disabled.
    """
    monkeypatch.setattr(app_module, "ADMIN_PASS", "changeme")
    monkeypatch.setattr(app_module, "_SECRET_KEY_ENV", None)
    monkeypatch.setattr(app_module, "SERVICES", [{"name": "Web"}])
    monkeypatch.setattr(app_module, "GROUPS", [])
    monkeypatch.setattr(app_module, "DNS_BAR", None)
    monkeypatch.setenv("ALLOW_DEFAULT_PASSWORD", "1")
    monkeypatch.setenv("DISABLE_SCHEDULER", "1")
    return monkeypatch


def test_startup_raises_when_admin_pass_is_default_and_not_allowed(monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_PASS", "changeme")
    monkeypatch.delenv("ALLOW_DEFAULT_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="ADMIN_PASS"):
        app_module._startup()


def test_startup_raises_when_secret_key_missing_and_not_allowed(monkeypatch):
    monkeypatch.setattr(app_module, "ADMIN_PASS", "a-real-password")
    monkeypatch.setattr(app_module, "_SECRET_KEY_ENV", None)
    monkeypatch.delenv("ALLOW_DEFAULT_PASSWORD", raising=False)
    with pytest.raises(RuntimeError, match="SECRET_KEY"):
        app_module._startup()


def test_startup_warns_on_default_password_when_allowed(startup_env, caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        result = app_module._startup()
    # Scheduler disabled -> returns None
    assert result is None
    assert any("changeme" in r.message for r in caplog.records)


def test_startup_warns_on_missing_secret_key_when_allowed(startup_env, caplog):
    import logging

    # Real password so the secret-key warning branch is the one we exercise.
    startup_env.setattr(app_module, "ADMIN_PASS", "a-real-password")
    with caplog.at_level(logging.WARNING):
        app_module._startup()
    assert any("SECRET_KEY" in r.message for r in caplog.records)


def test_startup_skips_orphan_cleanup_by_default(startup_env, caplog):
    import logging

    startup_env.delenv("CLEANUP_ORPHANS_ON_STARTUP", raising=False)
    called = {"cleanup": False}

    def fake_cleanup(names):
        called["cleanup"] = True
        return 0

    startup_env.setattr(app_module, "cleanup_orphan_services", fake_cleanup)
    with caplog.at_level(logging.INFO):
        app_module._startup()
    assert called["cleanup"] is False
    assert any("skipped orphan cleanup" in r.message for r in caplog.records)


def test_startup_runs_orphan_cleanup_when_enabled(startup_env, caplog):
    import logging

    startup_env.setenv("CLEANUP_ORPHANS_ON_STARTUP", "1")
    captured = {}

    def fake_cleanup(names):
        captured["names"] = names
        return 3

    startup_env.setattr(app_module, "cleanup_orphan_services", fake_cleanup)
    with caplog.at_level(logging.INFO):
        app_module._startup()
    assert captured["names"] == ["Web"]
    assert any("removed 3 orphan rows" in r.message for r in caplog.records)


def test_startup_includes_dns_bar_name_in_valid_names(startup_env):
    startup_env.setenv("CLEANUP_ORPHANS_ON_STARTUP", "1")
    startup_env.setattr(app_module, "DNS_BAR", {"name": "DNS Resolution"})
    captured = {}

    def fake_cleanup(names):
        captured["names"] = names
        return 0

    startup_env.setattr(app_module, "cleanup_orphan_services", fake_cleanup)
    app_module._startup()
    assert "DNS Resolution" in captured["names"]
    assert "Web" in captured["names"]


def test_startup_warns_when_cleanup_enabled_but_no_valid_names(startup_env, caplog):
    import logging

    startup_env.setenv("CLEANUP_ORPHANS_ON_STARTUP", "1")
    startup_env.setattr(app_module, "SERVICES", [])
    startup_env.setattr(app_module, "GROUPS", [])
    startup_env.setattr(app_module, "DNS_BAR", None)
    called = {"cleanup": False}

    def fake_cleanup(names):
        called["cleanup"] = True
        return 0

    startup_env.setattr(app_module, "cleanup_orphan_services", fake_cleanup)
    with caplog.at_level(logging.WARNING):
        app_module._startup()
    assert called["cleanup"] is False
    assert any("valid service list is empty" in r.message for r in caplog.records)


def test_startup_logs_gap_days(startup_env, caplog):
    import logging

    startup_env.setattr(app_module, "backfill_check_gaps", lambda names: 5)
    with caplog.at_level(logging.INFO):
        app_module._startup()
    assert any("5 service-days with no check data" in r.message for r in caplog.records)

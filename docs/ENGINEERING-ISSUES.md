# Engineering Issues and Improvement Backlog

This file tracks structural/code-quality issues identified during review, with recommended actions.

## Completed

### 1) `status_page/app.py` split — DONE

Refactored from ~1200 lines to ~570 lines. Extracted:
- `status_page/scheduler_jobs.py` (scheduler setup + job functions)
- `status_page/status_view.py` (service/group data-building helpers)
- `status_page/routes/admin.py`, `api.py`, `public.py` (Flask blueprints)
- `status_page/runtime.py` (shared context provider for blueprints)

### 2) Scheduler liveness health signal — DONE

Added `/api/health/scheduler` endpoint with per-job heartbeat tracking, staleness detection (`max_interval * 3`), and 503 response when stale. Deploy workflow verifies scheduler health after restart.

### 3) CI/CD pipeline — DONE

Added `.github/workflows/deploy.yml` with rsync code sync, systemd restart, and HTTPS scheduler health verification with retry loop.

### 4) Dev dependency separation — DONE

Created `requirements-dev.txt` (pytest, pytest-cov, pytest-playwright, ruff). Production `requirements.txt` contains only runtime dependencies. CI installs dev deps only in test jobs.

### 5) `tests/test_incident_service.py` coverage expanded — DONE

Added tests for declare success/failure with/without Jira, resolve not-found path, resolve success path including alert dispatch args.

### 6) Admin CSS extraction — DONE

Moved ~250 lines of inline CSS from `admin.html` to `status_page/static/admin.css`.

### 7) Slack resolution alerts — DONE

Resolution alerts now use Block Kit format consistent with declaration payloads.

### 8) `status_feeds.py` split into per-adapter modules — DONE

Split 1,080-line monolith into `status_page/feeds/` package:
- `feeds/common.py` — shared utilities (SESSION, TIMEOUT, history tracking, backfill caps)
- `feeds/statuspage.py` — Atlassian Statuspage API adapter
- `feeds/azure.py` — Azure RSS + history page scraping
- `feeds/statusio.py` — Status.io API + history scraping
- `feeds/__init__.py` — poll_feed dispatcher + re-exports
- `status_feeds.py` retained as thin re-export for backward compatibility

### 9) `test_app.py` split into focused test files — DONE

Split 1,969-line test file into:
- `tests/test_routes_public.py` — index page, theme toggle, tooltip tests
- `tests/test_routes_api.py` — API endpoint tests
- `tests/test_routes_admin.py` — admin auth and operations tests
- `tests/test_service_data.py` — build_service_data, bar coverage, auto-detection, severity
- Status feed tests moved to `tests/test_status_feeds.py`
- Feed importer integration tests moved to `tests/test_feed_importer.py`

### 10) Database backup strategy — DONE

Added `backup_database()` function using SQLite online backup API with 7-day rotation. Scheduled as daily cron job at 02:30 UTC in the APScheduler.

## Remaining Issues

### 11) Backward-compatible wrappers in `app.py`

Lines 234-279 contain 6 pass-through functions (`_incident_severity`, `_filter_incidents_for_service`, `run_service_check`, `run_dns_bar_check`) that exist solely for old import paths.

Recommended action:
- Audit callers and remove once all consumers use the extracted modules directly.

### 12) No structured logging

Application uses Python's basic `logging.basicConfig()` with `%s` formatting.

Recommended action:
- Switch to JSON structured logging for production to enable log aggregation and alerting.

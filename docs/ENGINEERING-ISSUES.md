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

## Remaining Issues

### 8) `status_feeds.py` is the largest module (1,079 lines)

Handles four feed types (statuspage, statusio, azure_rss, azure_service_health) plus two history page scrapers in a single file.

Recommended action:
- Split into per-adapter modules under a `feeds/` directory (e.g., `feeds/statuspage.py`, `feeds/azure_rss.py`).

### 9) `test_app.py` is 1,958 lines

Mixes route tests, rendering tests, and integration tests in one file.

Recommended action:
- Split into `test_routes_admin.py`, `test_routes_api.py`, `test_routes_public.py`, and `test_index_rendering.py`.

### 10) Backward-compatible wrappers in `app.py`

Lines 234-279 contain 6 pass-through functions (`_incident_severity`, `_filter_incidents_for_service`, `run_service_check`, `run_dns_bar_check`) that exist solely for old import paths.

Recommended action:
- Audit callers and remove once all consumers use the extracted modules directly.

### 11) No database backup strategy

PROJECT.md mentions daily SQLite backup in Phase 6 but no implementation exists.

Recommended action:
- Add a scheduler task or cron job for daily SQLite backups with rotation.

### 12) No structured logging

Application uses Python's basic `logging.basicConfig()` with `%s` formatting.

Recommended action:
- Switch to JSON structured logging for production to enable log aggregation and alerting.

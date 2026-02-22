# Engineering Issues and Improvement Backlog

This file tracks structural/code-quality issues identified during review, with recommended actions.

## Important Issues

### 1) `status_page/app.py` is too large (~1200+ lines)

Risk:
- Mixed concerns (routing, scheduler wiring, service data building, security glue) increase regression risk.

Recommended split:
1. `status_page/web_routes.py` (Flask route handlers)
2. `status_page/scheduler_jobs.py` (scheduler setup + job functions)
3. `status_page/presentation.py` (service/group data-building helpers)

Target:
- Keep `status_page/app.py` as composition/bootstrap only.

### 2) No scheduler liveness health signal

Risk:
- Scheduler can die while web process remains up, leading to stale data with no immediate alert.

Recommended action:
1. Track per-job heartbeat timestamps.
2. Add `/api/health/scheduler` (or extend `/api/health`) with:
   - scheduler running flag
   - last check job run time
   - stale threshold evaluation
3. Alert if stale beyond threshold.

### 3) CI/CD pipeline gap

Observation:
- Test/lint workflows exist, but there is no deployment workflow.

Recommended action:
1. Add explicit deploy workflow (manual dispatch + protected environment).
2. Include:
   - smoke check (`/api/health`)
   - rollback guidance
   - artifact/version annotation.

### 4) Test dependencies are in production requirements

Risk:
- Production install includes `pytest`/Playwright tooling unnecessarily.

Recommended action:
1. Keep `requirements.txt` runtime-only.
2. Add `requirements-dev.txt` for test/dev tools.
3. Update CI to install dev deps only in test jobs.

### 5) `tests/test_incident_service.py` coverage is too thin

Risk:
- Orchestration logic (DB + alerts + Jira key linking) can regress silently.

Recommended action:
1. Add tests for:
   - declare success/failure with/without Jira
   - resolve not-found path
   - resolve success path including alert dispatch args
   - idempotency/retry behavior assumptions.

### 6) Inline CSS in `status_page/templates/admin.html` (~250 lines)

Risk:
- Harder to maintain and test; style regressions become template regressions.

Recommended action:
1. Move inline admin CSS to `status_page/static/admin.css`.
2. Keep template structure-only.

### 7) Slack resolution alerts inconsistent with declaration format

Risk:
- Inconsistent operator experience and lower readability.

Recommended action:
1. Use Block Kit for resolution alerts too.
2. Align fields with declaration payload:
   - incident id/title/service/impact/status/update time.

## Suggested Execution Order

1. Split prod vs dev dependencies (`requirements-dev.txt`).
2. Add scheduler liveness signal endpoint.
3. Expand `incident_service` tests.
4. Move admin inline CSS to static file.
5. Standardize Slack resolution payload.
6. Refactor `status_page/app.py` into modules.
7. Add deploy workflow after refactor stabilizes.

# Status Page: Comprehensive Context & Codex Handoff Report

**Date:** 2026-02-22
**Purpose:** Full context document so Codex (or any AI agent) can work on this project without repeating mistakes or losing nuance.

---

## 1. What This Project Is

An internal status page for a CloudOps team monitoring Azure UK South infrastructure, GitHub, Docker Hub, Red Hat Quay.io, and other dependencies. It is a Flask app with SQLite, APScheduler for background checks, and Jinja2 templates.

**Key architectural decisions:**
- Single-process deployment (one gunicorn worker) to avoid duplicate APScheduler instances
- SQLite is sufficient for the expected load (internal team tool, not public-facing at scale)
- 90-day uptime bars on the dashboard, computed from real check data + real feed incidents
- External status feeds (GitHub, Azure, Docker, Red Hat) are polled for real incidents and imported into the `incidents` table
- Active health checks (HTTP, TCP, DNS, script) run on intervals defined in `config.yaml`

**The stack:**
- `app.py` — Flask routes, build_service_data, scheduler setup, admin panel, API endpoints
- `database.py` — SQLite schema, CRUD operations, coverage queries
- `checker.py` — Health check functions (HTTP, TCP, DNS, script)
- `status_feeds.py` — Parsers for external status APIs (Atlassian Statuspage v2, StatusIO, Azure RSS)
- `feed_importer.py` — Scheduler-driven feed polling orchestration
- `alerts.py` — Slack/Jira alerting on incident declaration/resolution
- `incident_service.py` — Orchestrates incident lifecycle (create + alert + Jira key persistence)
- `config.yaml` — All service definitions, feed configs, DNS bar targets
- `gunicorn.conf.py` — Production deployment config

---

## 2. The #1 Rule: No Synthetic Data

**This rule is in `CLAUDE.md` and is non-negotiable.**

```
NEVER create synthetic or fake data.
- No synthetic "up" check_results
- No assumed statuses for periods when the app wasn't running
- No backfill records with NULL response_time_ms
- The only valid sources of data are: real health checks and real feed incidents
- "No data" is always preferable to inaccurate data
```

### Why this rule exists

Early development considered backfilling `check_results` rows to fill the 90-day bar chart. This was rejected because:

1. The status page must be trustworthy. If someone sees a green bar for last Tuesday, it must mean the service was genuinely checked and found healthy — not that an AI guessed it was probably fine.
2. SQLite queries like `get_uptime_days()` compute percentages from real `check_results` rows. Injecting fake rows corrupts those percentages.

### The critical distinction: inference vs fabrication

**Fabrication (banned):** Inserting fake `check_results` rows into the database.

**Inference (allowed):** If a status feed covers a service and reports zero incidents for a given day, the service was operational that day. This is not synthetic data — it is a logical conclusion from real feed data. The coverage system (described in section 5) implements this inference without creating any database records.

**This distinction tripped up Codex.** Codex's commit `5506a0c` ("enforce no-data as grey") removed the coverage-based inference system, interpreting the CLAUDE.md rule too literally. The result: 83 out of 90 bars turned grey even though feed data proved those services were fine. The fix was restoring the coverage system — inferring green from absence of incidents within a feed's known date range, without writing any synthetic database records.

---

## 3. What Codex Did (Two Commits)

### Commit 5506a0c — "Harden incident integrity and enforce no-data as grey"

**Changes (416 insertions, 62 deletions across 10 files):**

| Change | Assessment |
|--------|------------|
| `requires_env` gating on checker functions | Good. Prevents false alarms for VPN-only targets |
| `.env.example` with env var documentation | Good |
| `alerts.py` — Slack and Jira alerting module | Good. Clean separation of concerns |
| `test_alerts.py` — Unit tests for alerts | Good |
| `database.py` — `set_incident_jira_key()`, `incidents.jira_key` column | Good. Enables Jira ticket linking |
| `config.yaml` — Added `requires_env` examples and DNS bar comments | Good |
| `requirements.txt` — Added `defusedxml`, `requests` | Good |
| `test_database.py` — Tests for Jira key persistence | Good |
| Removed coverage inference from `build_service_data()` | **Bad.** Broke the 90-day bars (see section 2) |
| Rewrote tests to expect grey bars for all non-check days | **Bad.** Tests validated the broken behavior |

### Commit 9213c4b — "Harden ops flows, deterministic Jira linking, and parser resilience"

**Changes (451 insertions, 214 deletions across 8 files):**

| Change | Assessment |
|--------|------------|
| CSP nonce support (per-request `secrets.token_urlsafe(16)`) | Good, but broke E2E tests |
| `config_schema.py` — Config validation at load time | Good |
| `telemetry.py` — In-process counters/timings | Good, but not yet wired to any dashboard |
| `incident_service.py` — Incident orchestration layer | Good |
| `static/admin.js` — Delegated form confirmation | Good. Replaces inline onclick handlers |
| `get_query_db()` context manager in database.py | Good. Fixes connection leak from prior review |
| `status_feeds.py` parser hardening | Good |
| `incident_service.py` file was empty (0 bytes) | **Bad.** File existed but had no content, causing ImportError |

---

## 4. What Codex Did Well

### 4.1 Security hardening
- CSP nonces replacing `'unsafe-inline'` in script-src
- Moved inline `onclick`/`onsubmit` handlers to external `admin.js`
- `requires_env` gating so VPN-only checks skip gracefully instead of false-alarming

### 4.2 Operational improvements
- `config_schema.py` validates config.yaml at startup, catching typos early
- `incident_service.py` encapsulates incident lifecycle (create + alert + Jira link)
- `get_query_db()` context manager prevents DB connection leaks for one-off queries
- `alerts.py` clean separation of Slack/Jira notification logic

### 4.3 Code quality
- Added test files for new modules
- `telemetry.py` is well-structured (thread-safe counters with snapshot for observability)
- Parser hardening in `status_feeds.py` (better error handling for malformed feeds)

### 4.4 Config improvements
- `requires_env` pattern with commented examples for private network targets
- `.env.example` documenting all environment variables

---

## 5. What Codex Did Poorly & Lessons

### 5.1 Misinterpreted CLAUDE.md rule (CRITICAL)

**What happened:** Codex read "no synthetic data" and removed the coverage inference system, forcing all days without `check_results` rows to show grey. This ignored that feed data (real incidents from real APIs) is itself a valid data source.

**The fix:** A coverage system that computes per-service "coverage windows" from three sources:
1. `get_check_coverage_start(service_names)` — earliest `check_results` date per service
2. `get_incident_coverage_start()` — earliest `incidents` date per service
3. `get_feed_incident_stats(svc_names)` — date range of a status feed's known incidents, propagated to all services that feed covers

Days within coverage with zero incidents = green (service was fine). Days before any coverage = grey (no data). No database records are created — the inference happens at render time in `build_service_data()`.

**Lesson for Codex:** The CLAUDE.md rule bans writing fake records to the database. It does not ban logical inference from real data. If a status feed has been running for 60 days and reports zero incidents for a service, that service was operational for those 60 days. This conclusion comes from real feed data, not fabrication.

### 5.2 Empty file shipped (incident_service.py)

**What happened:** `incident_service.py` was committed as a 0-byte file. `app.py` imported `declare_incident_with_alerts` from it, causing an `ImportError` at startup.

**Lesson for Codex:** Always verify that files you create actually contain content. Run `python -c "import app"` as a smoke test after changes.

### 5.3 CSP nonces broke E2E tests

**What happened:** Playwright's `wait_for_function()` uses `eval()` internally to execute JavaScript in the browser. The new CSP with nonce-based `script-src` (no `'unsafe-eval'`) blocked these calls silently, causing E2E test timeouts.

**The fix:** Replaced `wait_for_function()` with:
- `expect(page.locator("html")).to_have_attribute("data-theme", "dark")` for theme toggle tests
- `page.wait_for_timeout(300)` where checking computed styles

**Lesson for Codex:** When tightening CSP, run E2E tests. Playwright relies on eval in some APIs. Use `expect()` matchers and Playwright locators instead of `wait_for_function()`.

### 5.4 Tests validated broken behavior

**What happened:** After removing the coverage system, Codex rewrote the bar chart tests to expect all-grey bars. The tests passed, but the behavior was wrong — the tests were testing the bug, not the feature.

**Lesson for Codex:** Tests should encode the intended behavior from the product perspective, not merely validate what the code currently does. Ask: "Should a user see grey here?" If the answer is "no, they should see green because we have feed data," the test is wrong.

### 5.5 Telemetry not wired up

**What happened:** `telemetry.py` exposes `incr()`, `observe()`, `timed_call()`, and `snapshot()`. It's imported in `app.py`. But there's no admin endpoint or dashboard to view the data. The counters accumulate in memory with no way to inspect them.

**Lesson for Codex:** If adding observability infrastructure, wire it end-to-end. An admin endpoint like `/admin/telemetry` returning `snapshot()` as JSON would complete the feature.

---

## 6. Architecture Deep-Dive (for Codex)

### 6.1 Database schema (SQLite)

**Tables:**
- `check_results` — Health check outcomes (service_name, status, response_time_ms, error_message, checked_at)
- `incidents` — Imported feed incidents + manually declared incidents (title, status, impact, message, service_name, jira_key, created_at, updated_at, resolved_at)

**Key queries:**
- `get_uptime_days(service, days)` — Returns daily uptime percentages from `check_results`. Only includes days that have real check data.
- `get_incidents_by_day(service_names)` — Returns incidents grouped by date for the 90-day bar chart.
- `get_incident_coverage_start()` — Oldest incident date per service. Used for coverage computation.
- `get_check_coverage_start(names)` — Oldest check date per service.
- `get_feed_incident_stats(names)` — Count/oldest/newest incident across a set of feed-covered services.
- `get_latest_status(names)` — Most recent check result per service (for the current status badge).
- `cleanup_old_checks(active_services)` — Deletes check_results older than 90 days. Also removes orphan services.

### 6.2 The 90-day bar chart

Each service shows 90 bars (one per day, oldest on the left). Each bar is colored:

| Color | Meaning |
|-------|---------|
| Green | Service was operational (100% uptime from checks, or inferred from feed coverage with no incidents) |
| Yellow | Degraded (uptime < 100% but > 0%, or minor/partial incidents) |
| Red | Major outage (0% uptime or major incidents) |
| Grey | No data (outside the coverage window — no checks ran and no feed covers this service for that date) |

**How bars are computed in `build_service_data()`:**

```python
for day in last_90_days:
    coverage_start = coverage.get(service_name)
    in_coverage = coverage_start is not None and day >= coverage_start

    if has_check_data_for_day:
        # Use real check data (uptime_pct from check_results)
        bar_color = based_on_actual_uptime_percentage
    elif in_coverage and no_incidents_that_day:
        # Within feed coverage, no incidents reported = green
        bar_color = green  # uptime_pct = 100.0
    elif in_coverage and has_incidents:
        # Within feed coverage with incidents
        bar_color = based_on_incident_severity
    else:
        # Outside coverage = grey
        bar_color = grey  # uptime_pct = None
```

### 6.3 Coverage computation (in `index()`)

The coverage map is built once per page load:

```python
# 1. Per-service: when did checks start?
check_starts = get_check_coverage_start(all_service_names)

# 2. Per-service: when did incidents start?
incident_starts = get_incident_coverage_start()

# 3. Per-feed: what's the oldest incident across all services this feed covers?
#    Propagate that date to every service in the feed.
for feed in STATUS_FEEDS:
    feed_service_names = set(feed.components.values()) | set(feed.covered_services)
    stats = get_feed_incident_stats(feed_service_names)
    oldest_date = stats["oldest"]
    for svc in feed_service_names:
        feed_coverage_start[svc] = min(existing, oldest_date)

# 4. Merge: coverage[svc] = earliest of check_start, incident_start, feed_start
coverage = {svc: min(all_dates_for_svc) for svc in all_service_names if any_dates}
```

**Why feed coverage matters:** A service like "Azure ARM API" might have zero incidents in the database. Without feed coverage, all 90 bars would be grey. But the Azure RSS feed has been running for months — if it reported no ARM API incidents, that service was fine. The feed's date range (from its oldest incident for *any* covered service) becomes the coverage window for *all* services in that feed.

### 6.4 Status feeds

Four feed types are supported:

| Type | API | Services Covered |
|------|-----|-----------------|
| Atlassian Statuspage v2 | `/api/v2/incidents.json` | GitHub (Actions, API, Copilot, Web), Red Hat Quay.io |
| StatusIO | status.io API | Docker Hub |
| Azure RSS | `/en-us/status/feed/` | AKS, ACR, Entra ID, Portal, ARM API, Blob Storage, SQL |
| Azure Service Health | ARM API (commented out) | Same as RSS but with proper severity and region filtering |

**Feed-to-service mapping** is defined in `config.yaml`:
- `components:` maps feed component names to local service names (e.g., `"Actions": "GitHub Actions"`)
- `covered_services:` lists services that should inherit coverage from this feed even if no incidents exist for them specifically (used by Azure feed)

### 6.5 Config structure (config.yaml)

```yaml
page:           # Title, description
status_feeds:   # External APIs to poll (GitHub, Azure, Docker, Red Hat)
dns_bar:        # DNS Resolution bar (separate from service groups)
  targets:      # List of hostnames to resolve
services: []    # Top-level services (currently empty — all services are in groups)
groups:         # Service groups displayed on the dashboard
  - name: "Azure (UK South)"
    services: [...]
  - name: "GitHub"
    services: [...]
  - name: "Public Container Registries"
    services: [...]
```

### 6.6 Security model

- **Admin authentication:** Session-based, bcrypt-hashed password from `ADMIN_PASS` env var
- **CSRF protection:** Token-per-session stored in Flask session, validated via `_check_csrf_token()` for form submissions and `X-CSRF-Token` header for API calls
- **Rate limiting:** IP-based login failure tracking with `_login_failures` dict (regular dict, not defaultdict — fixed from prior review)
- **CSP:** `default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'nonce-{nonce}'; img-src 'self' data:` — per-request nonce
- **Security headers:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`
- **XML parsing:** `defusedxml.ElementTree` used instead of stdlib `xml.etree.ElementTree` to prevent XXE attacks
- **Script checker:** `subprocess.run()` with `shlex.split()` (no `shell=True`). Commands come from `config.yaml` which must be trusted.
- **Default password:** `ADMIN_PASS=changeme` is rejected at startup unless `ALLOW_DEFAULT_PASSWORD=1` is set

### 6.7 Request-scoped DB connections

Two patterns:
- `get_request_db()` — Stored in Flask's `g` object, closed on request teardown. Used by route handlers.
- `get_query_db()` — Context manager for one-off queries outside request context (scheduler tasks, CLI scripts). Opens and closes its own connection.

```python
# Route handler (request-scoped):
db = get_request_db()
db.execute(...)  # Connection lives for the full request

# Scheduler task (context manager):
with get_query_db() as db:
    db.execute(...)  # Connection closed when block exits
```

### 6.8 The `requires_env` pattern

Any check target can include `requires_env: "ENV_VAR_NAME"`. If the env var is not set, the check returns `("skip", None, "requires env ENV_VAR_NAME")` instead of marking the service as down.

This prevents false alarms when running off-VPN. Example from `config.yaml`:
```yaml
# - hostname: "mydb.privatelink.database.windows.net"
#   label: "DB Private Link"
#   requires_env: "ON_PRIVATE_NETWORK"
```

Currently all DNS bar targets and service checks are public endpoints. Private network targets are commented out and ready to enable.

---

## 7. What Was Done Before Codex (Context From Prior Sessions)

### 7.1 Features removed (by Claude, pre-Codex)
- **Social media scraping** (`social.py`) — Scraped Twitter/Reddit for outage chatter. Removed because it didn't serve the core status page mission and added fragile external dependencies.
- **Page view metrics** (`admin_metrics.html`, `record_page_view()`) — Tracked visitor IPs and page views. Removed for the same reason — an internal status page doesn't need analytics.

### 7.2 Security bugs fixed (by Claude, pre-Codex)
1. Default password bypass in debug mode — Replaced with explicit `ALLOW_DEFAULT_PASSWORD` env var
2. API endpoints lacked CSRF protection — Added `X-CSRF-Token` header validation
3. Missing input validation on incident updates — Added status allowlist and message length cap
4. `defaultdict(list)` for login failures — Changed to regular `dict` to prevent phantom entries
5. `xml.etree.ElementTree` without entity restriction — Replaced with `defusedxml`
6. Missing trust model documentation on `check_script` — Added security comment
7. No gunicorn worker recycling — Added `max_requests = 1000` with jitter
8. Missing security response headers — Added CSP, X-Content-Type-Options, X-Frame-Options

### 7.3 Code review iterations (by Claude, pre-Codex)
Two full code reviews were conducted. 16 issues were identified and fixed, including:
- Unbounded `_login_failures` dict (added eviction + hard cap at 10k IPs)
- Missing `_login_lock` threading lock
- Feed deduplication on reimport (preventing duplicate incidents)
- CSS animation performance (moved from expensive properties to transform/opacity)
- Template deduplication (consolidated repeated HTML)
- Dead code removal across the codebase

---

## 8. Nuances & Gotchas for Future Work

### 8.1 macOS fork safety
Gunicorn's prefork model conflicts with APScheduler threads on macOS. If scheduler threads make network calls (HTTP checks, DNS lookups) and then gunicorn forks, the process crashes with:
```
objc[90738]: +[NSNumber initialize] may have been in progress in another thread when fork() was called
```
Fix: Set `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` before starting gunicorn on macOS. This is only needed for local development — Linux deployments are unaffected.

### 8.2 Single-worker constraint
The app runs with `workers = 1` in gunicorn because APScheduler would create duplicate scheduler instances in each worker. This means the app handles concurrency via threads (`threads = 2`), not processes. This is fine for an internal tool but would need rethinking for high-traffic deployment (e.g., use an external scheduler like celery-beat).

### 8.3 Feed coverage edge cases
- A service with zero feed incidents still gets coverage if *another* service in the same feed has incidents. The feed's date range covers all services listed in its `components` or `covered_services`.
- If a feed has never had any incidents at all (empty history), no coverage is established. This is correct — we can't infer anything from an empty feed.
- `covered_services` in config.yaml exists specifically for the Azure feed, where the feed reports incidents by Azure service name but we want those services to show green bars when the feed reports nothing.

### 8.4 DNS bar vs service groups
The DNS Resolution bar is a separate UI element from the service groups. It has its own check interval (60s) and its own set of targets. In `build_service_data()`, it's treated as a single service named "DNS Resolution" whose daily uptime aggregates all target results.

### 8.5 Feed deduplication
When feeds are re-polled, the same incidents can be returned. `feed_importer.py` deduplicates by checking if an incident with the same title and service_name already exists. This prevents duplicate bar chart entries.

### 8.6 The `expected_status` pattern
Some services are checked by expecting a non-200 status code:
- `Azure ARM API` expects 401 (unauthenticated request is rejected, proving the API is alive)
- `Docker Hub` expects 401 (registry requires auth, but responding means it's up)
- `Azure ACR` is checked via DNS resolution instead of HTTP

### 8.7 Test structure
- `test_app.py` — Unit tests for Flask routes, `build_service_data()`, admin panel, API endpoints. Uses in-memory SQLite.
- `test_database.py` — Tests for database CRUD operations.
- `test_checker.py` — Tests for health check functions (mocked network calls).
- `test_status_feeds.py` — Tests for feed parsers.
- `test_alerts.py` — Tests for Slack/Jira alerting.
- `test_e2e.py` — Playwright browser tests. Requires `ALLOW_DEFAULT_PASSWORD=1`. Uses CSP nonces, so avoid `wait_for_function()`.

### 8.8 Azure feed region filtering
The Azure RSS feed reports global incidents. `exclude_regions` in config.yaml filters out incidents that mention other regions (West US, East US, etc.) so only UK South and global incidents are imported. This is important — without it, the dashboard would show incidents irrelevant to the team's infrastructure.

---

## 9. Common Mistakes to Avoid

1. **Don't insert fake `check_results`.** If you need historical data, let the feeds and checks accumulate it over time. The coverage system handles the gap gracefully.

2. **Don't remove coverage inference.** Green bars for days with no incidents within feed coverage are correct. This is real data inference, not fabrication.

3. **Don't use `wait_for_function()` in Playwright tests.** CSP nonces block eval. Use `expect()` matchers and Playwright locators.

4. **Don't add more gunicorn workers.** APScheduler will duplicate. If you need more concurrency, increase `threads`.

5. **Don't use `defaultdict` for security-sensitive data structures.** Regular dicts with `.get()` and `.setdefault()` prevent phantom entry accumulation.

6. **Don't use `xml.etree.ElementTree`.** Always use `defusedxml.ElementTree` to prevent XXE.

7. **Always run `python -c "import app"` after making changes** to catch import errors before deployment.

8. **Always run the full test suite** (`python -m pytest test_app.py test_database.py test_checker.py test_status_feeds.py test_alerts.py -v`) before committing. All 213+ tests should pass.

9. **Don't add inline JavaScript.** CSP requires nonce-gated script tags or external `.js` files.

10. **When adding private network targets**, always use `requires_env: "ON_PRIVATE_NETWORK"` so the check skips gracefully off-VPN instead of false-alarming.

---

## 10. Current State & Next Steps

**What's working:**
- All 213 tests pass
- Ruff lint and format clean
- 90-day bar chart shows correct green/grey/red bars based on real data
- Feed polling imports real incidents from GitHub, Azure, Docker, Red Hat
- Active health checks run every 60-300 seconds
- Admin panel for declaring/resolving incidents
- API endpoints for programmatic incident management
- Slack/Jira alerting on incident lifecycle
- CSP nonces, security headers, CSRF protection all in place

**What's pending:**
- Add private network DNS targets with `requires_env: "ON_PRIVATE_NETWORK"` (for when running on VPN)
- Add private link / private endpoint hostnames for Azure resources
- Wire up telemetry dashboard (endpoint to view `telemetry.snapshot()`)
- Consider enabling Azure Service Health API (commented out in config.yaml) for better incident severity data
- Consider adding Azure Resource Health checks (per-resource ARM API checks, commented out)

**Files that should rarely change:**
- `CLAUDE.md` — Project rules (the "no synthetic data" rule)
- `gunicorn.conf.py` — Production config
- `config_schema.py` — Config validation

**Files that change frequently:**
- `config.yaml` — Adding/removing services and targets
- `app.py` — Route changes, bar chart logic
- `database.py` — Schema evolution, new queries
- `status_feeds.py` — New feed type parsers

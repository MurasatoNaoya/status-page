# Status Page — Project Documentation

> **Purpose of this document**: Get a future Claude Code or Codex instance fully up to speed on the codebase, architecture, rules, and upcoming work (private link DNS resolution, Azure Service Health API, Azure Resource Health).

---

## Agent Handoff Snapshot (Read First)

If you're a fresh agent session, this is the minimum context to avoid regressions:

1. **No synthetic data, ever**. "No data" must render as grey and stay grey until real checks/incidents exist.
2. **This repo was recently restructured into a package** (`status_page/`). Root `app.py` is now only a thin compatibility entrypoint.
3. **Status badge vs 90-day bars are different semantics**:
   - badge = latest check result now
   - bars = historical uptime + incident coverage model
4. **Private endpoints must be env-gated** with `requires_env` (usually `ON_PRIVATE_NETWORK`) or they will show false downs off-VPN.
5. **CI lint workflow auto-formats and auto-commits** on branch pushes; do not assume formatting failures are "manual fix only".
6. **Scheduler is single-process by design**. Production must stay at one gunicorn worker to prevent duplicate jobs/import races.

---

## What This Project Is

An internal status page for a CloudOps team. It monitors the health of cloud infrastructure (Azure UK South, GitHub, container registries) and displays a 90-day uptime history with incident timelines. Think of it as a self-hosted Statuspage.io that also aggregates incidents from upstream providers.

**Stack**: Flask + SQLite + APScheduler + Gunicorn, ~10k lines, 319 tests (unit/integration + E2E Playwright).

**Repo structure** (as of `cf984cf`):

```
status_page/           # Application package
  app.py               # Flask app, routes, scheduler, service data building
  database.py          # SQLite schema, queries, migrations
  checker.py           # Health check implementations (HTTP, TCP, DNS, script)
  status_feeds.py      # External feed polling (Statuspage, Status.io, Azure RSS)
  feed_importer.py     # Imports feed incidents into database
  alerts.py            # Email, Slack, Teams, Jira notifications
  incident_service.py  # Incident lifecycle orchestration
  config_schema.py     # Config validation
  telemetry.py         # In-process counters and timings
  config.yaml          # Service definitions, feeds, DNS targets
  templates/           # Jinja2 (index.html, admin.html, admin_login.html)
  static/              # style.css, theme.js, admin.js

tests/                 # All tests
  conftest.py          # Fixtures (isolated DB per test, Flask client, Playwright)
  test_app.py          # Route + integration tests (~1746 lines)
  test_database.py     # Schema, queries, coverage, orphan cleanup
  test_checker.py      # All check types + env gating
  test_status_feeds.py # Feed parsers, Azure history, Status.io history
  test_alerts.py       # Email/Slack/Teams/Jira mocking
  test_e2e.py          # Browser tests (dark mode, filters, incident UI)
  test_config_schema.py
  test_incident_service.py

app.py                 # Thin entrypoint (imports from status_page.app)
gunicorn.conf.py       # 1 worker, 2 threads, port 5050
requirements.txt
CLAUDE.md              # Project rules (read this first)
```

---

## The One Rule You Must Never Break

**NEVER create synthetic or fake data.**

This is enforced at the database layer — `record_check()` raises `ValueError` if you try to record an "up" check without a real `response_time_ms`. The rule exists because:

- If the app wasn't running for 3 days, those 3 days must show as grey ("no data"), not green
- No fabricated check results, no assumed statuses, no backfill records with NULL timing
- The only valid data sources are: real health checks from the scheduler, and real incidents from external status feeds
- "No data" is always preferable to inaccurate data

This applies to everything: test helpers, migrations, coverage backfill. If you're tempted to insert a fake "up" record to make a bar green, stop.

---

## How Data Flows

### 1. Health Checks (every 60s per service)

```
APScheduler → run_service_check(service)
  → checker.run_check(service)     # HTTP/TCP/DNS/script
  → database.record_check(...)     # Real result only
  → Auto-incident logic:
      3 consecutive failures → declare_incident_with_alerts()
      Recovery after incident → resolve_incident_with_alerts()
```

Each check returns `(status, response_time_ms, error)` where status is `"up"`, `"down"`, or `"skip"`.

The `"skip"` status is used for **environment gating** — when a service has `requires_env: "ON_PRIVATE_NETWORK"` and that env var isn't set, the check is silently skipped (not marked as down). This is critical for private network targets that can't be reached from public CI or local dev.

### 2. Feed Polling (every 300s per feed)

```
APScheduler → feed_importer.poll_status_feed(feed_config)
  → status_feeds.poll_feed(feed_config)     # GitHub, Azure, Docker, Red Hat
  → For each incident:
      - Generate external_id: "{feed_id}:{service_name}"
      - If exists: sync status/impact changes
      - If new active incident: create_incident() + send alerts
      - If new resolved incident (historical): import only (no alert spam)
      - On status transitions (active↔resolved): send reopen/resolution alerts
      - On IntegrityError: handle race (another worker inserted first)
```

Feed types:
- **statuspage** (default): Atlassian Statuspage API — GitHub, Red Hat
- **statusio**: Status.io API — Docker Hub
- **azure_rss**: Azure status RSS feed + history page scraping
- **azure_service_health**: (commented out) Azure Service Health API — the upgrade path

For `statuspage` feeds, incident backfill is paginated up to `max_incident_pages`
(default `10`, max `100`) per feed in `config.yaml`.

### 3. Page Rendering (on each request to `/`)

```
GET / → app.index()
  → get_latest_status() for all services
  → get_incidents_by_day() for 90-day overlay
  → Build coverage map:
      coverage[service] = min(check_start, incident_start, feed_start)
  → build_service_data() per service:
      For each of 90 days:
        - Has check data? → show actual uptime %
        - In coverage window, no incidents? → green (100%)
        - Before coverage? → grey (no data)
  → Render index.html with uptime bars, incidents, status badges
```

### 4. Incident Lifecycle

```
Manual:  Admin panel form → declare_incident_with_alerts()
Auto:    3 consecutive check failures → declare_incident_with_alerts()
Feed:    External status page → feed_importer → create_incident()

Updates: Admin panel or API → update_incident()
Resolve: Admin panel, API, or auto-recovery → resolve_incident_with_alerts()

Alerts fire on declare and resolve:
  → Email (Resend preferred, SMTP fallback)
  → Slack webhook (Block Kit message)
  → Teams webhook
  → Jira ticket (create on declare, comment+transition on resolve)

Feed-imported incidents now use the same alert model for active/reopened/resolved
transitions. Historical resolved backfill imports do not send alerts.
```

---

## Database Schema

SQLite with WAL mode. Path from `STATUS_DB` env var (default: `status.db`).

```sql
check_results (id, service_name, status, response_time_ms, error_message, checked_at)
  -- Index: (service_name, checked_at)

incidents (id, title, service_name, external_id, jira_key, status, impact, created_at, resolved_at)
  -- UNIQUE partial index on external_id WHERE NOT NULL
  -- Index: (service_name, resolved_at)
  -- Index: (created_at)

incident_updates (id, incident_id FK, status, message, created_at)
  -- Index: (incident_id)

schema_migrations (migration PK, applied_at)
```

**Key query patterns**:
- `get_latest_status()` uses `ROW_NUMBER() OVER (PARTITION BY service_name ORDER BY checked_at DESC, id DESC)` for deterministic latest-per-service
- `get_uptime_percentage()` takes `min(check_pct, incident_pct)` — most conservative estimate
- `get_incident_downtime_hours()` merges overlapping incident intervals, falls back to impact-based estimates (major=4h, partial=2h, minor=1h)

**Connection management**:
- `get_request_db()` — request-scoped (stored on Flask `g`), shared across single HTTP request
- `get_db()` — short-lived context manager for scheduler/background tasks
- `get_query_db()` — smart router: uses request DB in HTTP context, short-lived DB otherwise

---

## Coverage System (How Green vs Grey Works)

This is the most misunderstood part of the codebase. Here's how it works:

A service's **coverage window** starts from the earliest date any data source can vouch for it. Three sources contribute:

1. **Check data**: earliest `date(checked_at)` in `check_results` for the service
2. **Incident data**: earliest `date(created_at)` in `incidents` for the service
3. **Feed date range**: earliest incident across all services covered by a feed

For any given day in the 90-day window:
- **Has check data** → show actual uptime percentage from checks
- **In coverage, no check data, no incidents** → green (inferred operational)
- **In coverage, has incidents** → show incident severity overlay
- **Before coverage start** → grey ("no data")

This means a service covered by the Azure RSS feed will show green for days where Azure had no incidents, even if we have no direct check data. That's correct — the feed is vouching that the service was operational.

**Do not** try to "fix" grey bars by inserting synthetic checks. If a service shows grey, either it genuinely has no data coverage, or coverage needs to come from adding a feed or waiting for checks to accumulate.

---

## Config Structure (config.yaml)

```yaml
page:
  title: "Status"

status_feeds:
  - name: "GitHub"
    url: "https://www.githubstatus.com/api/v2"    # Statuspage API
    max_incident_pages: 10                         # Statuspage backfill depth
    components:
      "Actions": "GitHub Actions"                   # external_name: our_name
      "Git Operations": "GitHub Web Pages (github.com)"
    interval: 300

  - name: "Azure"
    type: azure_rss
    url: "https://azure.status.microsoft/en-us/status/feed/"
    history_url: "https://azure.status.microsoft/en-us/status/history/"
    exclude_regions: ["West US", "East US", ...]    # Only import UK South
    covered_services:                               # For bar coverage (green vs grey)
      - "Azure Kubernetes Service (AKS)"
      - "Azure Blob Storage (UK South)"
    interval: 300

dns_bar:
  name: "DNS Resolution"
  interval: 60
  targets:
    - hostname: "uksouth.blob.core.windows.net"
      label: "Azure Blob"
    - hostname: "mydb.privatelink.database.windows.net"  # Private endpoint
      label: "DB Private Link"
      requires_env: "ON_PRIVATE_NETWORK"                 # Skipped when off VPN

services: []   # Top-level services (currently empty, using groups)

groups:
  - name: "Azure (UK South)"
    services:
      - name: "Azure Kubernetes Service (AKS)"
        type: http
        url: "https://mcr.microsoft.com/v2/"
        interval: 60
      - name: "App DB (Private Link)"       # Example private target
        type: dns
        hostname: "mydb.privatelink.database.windows.net"
        requires_env: "ON_PRIVATE_NETWORK"   # Skip when off VPN
        interval: 60
```

**Service types**: `http`, `tcp`, `dns`, `script`, `azure_service_health` (commented), `azure_resource_health` (commented)

**Feed types**: `statuspage` (default), `statusio`, `azure_rss`, `azure_service_health` (commented)

---

## How to Add Private Network Targets (The Next Phase)

This is what the project was built toward. The scaffolding exists in `checker.py` and `config.yaml` (commented out).

### Pattern 1: Private Link DNS Resolution

Private endpoints use Azure Private DNS zones. The hostname `mydb.privatelink.database.windows.net` only resolves to a private IP when queried from within the VNet.

```yaml
# In dns_bar.targets:
- hostname: "mydb.privatelink.database.windows.net"
  label: "DB Private Link"
  requires_env: "ON_PRIVATE_NETWORK"

# Or as a standalone service:
services:
  - name: "App DB (Private Link)"
    type: dns
    hostname: "mydb.privatelink.database.windows.net"
    requires_env: "ON_PRIVATE_NETWORK"
    interval: 60
```

When `ON_PRIVATE_NETWORK` is not set, the check returns `("skip", None, "Skipped: requires env ON_PRIVATE_NETWORK")` — it's not counted as down, it's invisible. When set (e.g., running inside the VNet or on VPN), it resolves normally.

### Pattern 2: Azure Resource Health (per-resource)

Commented out in `checker.py` lines 210-238. Uses Azure ARM API to check individual resource availability.

```python
# checker.py — uncomment and register in CHECKERS dict
def check_azure_resource_health(service):
    credential = _get_azure_credential()
    token = credential.get_token("https://management.azure.com/.default").token
    resource_id = service["resource_id"]
    url = f"https://management.azure.com{resource_id}/providers/Microsoft.ResourceHealth/availabilityStatuses/current?api-version=2024-02-01"
    # Returns: Available, Unavailable, Degraded, Unknown
```

Config:
```yaml
services:
  - name: "AKS Cluster Health"
    type: azure_resource_health
    resource_id: "/subscriptions/<sub-id>/resourceGroups/<rg>/providers/Microsoft.ContainerService/managedClusters/<aks-name>"
    interval: 300
```

**Prerequisites**:
1. `pip install azure-identity`
2. Service principal with Reader role on the subscription
3. Env vars: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_CERTIFICATE_PATH`

### Pattern 3: Azure Service Health API (subscription-wide incidents)

Commented out in `status_feeds.py` lines 520-682. Replaces the Azure RSS feed with proper API access.

**Why upgrade from RSS**: The RSS feed is lossy — it only shows active incidents, resolved ones disappear. The history page scraping partially compensates but is fragile. The Service Health API gives:
- Real severity levels (Critical, Error, Warning, Informational)
- Lifecycle updates with timestamps
- Region-filtered incidents (only "UK South")
- Impact start/end times
- Proper pagination

**Key insight**: Azure Service Health incidents are region-based, not subscription-based. You only need Reader on ONE subscription in your tenant — the same UK South incidents appear regardless of which subscription you query from.

Config:
```yaml
status_feeds:
  - name: "Azure"
    type: azure_service_health
    subscription_id: "<any-subscription-id>"
    region: "UK South"
    interval: 300
```

**Prerequisites**:
1. `pip install azure-identity`
2. `az ad sp create-for-rbac --name "status-page-reader" --role Reader --scopes /subscriptions/<ANY_SUB_ID>`
3. Env vars: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET`

### Implementation Checklist for Private Network Work

1. **Uncomment Azure credential helper** in `checker.py` (lines 152-169)
2. **Uncomment `check_azure_resource_health`** in `checker.py` (lines 210-238)
3. **Register in CHECKERS dict** in `checker.py` (line 246-247)
4. **Add `azure-identity`** to `requirements.txt`
5. **Add private DNS targets** to `config.yaml` with `requires_env: "ON_PRIVATE_NETWORK"`
6. **Add Azure Resource Health services** to `config.yaml` groups
7. **Optionally**: Uncomment `poll_azure_service_health` in `status_feeds.py` and register in `poll_feed()` router
8. **Set env vars** on the deployment: `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_CERTIFICATE_PATH`, `ON_PRIVATE_NETWORK=1`
9. **Write tests** for the new check types (mock the Azure API responses)

---

## Security Model

**Authentication**: Session-based admin login with brute-force protection (5 attempts per IP in 5 minutes). Passwords compared with `hmac.compare_digest()` (constant-time).

**CSRF**: Session tokens for forms, `X-CSRF-Token` header for API endpoints. All mutations require CSRF validation.

**CSP**: Per-request nonces for inline scripts (`secrets.token_urlsafe(16)`). No `'unsafe-inline'` in script-src. This means Playwright's `page.wait_for_function()` breaks — use `page.evaluate()` instead.

**Headers**: `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, `Permissions-Policy: camera=(), microphone=(), geolocation=()`.

**Session cookies**: `HttpOnly`, `SameSite=Strict`, `Secure=true` (set `SESSION_COOKIE_SECURE=false` for local HTTP dev).

**Script execution**: `checker.check_script()` uses `shlex.split()`, never `shell=True`. Config.yaml must be a trusted, read-only file.

**HTTP checker**: `allow_redirects=False` to prevent SSRF via open redirects.

**Default password guard**: App refuses to start if `ADMIN_PASS` is `"changeme"` unless `ALLOW_DEFAULT_PASSWORD=1` is set.

---

## Environment Variables

### Required for Production
| Variable | Purpose |
|----------|---------|
| `ADMIN_PASS` | Admin panel password (must not be "changeme") |
| `SECRET_KEY` | Flask session signing key |

### Optional — Alerts
| Variable | Purpose |
|----------|---------|
| `ALERT_EMAIL_TO` | Comma-separated alert recipients |
| `RESEND_API_KEY` | Resend API key (preferred email transport) |
| `RESEND_FROM` | Verified sender email for Resend |
| `RESEND_REPLY_TO` | Optional reply-to address for Resend |
| `SMTP_HOST` | SMTP host (fallback transport) |
| `SMTP_PORT` | SMTP port (default `587`) |
| `SMTP_USER` | SMTP username |
| `SMTP_PASS` | SMTP password |
| `SMTP_FROM` | SMTP sender address |
| `SMTP_STARTTLS` | SMTP STARTTLS toggle (default true) |
| `SMTP_SSL` | SMTP SSL toggle (default false) |
| `SLACK_WEBHOOK_URL` | Slack incoming webhook |
| `TEAMS_WEBHOOK_URL` | Teams incoming webhook |
| `JIRA_URL` | e.g. `https://yourcompany.atlassian.net` |
| `JIRA_PROJECT` | e.g. `OPS` |
| `JIRA_USER` | Jira email |
| `JIRA_TOKEN` | Jira API token |

### Optional — Azure
| Variable | Purpose |
|----------|---------|
| `AZURE_TENANT_ID` | Azure AD tenant |
| `AZURE_CLIENT_ID` | Service principal client ID |
| `AZURE_CLIENT_CERTIFICATE_PATH` | Path to .pfx cert |
| `AZURE_CLIENT_SECRET` | Alternative to cert auth |
| `AZURE_SUBSCRIPTION_ID` | Any subscription for Service Health |

### Optional — Runtime
| Variable | Purpose |
|----------|---------|
| `ON_PRIVATE_NETWORK` | Gate private endpoint checks |
| `STATUS_DB` | SQLite path (default: `status.db`) |
| `CLEANUP_ORPHANS_ON_STARTUP` | Set `1` to delete orphan service data |
| `ALLOW_DEFAULT_PASSWORD` | Set `1` for local dev with default creds |
| `DISABLE_SCHEDULER` | Set `1` to skip scheduler (E2E test mode) |
| `SESSION_COOKIE_SECURE` | Set `false` for local HTTP |
| `GITHUB_TOKEN` | For authenticated GitHub API checks (5k req/hr) |

---

## Running and Testing

### Local Development

```bash
cd /Users/andrewnaoyamcwilliam/repos/status-page
source .venv/bin/activate

# Start with default credentials (dev only)
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
ALLOW_DEFAULT_PASSWORD=1 \
gunicorn -c gunicorn.conf.py app:app
```

The `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` is required on macOS when gunicorn forks with APScheduler.

If you use `flask run`, you're using Flask's development server (expected warning in logs). Use gunicorn to mirror production behavior.

For UI-only debugging where background checks/feed jobs create noise, you can run with scheduler disabled:

```bash
ALLOW_DEFAULT_PASSWORD=1 \
DISABLE_SCHEDULER=1 \
SECRET_KEY=dev-local-secret \
SESSION_COOKIE_SECURE=false \
flask run
```

### Running Tests

```bash
# Unit tests (fast, no browser)
pytest tests/ --ignore=tests/test_e2e.py -v

# E2E browser tests (requires playwright browsers)
pytest tests/test_e2e.py -v

# All tests
pytest tests/ -v

# Lint
ruff check .
ruff format --check .
```

### Test Architecture

- `conftest.py` sets `ALLOW_DEFAULT_PASSWORD=1`, `DISABLE_SCHEDULER=1`, and gives every test an isolated temp SQLite database
- `app_client` fixture creates a Flask test client
- `live_server` fixture (session-scoped) starts Flask in a subprocess for E2E
- `admin_session` fixture logs in via Playwright
- `seed_incidents` fixture creates test incidents via the API

---

## Deployment

**Gunicorn config** (`gunicorn.conf.py`):
- 1 worker (prevents duplicate APScheduler instances)
- 2 threads (handles concurrent requests)
- `max_requests = 1000` with jitter (worker recycling to prevent memory leaks)
- Binds to `0.0.0.0:5050`

**Systemd service** (`status-page.service`):
- Runs as `pi` user
- Reads `.env` file for secrets
- Auto-restarts on failure

**Important**: Single worker is required. Multiple workers = multiple schedulers = duplicate checks and race conditions in feed imports. The `IntegrityError` handler in `feed_importer.py` provides safety against races, but the single-worker design avoids them entirely.

---

## Common Mistakes to Avoid

1. **Don't insert synthetic check records.** If coverage is grey, that's correct. Add a feed or wait for real checks.

2. **Don't use `page.wait_for_function()` in E2E tests.** CSP nonces block it. Use `page.evaluate()` or `page.locator().evaluate()`.

3. **Don't add `'unsafe-inline'` to CSP.** Use nonces. Every `<script>` tag in templates must have `nonce="{{ csp_nonce() }}"`.

4. **Don't run multiple gunicorn workers.** APScheduler will duplicate. Keep `workers = 1`.

5. **Don't mark a check as `"up"` without a real `response_time_ms`.** The database layer will raise `ValueError`.

6. **Don't forget `requires_env` for private network targets.** Without it, targets that only resolve inside the VNet will show as "down" on public deployments.

7. **Don't confuse impact levels.** Our scale: `major` > `partial` > `minor` > `none`. The Statuspage API uses "critical" and "major" which we map to our "major" and "partial" respectively.

8. **Don't delete the `schema_migrations` table.** It tracks one-time migrations. Deleting it will re-run the impact rename migration and corrupt data.

9. **Don't use `get_request_db()` outside Flask request context.** It will raise `RuntimeError`. Use `get_db()` or `get_query_db()` in scheduler jobs and background tasks.

10. **Don't forget `OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES` on macOS.** Gunicorn fork + APScheduler + macOS = crash without it.
11. **Don't commit local runtime artifacts.** Keep local DB/log files out of commits (`status_page.db`, temp logs, Playwright traces unless debugging).
12. **Don't regress static/template paths.** Flask app initialization uses explicit absolute `template_folder` and `static_folder` to survive package layout changes.

---

## Azure Service Keyword Mapping

`status_feeds.py` contains `AZURE_SERVICE_KEYWORDS` — a dict mapping ~50 keywords to our configured Azure service names. This is how Azure RSS/history incidents get routed to the correct service bars. If you add a new Azure service, add its keywords here.

Examples:
```python
"aks": "Azure Kubernetes Service (AKS)",
"kubernetes": "Azure Kubernetes Service (AKS)",
"entra": "Azure AD / Entra ID",
"blob": "Azure Blob Storage (UK South)",
"sql database": "Azure SQL (UK South)",
"private link": "Azure Private Link",
"private endpoint": "Azure Private Link",
```

Short keywords (<=4 chars like "aks", "acr", "arm", "cdn", "dns", "vpn") use word-boundary regex matching to avoid false positives (e.g., "acr" matching "across").

---

## Theme System

Dark mode is implemented with CSS variables and a `data-theme` attribute on `<html>`:

- `static/theme.js` handles toggle, localStorage persistence, and system preference detection
- `static/style.css` uses `[data-theme="dark"]` selectors for color overrides
- The favicon changes accent color (green→blue) via JS rebuilding the SVG data URI
- The header logo uses `stroke: var(--green)` with CSS transitions
- Badge labels are normalized for clarity:
  - `operational` → `Operational`
  - `degraded` / `partial_outage` / `under_maintenance` → `Degraded`
  - `major_outage` → `Outage`
  - `no_data` → `No Data`

---

## Telemetry

In-process only (no external service). Thread-safe counters and timings exposed at `/api/metrics` (admin-only).

Counters: `checks.total`, `checks.status.up`, `checks.status.down`, `checks.skipped`, `backfill.feed.success`, `backfill.feed.error`

Timings: `checks.response_time_ms`, `jobs.{function_name}.ms`

---

## API Endpoints

| Method | Path | Auth | Purpose |
|--------|------|------|---------|
| GET | `/` | No | Main status page |
| GET | `/api/health` | No | JSON health status for all services |
| POST | `/api/incidents` | Yes + CSRF | Create incident |
| PATCH | `/api/incidents/<id>` | Yes + CSRF | Update incident |
| GET | `/api/metrics` | Yes | Telemetry snapshot |
| GET/POST | `/admin/login` | No | Login form |
| POST | `/admin/logout` | CSRF | Logout |
| GET | `/admin` | Yes | Admin dashboard |
| POST | `/admin/declare` | Yes + CSRF | Declare incident |
| POST | `/admin/update/<id>` | Yes + CSRF | Update incident |
| POST | `/admin/backfill` | Yes + CSRF | Trigger feed backfill |
| POST | `/admin/test-email` | Yes + CSRF | Send test email using configured transport |
| GET | `/admin/feed-coverage` | Yes | Feed coverage JSON |

---

## Agent PR Checklist

Before opening/merging:

1. Run `ruff check .` and `ruff format --check .`
2. Run `pytest tests --ignore=tests/test_e2e.py -v`
3. If UI or CSP touched, run `pytest tests/test_e2e.py -v`
4. Validate no synthetic "up without response time" paths were introduced
5. Validate off-VPN private checks are `skip`, not `down`
6. Confirm no generated/local files are staged

---

## VPN Rollout Plan (Work Laptop + Private Checks)

### Objective

Enable and validate private-network monitoring safely on your work laptop/VPN, without regressing existing public-feed behavior or creating noisy alerts.

### Phase 0: Preflight (Before touching config)

1. Confirm runtime baseline on work laptop.
   - `python3 --version`
   - `pip --version`
   - `./.venv/bin/python -V` (after venv setup)
   - `sqlite3 --version`
2. Pull latest `main`.
   - `git pull origin main`
   - Ensure clean tree: `git status --short` should be empty.
3. Confirm secret posture.
   - `ADMIN_PASS` strong, not default.
   - `SECRET_KEY` set.
   - Resend/SMTP configured.
   - `ALLOW_DEFAULT_PASSWORD` not set in production-like run.
4. Confirm you can reach private DNS/targets when VPN is up.
   - `nslookup <private-hostname>`
   - `dig <private-hostname>`
   - `curl -I https://<private-endpoint>` (if HTTP)
   - `nc -zv <host> <port>` (if TCP)

### Phase 1: Work Laptop Deployment Mode

1. Set env for VPN-aware monitoring.
   - `ON_PRIVATE_NETWORK=1`
   - production-like auth/env vars in `.env` (or shell export)
2. Run app in production-like mode (recommended locally).
   - Gunicorn path, single worker (scheduler safety):
```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
ADMIN_PASS=... SECRET_KEY=... SESSION_COOKIE_SECURE=false \
ON_PRIVATE_NETWORK=1 \
gunicorn -c gunicorn.conf.py app:app
```
3. Confirm app/scheduler health.
   - `/` loads
   - `/admin` login works
   - logs show scheduler started and checks running

### Phase 2: Add Private Checks Safely (`requires_env`)

1. Start with 1-2 low-risk private checks first.
   - DNS private endpoint
   - one HTTP/TCP internal dependency
2. Add with explicit gating in `status_page/config.yaml`.
   - DNS target example:
```yaml
- hostname: "mydb.privatelink.database.windows.net"
  label: "DB Private Link"
  requires_env: "ON_PRIVATE_NETWORK"
```
   - Service example:
```yaml
- name: "Internal API"
  type: http
  url: "https://internal-api.company.local/health"
  requires_env: "ON_PRIVATE_NETWORK"
  interval: 60
```
3. Keep intervals conservative initially.
   - 60s is fine for key checks.
   - 120-300s for heavier endpoints.
4. Validate config before run.
   - `./.venv/bin/pytest -q tests/test_config_schema.py`
   - restart app after config changes.

### Phase 3: Immediate Functional Validation (first 30-60 min)

1. Verify private checks are active on VPN.
   - New check rows appear in DB/logs.
   - Status badges reflect real current state.
   - No accidental down from unreachable-off-VPN if gating is set.
2. Verify `requires_env` behavior explicitly.
   - Stop app, unset `ON_PRIVATE_NETWORK`, restart.
   - Private checks should be `skip` (not down).
   - Re-enable `ON_PRIVATE_NETWORK` and confirm checks resume.
3. Verify alert path once manually.
   - Declare and resolve one test incident from `/admin`.
   - Confirm email arrival and log entries.

### Phase 4: 24–48h Observation Window

#### What to watch

1. Private checks on VPN
   - expected latency and success rate
   - no false negatives from DNS flaps
2. Public feed behavior
   - active feed incident => alert
   - resolution transition => resolution alert
   - historical backfill => no spam
3. Data integrity/UI semantics
   - no synthetic data
   - pre-coverage days remain grey
   - in-coverage/no-incident days show green

#### Suggested cadence

1. T+2h: quick sanity pass
2. T+12h: overnight behavior check
3. T+24h and T+48h: final review + decide promotion

### Phase 5: Acceptance Criteria

Ship-ready when all are true:

1. Private checks stable over 24–48h.
2. No misleading status transitions (especially VPN-dependent services).
3. Email alerts received for:
   - manual declare/resolve
   - active public-feed incident + resolution
4. No backfill alert spam.
5. Admin UX clear:
   - email transport/recipient visibility
   - test email action works
   - feed coverage readable

### Phase 6: Operational Hardening

1. Key hygiene
   - rotate any exposed API keys
   - keep only active keys
2. Alert noise controls
   - tune intervals/thresholds if needed
   - avoid over-broad private targets initially
3. Backups
   - daily SQLite backup job
   - keep last N backups
4. Change discipline
   - make one config change set at a time
   - validate before adding more services

### Phase 7: Rollback Plan (if noisy/broken)

1. Quick rollback
   - revert latest config commit
   - restart service
2. Narrow rollback
   - disable only failing private checks
   - keep public feeds running
3. Data safety
   - never fabricate checks
   - preserve DB; restore from backup only if needed

### Suggested Additions for Agent Sessions

Add a small validation matrix in PR descriptions or notes with:

1. `Service`
2. `Type (dns/http/tcp)`
3. `requires_env`
4. `Expected on VPN`
5. `Expected off VPN`
6. `Alert expected?`
7. `Validated (Y/N)`

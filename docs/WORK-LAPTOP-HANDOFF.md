# Work Laptop Handoff Runbook

Use this document for execution. For architecture/background, read `docs/PROJECT.md`.

## Goal

Bring `status-page` from personal laptop context into work laptop + private VPN context, then validate private checks safely.

## Preconditions

1. VPN access is available and working.
2. Repo is up to date on `main`.
3. Runtime secrets are available (`ADMIN_PASS`, `SECRET_KEY`, alerting env vars).
4. Work laptop has Python + venv support.

## Setup

1. Clone/pull latest:
```bash
cd ~/repos/status-page
git pull origin main
```

2. Create/update venv and deps:
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

3. Set runtime env (example):
```bash
export ADMIN_PASS='...'
export SECRET_KEY='...'
export SESSION_COOKIE_SECURE=false
export ON_PRIVATE_NETWORK=1
```

## Start App (Production-like Local)

```bash
OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES \
ADMIN_PASS="$ADMIN_PASS" \
SECRET_KEY="$SECRET_KEY" \
SESSION_COOKIE_SECURE=false \
ON_PRIVATE_NETWORK=1 \
gunicorn -c gunicorn.conf.py app:app
```

## First Private Checks to Add

Start small (1-2 checks), then scale:

1. Private DNS endpoint
2. One internal HTTP or TCP health endpoint

Example in `status_page/config.yaml`:

```yaml
dns_bar:
  targets:
    - hostname: "mydb.privatelink.database.windows.net"
      label: "DB Private Link"
      requires_env: "ON_PRIVATE_NETWORK"

services:
  - name: "Internal API"
    type: http
    url: "https://internal-api.example.local/health"
    requires_env: "ON_PRIVATE_NETWORK"
    interval: 60
```

## Validation Checklist

### On VPN

1. `/` and `/admin` load.
2. Private checks run and produce real results (not grey forever).
3. No false failures from DNS/TCP policy issues.

### Off VPN

1. Unset `ON_PRIVATE_NETWORK` and restart app.
2. Private checks are skipped (not marked down).
3. Public checks/feeds continue normally.

## Alerting Validation

1. Manual declare + resolve from `/admin` sends alerts.
2. Public feed active incident (if one occurs) sends alert.
3. Public feed resolution sends resolution alert.
4. Backfill of historical resolved incidents does not spam.

## Observation Window (24-48h)

Monitor:

1. Private check stability and latency.
2. Alert quality (signal/noise).
3. UI correctness for no-data vs inferred coverage.

## Acceptance Criteria

1. Private checks behave correctly on VPN and off VPN.
2. Alerting behavior matches expectations.
3. No noisy backfill spam.
4. No synthetic/incorrectly inferred data introduced.

## Rollback

1. Revert latest config changes:
```bash
git checkout -- status_page/config.yaml
```

2. Restart service/process.

3. Keep only public feed/public checks if private rollout is unstable.

<h1 align="center">
  <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" width="28" height="28" aria-hidden="true">
    <path d="M32 6L4 20l28 14 28-14Z" fill="#E04343"/>
    <path d="M4 26l28 14 28-14" fill="none" stroke="#E86235" stroke-width="4.5" stroke-linejoin="round"/>
    <path d="M4 36l28 14 28-14" fill="none" stroke="#FAA72A" stroke-width="4.5" stroke-linejoin="round"/>
    <path d="M4 46l28 14 28-14" fill="none" stroke="#76AD2A" stroke-width="4.5" stroke-linejoin="round"/>
  </svg>
  status-page
</h1>

Lightweight Flask status dashboard for internal/public service visibility.

## What It Does
- Monitors services with HTTP/TCP/DNS/script checks
- Imports real vendor incidents (GitHub, Azure, Docker, Red Hat)
- Shows 90-day uptime bars with strict no-synthetic-data behavior
- Supports admin incident workflows + alerts (Email/Slack/Teams/Jira)

## Quick Start
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export ALLOW_DEFAULT_PASSWORD=1
export ADMIN_PASS=testpass
export SECRET_KEY=dev-local-secret
export SESSION_COOKIE_SECURE=false

flask run
```

Open: `http://127.0.0.1:5000`

## Docs
- `docs/PROJECT.md` — full architecture and operational guide
- `docs/WORK-LAPTOP-HANDOFF.md` — handoff plan for work/VPN setup
- `docs/ENGINEERING-ISSUES.md` — engineering backlog and refactor notes

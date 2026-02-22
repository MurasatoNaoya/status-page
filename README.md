<h1 align="center">
  <img src="status_page/static/favicon.svg" width="26" height="26" alt="" />
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

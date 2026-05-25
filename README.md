![status-page banner](docs/assets/readme-banner.svg)

Lightweight Flask status dashboard for internal/public service visibility.

## What It Does
- Monitors services with HTTP/TCP/DNS/script checks
- Imports real vendor incidents (GitHub, Azure, Docker, Red Hat)
- Shows 90-day uptime bars with strict no-synthetic-data behavior
- Supports admin incident workflows + alerts (Email/Slack/Teams/Jira)

## Tech Stack
- Python 3, Flask, Gunicorn, APScheduler
- SQLite (WAL mode) for checks/incidents/updates
- Jinja templates + lightweight vanilla CSS/JS

## Design Principles
- **No synthetic health data**: unknown history stays grey, never fabricated green.
- **Operationally simple**: one process model, SQLite backups, straightforward deploy.
- **Practical extensibility**: feed adapters, check types and alert transports are modular.

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
Default admin login is `admin` / `testpass` in the example above (dev only).

## Docs
- `docs/PROJECT.md` — full architecture and operational guide
- `docs/WORK-LAPTOP-HANDOFF.md` — handoff plan for work/VPN setup
- `docs/ENGINEERING-ISSUES.md` — engineering backlog and refactor notes

## Security and Support
- Security policy and vulnerability reporting: `SECURITY.md`
- Support model: best-effort community support via GitHub issues (no SLA)

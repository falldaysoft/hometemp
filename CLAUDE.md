# hometemp

Compares HomeKit room temperatures with the thermostat and its settings, measures heating and cooling effectiveness (run time, recovery, heat loss), and estimates cost from the weather forecast. The house is split into two HVAC zones (Home side and Office side). See `PLAN.md` for the product plan and the collector spike's findings.

## Layout
- `hometemp/`, `core/`, `templates/`: the Django 6 site (HTMX + Alpine + Tailwind via CDN, no build step).
- `collector/`: a Mac Catalyst app that records HomeKit events (XcodeGen: `cd collector && xcodegen generate`). It runs on idlemac.local as a LaunchAgent. HomeKit only works in an app in a logged-in session.

## Local dev
- The Python venv is `.venv`. `DEBUG=true python manage.py migrate && DEBUG=true python manage.py runserver` uses SQLite.

## Deployment
- Docker → GHCR (`ghcr.io/falldaysoft/hometemp`) → docker compose on the Oracle VM (`ovm.fallday.ca`, arm64). Pushes to `main` deploy, except changes only to `collector/**` or `*.md`.
- CI SSHes with a restricted `OVM_DEPLOY_KEY` that can only run `~/apps/hometemp/deploy.sh`.
- `deploy/` is the source of truth for `~/apps/hometemp/`, but CI doesn't copy it: `scp` changes by hand.
- Traefik terminates TLS. Migrations run on container start.
- The shared `postgres` container holds the `hometemp` role and database. Secrets live only in the VM's `.env`.
- Work is tracked in the bots.fallday.ca room **falltemp** (`fdbot`; task refs are `falltemp.N`).

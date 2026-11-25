# GAB Environment Platform

Seeds and resets Google test accounts for **GAB (GeminiApp Bench)** tasking, so every task starts from the same persona baseline.

- **Seed:** fill an account with a persona's Gmail, Calendar, Drive, Contacts and Tasks (Google APIs), and Keep (browser worker).
- **Reset:** after a task, put the account back to that baseline. Taskers trigger it from Cosmo; it ends with a Session ID they paste back into Cosmo.
- **Verify:** compare the account with the persona after every seed and reset.
- One FastAPI service (`api/`), one EC2 host, Supabase for records, a Mac running the Keep worker.

## Live sites (`https://<your-domain>`)

| Path | Who | What |
|---|---|---|
| `/onboard` | Operators | Classic personas: load accounts CSV, authorize, upload/reseed/clean, Explain failures |
| `/wave2` | Operators | Wave 2 personas (13): authorize with Contacts+Tasks, Keep step, seed, live counts |
| `/reset` | Taskers (from a Cosmo link) | Google sign-in, "Did this task use Google Keep?", reset, Session ID |
| `/managers` | QMs | Add/remove the freelancers allowed to sign in on `/reset` |
| `/logins` | Operators | Manage the `/reset` sign-in allow-list |
| `/qc` | QC | Read-only reset logs for a task |
| `/healthz` | Monitoring | Service health |

## Repo map

| Path | What it is |
|---|---|
| `api/reset_service/` | The service: routes (`app.py`), settings (`config.py`), engine adapter (`engine.py`), OAuth/client handling (`upload.py`), signed reset links (`links.py`), Supabase store (`db.py`), Keep job queue (`jobs*.py`), Keep check (`wave2_keep.py`), failure explainer (`diagnostics.py`) |
| `api/static/` | The web pages listed above |
| `api/tests/` | API tests (unittest, offline with `GAB_RESET_SIMULATE=1`) |
| `seeder/materialize/` | Classic seeding pipeline: OAuth/DWD auth, Gmail/Calendar/Drive sync, verify, `provision/` queue |
| `seeder/wave2/` | Wave 2: persona loader, credentials, seed/restore/wipe runner |
| `seeder/pilot10/` | Google's reference v2 seeder (`gab_seed.py`, used by `seeder/wave2`) |
| `engine/` | Legacy `gab-seed` CLI (only used when `GAB_CONFIG` is set) |
| `db/schema.sql` | Supabase schema (all tables) |
| `deploy/` | systemd unit, nginx config, EC2 bootstrap, env template |

## Run locally

```bash
./setup.sh                                   # builds the virtualenvs
cp deploy/.env.ec2.example .env              # fill in values (never commit .env)
cd api && .venv/bin/uvicorn reset_service.app:app --port 8791
```
- Tests: `cd api && GAB_RESET_SIMULATE=1 .venv/bin/python -m unittest discover -s tests`
- `GAB_RESET_SIMULATE=1` runs without touching Google.

## Deploy

See [docs/RUNBOOK.md § Deploy](docs/RUNBOOK.md#8-deploy-a-change). One systemd service (`gab-api`) on the EC2 host; restart only when no seed or reset is running.

## Documentation

| Doc | Read it for |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | How the system works: components, flows, data, auth, modes, settings |
| [docs/RUNBOOK.md](docs/RUNBOOK.md) | Step-by-step operations, from a new Google project to a tasker's reset; troubleshooting |
| [docs/STATUS.md](docs/STATUS.md) | What is deployed now, known issues, open risks |
| [docs/DOCS_INDEX.md](docs/DOCS_INDEX.md) | Every other doc, sheet, site and repo, and whether it is current |
| [docs/WAVE2_SEEDING.md](docs/WAVE2_SEEDING.md) | Wave 2 seeding design detail |
| [docs/MODULE_JOBS.md](docs/MODULE_JOBS.md) | Keep job queue API detail |

Access details (host, VPN, key locations) are kept in the team's private handover sheet, not in this repo.

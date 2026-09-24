# GAB Environment Platform

A service that **seeds and resets Google accounts** (Gmail, Drive, Calendar, GitHub-as-Drive)
to an identical baseline for LLM benchmarking. Between model runs, an account is reset so every
run starts from the same environment.

It is **one FastAPI service** (`api/`, port **8791**) that:
- **Onboards** accounts: upload an OAuth `client.json`, authorize each Google account, seed its
  environment from the archive.
- **Resets** an account back to baseline — automatically choosing **delta** (surgical) or
  **reseed** (full wipe + seed).
- Records everything in **Supabase** (`gab_accounts`, `reset_sessions`).

---

## How it works

```
Onboard ─▶ upload client.json ─▶ authorize account (OAuth) ─▶ seed (writes a manifest)
                                                                   │
Reset ─▶ POST /api/environment/reset ─▶ auto-route:                ▼
           • complete manifest  → DELTA  (restore only the diff)
           • else               → RESEED (wipe + fresh seed)   ── runs the engine (gab-seed)
         records the op in reset_sessions
```

- **Manifest** — per-(account, persona) JSON recording every seeded object's id + fingerprint.
  Delta reconciles against it; if it's incomplete/missing/mismatched, the platform **auto-reseeds**.
- **Persona cache** — decodes each persona's Drive files once, reused across accounts (bulk speedup).
- Full logic reference: [`docs/PLATFORM_LOGIC_REFERENCE.md`](docs/PLATFORM_LOGIC_REFERENCE.md).

### Components
| Folder | Role |
|---|---|
| `api/` | The platform: API + UI pages. Imports the seeder in-process for OAuth; runs the engine for seeding. |
| `engine/` | The reset engine (`gab-seed`) — seed / delta / reseed / reset. |
| `seeder/` | OAuth helpers (used by `api/`) + a standalone bulk seeder (optional). |
| `db/schema.sql` | The two Supabase tables. |
| `docs/` | Logic reference + client/OAuth setup docs. |

---

## Pages (open in a browser)
| URL | Purpose |
|---|---|
| `/onboard` | Load accounts (CSV), upload `client.json`, authorize + upload per account. |
| `/onboard/authorize` | Authorize workspace — passwords shown for copy-paste, authorize all + bulk upload. |
| `/reset` | Freelancer reset page — one account back to baseline, with a live progress card. |
| `/qc` | QC review — what each reset/upload did (per-module counts). |
| `/` | Dashboard — accounts + recent operations. |

## API endpoints (JSON; `/api/*` require `Authorization: Bearer <RESET_API_KEY>`)
| Method + path | Purpose |
|---|---|
| `POST /api/environment/upload` | Start first upload (returns an OAuth consent URL). |
| `GET  /api/environment/upload/{id}` | Poll upload status. |
| `POST /api/environment/reset` | Reset an account (`{email, persona?, task_allocation_id}`). |
| `GET  /api/environment/reset/{id}` | Poll reset status. |
| `POST /api/reset-link` | Mint a signed, tamper-proof freelancer reset link. |
| `GET  /api/qc/{task_allocation_id}` | Fetch the QC log for a task. |
| `POST /api/qc/{task_allocation_id}/confirm` | Purge a QC log (admin). |
| `GET  /healthz` | Health check (no auth). |

---

## Run it

**Prerequisites:** Python 3.11+, a Supabase project.

```bash
# 1. Build the three virtualenvs (engine, api, seeder)
bash setup.sh

# 2. Create the tables — run db/schema.sql in the Supabase SQL editor

# 3. Configure — copy the template and fill it in
cp .env.example .env      # then edit .env

# 4. Start the platform
cd api && .venv/bin/uvicorn reset_service.app:app --host 127.0.0.1 --port 8791
```
Open **http://127.0.0.1:8791/onboard**.

### Quick test without real Google (simulate mode)
To click through the whole UI/flow without OAuth, credentials, or the archive:
```bash
# in .env set:  GAB_RESET_SIMULATE=1
cd api && .venv/bin/uvicorn reset_service.app:app --port 8791
```
Resets/uploads then complete instantly (no Google calls) so the pages, flow, and DB writes can be
exercised end-to-end. Set `GAB_RESET_SIMULATE=0` for real runs.

### For real runs you also need
- An **OAuth `client.json`** (Web application type) — uploaded on `/onboard`.
- The **environment archive** (`.zip`) and an **engine `config.json`** (its `archive`, `token_dir`,
  `state_dir`, and `accounts`), pointed at by `GAB_CONFIG`.
- Google **test accounts** to authorize.

---

## Config (`.env`)
| Key | Meaning |
|---|---|
| `SUPABASE_URL`, `SUPABASE_KEY` | Supabase project + service-role key (server-side only). |
| `SUPABASE_TABLE`, `SUPABASE_ACCOUNTS_TABLE` | `reset_sessions`, `gab_accounts`. |
| `RESET_API_KEY` | Bearer secret for `/api/*`. |
| `PUBLIC_BASE_URL` | Public origin; the OAuth redirect derives from it. |
| `RESET_LINK_SECRET` | Signs freelancer reset links (unset = dev raw-id fallback). |
| `GAB_SEED_BIN` | Path to the engine's `gab-seed` (in `engine/.venv`). |
| `GAB_CONFIG` | Engine `config.json` (archive, token_dir, state_dir, accounts). |
| `GAB_RESET_MODE` | Default op (`delta`; auto-routes to reseed when needed). |
| `GAB_DRIVE_CACHE` | `1` = per-persona Drive cache (bulk speedup). |
| `GAB_RESET_SIMULATE` | `1` = simulate (no Google calls) for testing. |
| `GAB_PERSONA_ROOT` | Persona archive tree the seeder reads. |

## Tests
```bash
( cd api    && .venv/bin/python -m unittest discover -s tests )   # platform
( cd engine && .venv/bin/python -m pytest -q )                    # reset engine
```

# GAB Environment Platform — simple operator guide

This is the “how do I actually run this” doc. No jargon beyond what you need.

There is **one app**. Start it on port **8791**. That one process does upload, reset, and login-user lists.

```
http://127.0.0.1:8791
```

---

## What we changed recently (this local work)

- **Upload and reset live on the same server** (`:8791`). You do not need a second app just to reset.
- **Reset uses the same POST** as Cosmo: `POST /api/environment/reset`. Response has `url`, `status`, `error`, `reset_session_id`.
- **Same persona → delta** (only fix what drifted). **Different persona → reseed** (wipe + full upload). That decision is supposed to come from Supabase `gab_accounts`.
- **Login users are not the accounts we reset.** Reset targets stay in `gab_accounts`. People allowed to open the reset page stay in **`freelancers`**.
- **Google sign-in on the reset/status page is off for now** so you can test reset. The login code is still there to turn back on later.
- If Supabase **refuses a write** (wrong key), the API saves the reset session **on this machine** so the job can still run. Status can look “stuck queued” if you only look in Supabase.
- Seeder reset now **finds the local GitHub folder** under the persona (`…/services/github`). Without that, reset died immediately.
- `.env` must use the Supabase **service_role** key (starts with `eyJ`). A publishable / `sb_publishable_` / `sb_secret_` key can **talk** to Supabase but **cannot see or write** the real rows. That is why `test02gemini@gmail.com` uploaded on disk but **never landed in `gab_accounts`**.

---

## How to start (for upload)

1. Tables already exist in the shared Supabase project. You do not create a new project.
2. Fill `GAB_Env_Reset_Upload/.env`:
   - `SUPABASE_URL` — this project
   - `SUPABASE_KEY` — **service_role** JWT (`eyJ…`), not the publishable key
   - `RESET_API_KEY` — secret for POST APIs
   - `GAB_PERSONA_ROOT` — folder of persona archives (Gmail/Drive/Calendar/GitHub files)
   - `PUBLIC_BASE_URL=http://127.0.0.1:8791` locally
3. Start the app:

```bash
cd /Users/jahnaviab/Desktop/Env_Reset_upload/GAB_Env_Reset_Upload/api
.venv/bin/uvicorn reset_service.app:app --host 127.0.0.1 --port 8791
```

4. Open **http://127.0.0.1:8791/onboard**
5. Upload the Google OAuth web `client.json` if asked.
6. Add the Gmail to seed (CSV or one row: email + persona).
7. **Authorize** that Google account (browser Google login / consent).
8. **Upload / seed** — this writes Gmail, Drive, Calendar, and GitHub-as-Drive from the persona folder.

After a good authorize, the email should appear in Supabase **`gab_accounts`**. If it does not, the key cannot write. Upload can still finish on this laptop (token + files). Reset will not know the last persona until that row exists.

Optional older UI: seeder on **http://127.0.0.1:8765** does the same kind of upload. Prefer **8791 /onboard** so everything is one server.

---

## How to POST a reset

Need: API running, account already seeded, `RESET_API_KEY` from `.env`.

```bash
curl -sS -X POST http://127.0.0.1:8791/api/environment/reset \
  -H "Authorization: Bearer <RESET_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "email": "test02gemini@gmail.com",
    "persona": "backend_software_engineer",
    "task_allocation_id": "local-test"
  }'
```

You get JSON like:

```json
{
  "url": "http://127.0.0.1:8791/api/environment/reset/<id>",
  "status": "in_progress",
  "error": null,
  "reset_session_id": "<id>"
}
```

Open the **status page** (browser):

```
http://127.0.0.1:8791/reset/status/<reset_session_id>
```

Or poll JSON (same Bearer):

```
GET /api/environment/reset/<reset_session_id>
```

**What mode you get**

| Situation | What happens | How long (rough) |
|---|---|---|
| Same persona as last time on `gab_accounts` | **Delta** — compare live account to the seed **manifest**, fix only missing/wrong items | Minutes if little changed |
| Different persona, or no row / no last persona in Supabase | **Reseed** — wipe + full upload again | Often 30–90 min (thousands of jobs, Gmail quota) |

Persona names like `backend_software_engineer` and `Backend_software_engineer` count as the **same**.

Right now sign-in is off, so the status link opens with no Google login.

---

## How to add users who may log in for reset

These people are **not** the Gmail inboxes we reset. They are operators / freelancers who would be allowed to open the reset page when login is turned back on.

**Table:** Supabase **`freelancers`** (`email`, optional `name`, `active`).

**One email (API):**

```bash
curl -sS -X POST http://127.0.0.1:8791/api/logins \
  -H "Authorization: Bearer <RESET_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{"email": "jahnavi@deccan.ai", "name": "Jahnavi"}'
```

**Many emails (CSV, one address per line or an `email` column):**

```bash
curl -sS -X POST http://127.0.0.1:8791/api/logins/csv \
  -H "Authorization: Bearer <RESET_API_KEY>" \
  -F "file=@logins.csv"
```

**UI:** http://127.0.0.1:8791/logins

You can also insert the row in Supabase **Table Editor → freelancers**.

Writes only work with the **service_role** key. With a publishable key the list looks empty even if you added someone in the dashboard (RLS hides the row).

Login on `/reset` and `/reset/status/...` is **disabled for now**. Adding emails now just fills the table for later.

---

## Which database stores what

### Supabase (cloud) — the shared “who / what happened” DB

Same project for everyone. Not the 10k job queue.

| Table | What it is | Example |
|---|---|---|
| **`gab_accounts`** | Google accounts that get **seeded and reset**. Last persona lives here. | `test02gemini@gmail.com` + `Backend_software_engineer` |
| **`reset_sessions`** | One row per upload/reset job (id, status, error, mode) | `e071fc9b-…` running / completed / failed |
| **`freelancers`** | People allowed to **sign in** to the reset page | `jahnavi@deccan.ai` |
| **`gab_logins`** | Old unused login table. **Do not use.** Sign-in list is `freelancers`. | — |

`gab_accounts.password` is for the Gemini/demo password cheat-sheet, **not** for reset login.

### Local on this machine — the heavy / secret stuff

| Where | What |
|---|---|
| `seeder/tokens/*.json` | Google OAuth tokens (the real login to that Gmail) |
| `seeder/runs/<id>/` | Run folder + **manifest** (ids/fingerprints of what was seeded) |
| `seeder/runs/.../provision.sqlite` | Job queue for one upload/reset (thousands of Drive/Gmail/Calendar jobs). **Not** copied to Supabase. |
| Persona folder (`GAB_PERSONA_ROOT`) | Source files: email JSON, Drive tree, calendar JSON, GitHub folder |
| Drive cache under the seeder | Speeds up Drive uploads for the same persona |
| `api/.reset_sessions.json` | Local copy of sessions if Supabase write is denied |
| `seeder/credentials.json` | Google OAuth **web client** (client id + secret) |

SQLite here is a **work queue**, not “the database of record.” Supabase is the shared record of accounts and sessions.

---

## What is local vs what is the DB (one picture)

```
You POST reset
        │
        ▼
API on this laptop (:8791)
        │
        ├─ Asks Supabase: who is this email? last persona?
        │     gab_accounts  →  same persona? delta : reseed
        │
        ├─ Writes a reset_sessions row in Supabase
        │     (or local .reset_sessions.json if the key cannot write)
        │
        └─ Runs the worker on THIS machine
              reads token from seeder/tokens
              reads persona files from disk
              compares to the local manifest for DELTA
              talks to Google (Gmail / Drive / Calendar)
              job list lives in local SQLite
```

| Question | Answer |
|---|---|
| Which Gmail do we reset? | Supabase `gab_accounts` (or a local token if the row was never saved) |
| Same persona or switch? | Supabase `gab_accounts.persona` / `last_reset_persona` |
| Who may sign in later? | Supabase `freelancers` |
| Did this reset finish? | Supabase `reset_sessions` (or local JSON if writes failed) |
| What files go back into Drive/Gmail? | **Local** persona folder |
| What is the “correct” baseline to compare? | **Local** manifest from the first seed |
| How do we log into that Gmail? | **Local** token file |
| 9,000 small jobs — where? | **Local** SQLite, not Supabase |

---

## Keys you actually need

| Key in `.env` | What it is |
|---|---|
| `SUPABASE_URL` | Your Supabase project URL |
| `SUPABASE_KEY` | **service_role** (`eyJ…`). Server only. Lets the API read/write tables. |
| `RESET_API_KEY` | Password for `POST /api/environment/reset` and `/api/logins` |
| `GAB_PERSONA_ROOT` | Path to persona archives on disk |
| `GOOGLE_CLIENT_ID` | Optional. For Google sign-in later. Public, not a secret. |

Get service_role: [Project Settings → API → Legacy / JWT keys → service_role](https://supabase.com/dashboard/project/tamrkayujdgnrbrhbsxz/settings/api)

Never put service_role in the browser. Never commit `.env`.

---

## Quick “is it working?” checks

```bash
# App up?
curl -sS http://127.0.0.1:8791/healthz

# After service_role is in .env and API restarted:
# Table Editor → gab_accounts should show test02gemini@gmail.com
# If that row is missing, add it (email + persona) or authorize again.
```

Then POST reset again and open the **new** `reset_session_id` status URL. Do not refresh an old queued link.

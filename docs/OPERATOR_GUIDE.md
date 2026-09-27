# GAB Environment Platform — simple operator guide

This is the “how do I actually run this” doc. No jargon beyond what you need.

There is **one app**. Start it on port **8791**. That one process does upload, reset, and login-user lists.

```
http://127.0.0.1:8791
```

---

## Architectural changes (how the system is shaped now)

### Before (two apps)

```
Browser / curl
    │
    ├─ Seeder UI :8765     →  first upload (OAuth, seed Gmail/Drive/Calendar)
    │                         tokens + SQLite jobs + manifest on disk
    │
    └─ Reset API :8791     →  reset only, expected a separate engine config (GAB_CONFIG)
                              easy to be “up” while the other app was down
```

Login emails and reset Gmails were easy to mix up. Reset did not always share the same OAuth tokens the seeder just saved.

### After (one platform)

```
Browser / Cosmo / curl
              │
              ▼
     One FastAPI process :8791
     ┌─────────────────────────────────────────────┐
     │  /onboard     first upload (OAuth + seed)   │
     │  POST /api/environment/reset   Cosmo reset  │
     │  /reset/status/<id>            progress UI  │
     │  /logins + POST /api/logins    who may sign in │
     │                                             │
     │  Seeder code is imported *inside* this app  │
     │  (same tokens, same persona folder)         │
     │  If GAB_CONFIG is empty, reset uses that    │
     │  same seeder pipeline (not a second server) │
     └───────────────┬─────────────────────────────┘
                     │
         ┌───────────┴───────────┐
         ▼                       ▼
   Supabase (shared)      This machine (heavy work)
   gab_accounts           tokens, persona files
   reset_sessions         manifest, SQLite job queue
   freelancers            Drive cache
```

The old seeder on **:8765** can still run, but it is optional. Prefer **:8791** so upload and reset are one process.

### Split that used to be muddy

| Piece | Old habit | Now |
|---|---|---|
| Who gets **reset** (the Gmail inbox) | Sometimes mixed with “who can open the page” | **`gab_accounts` only** |
| Who may **sign in** to the reset page | Same table / leftover `gab_logins` | **`freelancers` only** (login UI is off until you turn it back on) |
| Job of 9,000 Drive/Gmail pieces | Tempting to dump into Supabase | Stays **local SQLite**. Supabase only stores account + session status |
| Same vs different persona | Easy to always full-wipe | **Same persona → delta**, **switch → reseed**, using last persona on `gab_accounts` |
| Cosmo / POST contract | Ad-hoc | Always `{ url, status, error, reset_session_id }` |
| Engine config | Reset died if `GAB_CONFIG` was empty | Empty config → **seeder pipeline fallback** on the same server |
| Supabase key | Publishable key “connected” but saw zero rows | Needs **service_role**. Otherwise writes 401 and sessions fall back to a **local JSON** file |

### How a reset is decided (architecture, not UI)

1. Cosmo (or you) **POST**s email + persona to `:8791`.
2. API reads **`gab_accounts`** in Supabase for that email.
3. **Same persona** as stored → **delta** (read the **local manifest**, only push the diff).
4. **Different persona** or **no row** → **reseed** (wipe Google, full upload from the **local** persona folder).
5. Worker runs **on this machine**. Google is called from here. Supabase only gets “queued / running / failed / done.”

### What we did *not* change

- Persona archives still live on disk (`GAB_PERSONA_ROOT`).
- Google tokens still live in `seeder/tokens/`.
- Same Supabase **project** (no new cloud DB).
- GitHub for the environment is still the **Drive “Github” folder** from the persona tree, not a new github.com app.

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

---

## What is yet to be done

Not finished / not proven yet. Do these before calling the platform “production ready.”

### Must do before delta will work

| Item | Why it is still open |
|---|---|
| Put the real **service_role** JWT in `.env` (`eyJ…`) and restart `:8791` | `sb_publishable_` / `sb_secret_` still cannot see `gab_accounts`. Reset thinks there is no last persona and **reseeds**. |
| Confirm **`test02gemini@gmail.com` exists in Supabase `gab_accounts`** with persona `Backend_software_engineer` | First upload never wrote the row (401). Until that row exists, same-persona **delta cannot run**. Add it in Table Editor or authorize again after the good key. |
| **POST reset once more** and check the log says `mode=delta` | We have not yet seen a successful same-persona delta on this machine. |
| Watch the **new** status URL, not an old queued one | Old session ids stay “waiting” because they were saved locally and never updated in Supabase. |

### Login (you said you will do this later)

| Item | Notes |
|---|---|
| Turn **Google sign-in back on** for `/reset` and `/reset/status/<id>` | Code exists; the gate is **off** so you can test reset. |
| Register Google redirect `http://127.0.0.1:8791/ui/google/callback` (and localhost) on the OAuth web client | Needed when login uses “Google email + password.” |
| Add `http://127.0.0.1:8791` as a JavaScript origin if you use the Google button | Already listed in `seeder/credentials.json`; confirm in Google Cloud Console. |
| Prove **`jahnavi@deccan.ai` (or whoever) is in `freelancers`** and the API can **read** that row | Same key problem: empty list = RLS, not “user missing.” |
| Decide: people not on `freelancers` must get **403** on the status link | Logic was written, then bypassed. Re-enable the gate when login ships. |

### Data / keys hygiene

| Item | Notes |
|---|---|
| Stop using the publishable key in `.env.example` as if it were the server key | Easy to copy the wrong key again. |
| After a successful seed/reset, **`last_reset_persona` must stay updated** in `gab_accounts` | Writes fail today, so the next reset cannot trust that column. |
| Drop or ignore unused **`gab_logins`** so nobody writes login emails there | Sign-in list is **`freelancers`**. |
| Do not commit `.env` or `credentials.json` client secret | Already the rule; keep it that way on EC2 too. |

### Product / deploy later

| Item | Notes |
|---|---|
| **EC2 (or any host)** | Same `.env` keys. Set `PUBLIC_BASE_URL=https://your-host`. Add that origin + `/ui/google/callback` (if login is on) and `/oauth/callback` on the Google web client. |
| Wire Cosmo to this **same POST** and open the returned `url` | Contract is ready; Cosmo integration is not done here. |
| Optional: run the **engine** with a real `GAB_CONFIG` | Today reset uses the **seeder pipeline** because `GAB_CONFIG` is empty. Fine for local; decide what EC2 should run. |
| **github.com repo** (if you still want a real GitHub repo) | First upload’s GitHub.com create failed (bad/missing PAT). Environment GitHub files still go to **Drive**. Separate from reset. |
| Persona-switch **reseed** test | We have not cleanly tested “was Backend, now Student → wipe + full upload.” |
| Status page should show **failed** when the worker dies, even if Supabase insert failed | Partially fixed (local JSON update). Confirm after service_role is in. |

### Nice-to-have (not blocking a local delta test)

- Progress bar that tracks real job counts (9,702/…) instead of a coarse queued/running/done pill.
- One “active reset per email” lock that works when sessions are only local (Supabase unique index does not see local JSON).
- Clean up leftover local session files (`.reset_sessions.json`, `.reset_sessions.freelancers.json`) so they are not mistaken for source of truth.

**Minimum path to “delta works”:** service_role in `.env` → restart → `gab_accounts` row for test02 → POST reset → log shows `mode=delta`.

---

## What has to be moved to Supabase (if anything)

**No new tables.** Do not move the job queue, tokens, or manifests to Supabase. Those stay on the machine that runs the worker.

The tables you need are **already designed**. What is missing is **rows** that never got written because the key could not write.

### Already supposed to live in Supabase — just make sure the rows are there

| Data | Table | Move? |
|---|---|---|
| Which Gmails we seed/reset + last persona | `gab_accounts` | **Yes — the missing rows.** Example: add `test02gemini@gmail.com` / `Backend_software_engineer` (or re-authorize after service_role). |
| Each upload/reset job status (`url` / session id) | `reset_sessions` | **Yes — new jobs should insert here.** Old ones stuck in `api/.reset_sessions.json` can stay as leftovers; do not bulk-upload 10k job lines. |
| Who may sign in later | `freelancers` | **Already the right table.** If you only saved emails in local JSON, add those emails here (e.g. `jahnavi@deccan.ai`). |
| Last persona after a successful seed/reset | `gab_accounts.last_reset_persona` | **Yes — keep this column updated** so the next reset can be a delta. |

### Do **not** move to Supabase

| Data | Why it stays local |
|---|---|
| `provision.sqlite` / thousands of Drive/Gmail/Calendar **jobs** | Work queue. Huge, short-lived, one machine. |
| Seed **manifest** (ids + fingerprints) | Used on the worker for delta. Not a shared Cosmo table. |
| `seeder/tokens/*.json` | Google refresh tokens. Secret. Files on disk (or a secrets store later), not a public table. |
| Persona archives (`GAB_PERSONA_ROOT`) | Source files for upload. Gigabytes. |
| Drive persona cache | Speedup cache on disk. |
| OAuth `credentials.json` | Google app client. File / secret manager. |
| QC log files (`qc_logs/`) | Optional local debug. Session **status** is enough in `reset_sessions`. |

### Unused — do not migrate into this

| Thing | Action |
|---|---|
| `gab_logins` | **Do not move login users here.** Use `freelancers`. |
| Local `.reset_sessions.freelancers.json` | Copy any emails you still need into **`freelancers`**, then ignore the file. |
| Local `.reset_sessions.json` | Only a fallback when writes failed. After service_role works, **stop relying on it**. |

**One-line rule:** Supabase = “who is the account, who may log in, did this reset start/finish.” Everything that talks to Google or holds files stays on the server disk.

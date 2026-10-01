# GAB Environment Reset API

HTTP + audit layer in front of Vishal's `gab_seeder` reset engine. Covers
**steps 1–3** of the design:

1. A public API that accepts `{email, persona, password, task_allocation_id}`.
2. Triggers the existing reset flow (`gab-seed` CLI) **unchanged**.
3. Records a unique `reset_session_id`, start/end timestamps, email, and persona
   in **Supabase** (with a local-JSON fallback for dev).

Parallel resets, EC2 packaging, and the OAuth "publish to Production" work are
tracked separately and are **not** in this build.

---

## API

### `POST /api/environment/reset`
Header: `Authorization: Bearer <RESET_API_KEY>`

```json
{
  "email": "geminiapp.gab.demo.user410@gmail.com",
  "persona": "Student",
  "password": "•••••",
  "task_allocation_id": "6a84bc5ab47b41eee8a5a799"
}
```

Returns **202** immediately (a reset can take up to ~an hour, far past any HTTP
timeout — so it runs in the background and you poll):

```json
{
  "success": null,
  "reset_session_id": "590ae1b2-…",
  "status": "in_progress",
  "task_allocation_id": "6a84bc5ab47b41eee8a5a799",
  "message": "reset accepted; poll GET /api/environment/reset/{reset_session_id}"
}
```

### `GET /api/environment/reset/{reset_session_id}`
```json
{
  "success": true,
  "reset_session_id": "590ae1b2-…",
  "status": "completed",
  "started_at": "2026-09-21T14:36:35Z",
  "completed_at": "2026-09-21T14:36:36Z",
  "error": null
}
```
`status` ∈ `queued | running | completed | failed`; `success` is `null` until terminal.

### `GET /healthz`
Liveness + which store is active.

---

## Run locally (no Google, no Supabase)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
GAB_RESET_SIMULATE=1 RESET_API_KEY=demo-key \
  .venv/bin/uvicorn reset_service.app:app --host 127.0.0.1 --port 8791
```

`SIMULATE=1` skips Google entirely and fakes a successful reset, so the whole
API + store path is exercisable. With Supabase env vars unset it writes to
`.reset_sessions.json`.

Tests: `.venv/bin/python -m pytest tests/ -q` (4 tests, no network).

---

## Wire to Supabase

1. Create a Supabase project (free tier is fine).
2. Run [`schema.sql`](schema.sql) in the SQL editor.
3. Set `SUPABASE_URL` and `SUPABASE_KEY` (service-role or an insert/update key).

The store auto-switches to Supabase once both are set. The table doubles as the
audit log and, later, the parallel-worker queue (note the
`uniq_active_reset_per_email` partial index — it already enforces "one active
reset per account").

---

## Wire to Vishal's reset engine

Set:
```bash
GAB_SEED_BIN=gab-seed          # or an absolute path to the installed entrypoint
GAB_CONFIG=/secure/path/config.json
GAB_RESET_MODE=delta           # delta (sparse) | reseed (destructive) | reset
```

`engine.run_reset()` clones `GAB_CONFIG`, overrides
`accounts[<persona>].email` with the requested email, and invokes:
```
gab-seed delta --config <tmp> --persona <persona> --execute
```
This targets the requested **email** while `gab_seeder` uses `persona` to pick
the archive data — no change to Vishal's code.

### ⚠ One integration decision needed before scale
Vishal's config maps **one account per persona** (`config["accounts"][persona]`),
and `persona` also selects the archive folder. The adapter's email-override
handles the common case, but confirm the intended mapping when many accounts
share a persona (e.g. several `Applied ML` accounts). Options:
- keep the per-request email override (current approach), or
- extend `gab_seeder` to accept an explicit `--account/--email`.

## Security notes
- `password` is accepted for the platform's request contract but is **never
  stored** and is **never** used for Google (OAuth refresh tokens do that).
  Verified by `tests/test_api.py::test_password_not_persisted`.
- `RESET_API_KEY` gates every endpoint; if unset, auth is disabled and a startup
  warning is logged (dev only).

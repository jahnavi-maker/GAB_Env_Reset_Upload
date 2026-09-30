# Dead / duplicated code audit — findings & disposition

Result of the 2026-10-01 codebase audit. Records what was removed in the accompanying
cleanup and what was **deliberately kept** (with the reason), so the larger removals are a
documented team decision rather than a silent deletion. Verify a symbol still has no live
caller before acting on anything in the "kept" lists.

## Active vs legacy (the key architectural fact)

- **Active production path (in-process):** `api/reset_service/app.py` (systemd `gab-api`)
  → `engine.py` → `materialize/runner.run_populate` → `materialize/provision/*` +
  `materialize/{drive,calendar,gmail}_sync.py`, `verify.py`, `authbackend.py`.
  Runs when `GAB_CONFIG` is **unset**; `reconcile` **always** runs in-process.
- **Legacy (subprocess):** `engine/src/gab_seeder/*` — invoked only as the `gab-seed`
  CLI when `GAB_CONFIG` is **set**. Not imported in-process anywhere.

## Removed in this cleanup (safe hygiene — verified zero live refs)

| Path | Why removed |
|------|-------------|
| `api/accounts_schema.sql` | Stale duplicate of `db/schema.sql`'s `gab_accounts`, **missing the `last_reset_mode` column** the code now writes → bootstrapping from it gives a broken DB. Zero references. |
| `api/schema.sql` | Stale duplicate of `db/schema.sql`'s `reset_sessions` (missing `upload`/`reconcile` modes). Only referenced by `api/README.md` + a `db.py` docstring, both repointed to `db/schema.sql`. |
| `api/static/authorize_all.html` | Orphaned page — superseded by `onboard.html`; zero references in code/templates/config. |
| `api/.reset_sessions.accounts.json`, `api/.reset_sessions.freelancers.json` | Runtime state committed by accident (stale `.gitignore`). `git rm --cached` + ignores broadened. |
| `api/provision.sqlite`, `api/provision.sqlite-journal` | Live SQLite artifact committed by accident. `git rm --cached` + ignores broadened. |

`db/schema.sql` is the single authoritative schema (it is a superset of both removed files).

## Kept deliberately (NOT dead in the way it first looks)

- **`engine/src/gab_seeder/` (Vishal's legacy engine) + `engine/scripts/*`** — dead in
  production, but `.env` / `deploy/.env.ec2.example` still *set* `GAB_CONFIG`, so a
  diff-persona reseed run locally still shells out to `gab-seed`. Retiring it is a real
  decision (it is also the content-diff **baseline** the reconcile logic was ported from).
  Keep until the team confirms local reseed no longer uses it.
- **`seeder/app.py` (1387 lines) + `seeder/static/*`** — standalone dev onboarding UI on
  `:8765` (started by `setup.sh`), not deployed. **But `api/` imports the `materialize`
  library at runtime**, so only `seeder/app.py` + `seeder/static/` are dead, not
  `materialize/**`. Several helpers in `runstate.py`, `jobs.py`, `skip_retry.py`,
  `github_repo.py`, `auth.py`, `csv_ingest.py`, `provision/route.py` are reachable **only**
  through this dev UI ("conditionally dead") — they all die together **iff** `seeder/app.py`
  is retired. Do not delete piecemeal.

## Duplicated logic (legacy `gab_seeder` vs active `materialize`) — for later consolidation

The active path always uses the `materialize`/`provision` side; the legacy copies live on
only via the `gab-seed` subprocess above.

1. **Retry/backoff** is implemented ~4× and has diverged: `drive_sync._retry`,
   `gmail_sync._retry`, `calendar_sync._retry`, `provision/errors.backoff_seconds`.
   `gmail_sync._retry` is **missing the socket/network-stall branch** the other two have.
   → consolidate into one shared helper (single follow-up; `verify.py` already reuses the
   per-service ones rather than adding a 5th copy).
2. Drive md5 content-diff — legacy `drive.content_matches` vs active `executors._drive`
   (DELTA) + `drive_sync.list_owned_files_index`.
3. Calendar field-drift — legacy `calendar_compare.compare_calendar_events` vs active
   `calendar_sync.event_needs_update`.
4. Gmail reconcile — legacy `gmail.reconcile_gmail_delta` vs active `engine._run_reconcile`
   + `gmail_sync.list_seeded_mail` (manifest-id based).

## Superseded within the active path (safe to remove once confirmed; left in place for now)

- `api/reset_service/upload.py` `delegation_active`, `in_workspace_domain` — superseded by
  per-account `account_uses_delegation`; no live callers.
- `materialize/authbackend.py` `get_backend` — superseded by per-account `backend_for`;
  referenced only by the dead `seeder/app.py`.
- Dead monolithic populate path: `drive_sync.populate_drive`,
  `drive_sync.populate_drive_from_cache`, `gmail_sync.populate_gmail`,
  `calendar_sync.populate_calendar` — fully superseded by planner + executors.
- `api/reset_service/activity_log.py:record`, `app.py:_bearer_ok`,
  `github_sync.{upload_github_zip,upload_github_folder,wipe_my_drive_github}`,
  `calendar_sync.dedupe_primary_events` — no live callers found.

## Structure follow-ups (not done here — larger refactors)

- `api/reset_service/app.py` is ~1825 lines / ~55 routes with reset orchestration + raw
  PostgREST query-building inline in handlers. Proposed split: `routers/` by domain +
  `reset_orchestrator.py` + `deps.py` (target app.py ≈150 lines). See the structure audit.
- `api/static/*.html` duplicate palette/font/`.card`/`.pill` across 7 self-contained pages;
  extract a shared `common.css`/`common.js` and serve `api/static/` via `StaticFiles`.

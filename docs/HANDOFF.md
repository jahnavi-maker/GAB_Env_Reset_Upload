# GAB Environment Platform — Session Handoff (resume here)

Read this + `docs/PLATFORM_LOGIC.md` first, then continue. This is the state as of the last session.

## TL;DR — where we are
- We built the **merged platform** at `/Users/divya/Downloads/GAB/gab-env-platform/` for the
  **geminiapp… @gmail.com** accounts (consumer OAuth, engine-based). It's separate from the
  bulk `@deccanexperts.us` freelancer seeder.
- The reset API now has: **bounded parallel pool**, **delta-vs-reseed routing**, **compact local
  QC logs + purge**, and **Supabase inserts** — all built and verified (in simulate).
- A **bulk cleanup run is in progress on the OLD standalone seeder** (port 8765) — see "Running now".

## Scope split (important)
- **Platform (this repo, geminiapp):** Engine-based (`gab_seeder`), consumer OAuth with full
  `https://mail.google.com/` scope → `delta`/`reseed`/`delete` all work with permanent Gmail delete.
  This is **Option A** (engine for both upload and reset).
- **Bulk seeder (`GAB/GAB/gab-workspace-seed`, @deccanexperts.us):** the freelancer-assessment
  tool, DWD service account (`gmail.modify` → trash only). **Kept separate.** Not part of the platform.

## Layout / ports / config
```
gab-env-platform/
├── .env               # shared: SUPABASE_*, RESET_API_KEY, GAB_SEED_BIN, GAB_CONFIG,
│                      #         GAB_RESET_MODE/SERVICES, RESET_CONCURRENCY=10, RESET_AUTO_ROUTE=1,
│                      #         RESET_LOG_DIR, GAB_PERSONA_ROOT
├── db/schema.sql      # gab_accounts + reset_sessions (both live in Supabase)
├── engine/            # gab_seeder (reset engine)  — venv: engine/.venv
├── seeder/            # gab-workspace-seed + db_hooks.py — venv: seeder/.venv
├── api/               # reset service + dashboard   — venv: api/.venv  (port 8791)
└── docs/PLATFORM_LOGIC.md  # the definitive per-module logic + parallelization/logging/API-insert design
```
- Supabase project is live; `gab_accounts` (4 rows: user410/414/416/417) + `reset_sessions` populated from tests.
- Engine config: `~/.config/gab-seeder/config.json` (archive → `GAB/GAB/docs/_Deccan_ GAB_Ultra_Core_Environments.zip`,
  token_dir/state_dir under `~/.config/gab-seeder/`). Tokens exist for user410/414/416/417.

## Built & verified (platform api — `api/reset_service/`)
- `POST /api/environment/reset` (Bearer) → 202 + `reset_session_id`; `GET .../{id}` to poll.
- **Parallel pool:** `RESET_CONCURRENCY` (default 10) semaphore; each account = one `gab-seed`
  subprocess (serial internally). **One-active-per-email** via DB partial-unique index + `409`. ✅ tested
- **Routing (`_decide_mode`):** same persona → `delta`; new/first → `reseed` (from `gab_accounts.last_reset_persona`). ✅ tested
- **QC logs:** compact ~1.5 KB JSONL per `task_allocation_id` in `qc_logs/`; `GET /api/qc/{tid}` reads,
  `POST /api/qc/{tid}/confirm` purges. DB rows kept for audit. ✅ tested
- **Dashboard** at `/` (`/ui/accounts`, `/ui/sessions`); single-button page at `/simple`.
- **Seeder hooks** (`seeder/db_hooks.py`): `on_authorize → gab_accounts` (+ mirrors token to engine
  token_dir), `on_push_success → reset_sessions` + `last_reset_persona`.
- Tests: `api/tests/` pass (4). Run real by setting `GAB_RESET_SIMULATE=0`.

## Key code fixes made (in the standalone `GAB/GAB/gab-workspace-seed`, for the bulk run)
- `_fixed_push_body` now honors `wipe` (was hardcoded `False` — the root cause of "nothing wiped → seed skipped").
- Full wipe (all mail→Trash / all events / all owned Drive), Drive 409-tolerant.
- Full `mail.google.com` scope; `app.js` sends `wipe:true`.
- **git → Drive uploads the REAL files into one `Github` folder (per-file), NOT a zip** (the `github_zip`
  flag name is legacy; `upload_github_zip` is unused).
- ⚠️ **These fixes are on the standalone seeder, NOT yet ported into `gab-env-platform/seeder/`.**

## Bulk cleanup run — STALLED (bulk tool only; separate from platform)
- Run `d9443da99761` on the OLD seeder (port 8765) ended: **6 ok, 1 partial, 48 stuck**, no activity ~24 min.
- **Why:** the stuck 48 were frozen mid-**full-wipe**, each trashing **thousands of Drive items** — cruft
  accumulated from the earlier overlapping runs. 20 accounts wiping + git-uploading at once through **one
  service account** hit the **Drive write throughput/quota** and silently backed off → stall. No quota error
  line, just no progress. (More threads made it worse.)
- This is the **@deccanexperts.us bulk freelancer tool**, NOT the geminiapp platform — platform work unaffected.
- If resuming the bulk cleanup: **lower concurrency** (e.g. threads 3–5), and/or wait for the SA's Drive
  quota window; the real fix is starting from **clean accounts** (no cruft → wipe is trivial) and cutting the
  ~7,600-file git upload. Earlier full bulk run had ~24 fully-ok; the platform doesn't depend on any of this.
- **Never run two push-alls concurrently** (overlap = one run's wipe trashes another's seed).

## What's remaining (next steps, priority order)
1. **Port the seeder fixes** (full wipe, `_fixed_push_body` wipe-honoring, drive-wipe-all, full-Gmail
   scope, git-folder) into `gab-env-platform/seeder/`.
2. **Wire the real engine end-to-end for the platform** (geminiapp): confirm `delta`/`reseed`/`reset`
   run via `api` with `GAB_RESET_SIMULATE=0` on a real authorized account.
3. **`/upload` API endpoint** + a **"push skipped only"** endpoint/button (engine `delta` = recovery;
   seeder `only_skipped` = recovery).
4. **QC delete on confirm** already built — wire it into the dashboard.
5. **EC2 deploy** (systemd api+workers, TLS, EBS for tokens/state/archive) + **Production OAuth
   verification** (runbook: `Downloads/GAB-Move-to-Production-Runbook.md`).
6. **Security:** encrypt `password`/`refresh_token` in `gab_accounts`.

## Open confirmations
- `RESET_CONCURRENCY = 10` (tune vs Calendar daily cap).
- Permanent Gmail delete for geminiapp (full scope) — intended? (yes, per Option A).
- Rename Drive `Github` folder → `git`? (one-line change if wanted.)

## How to resume in the new chat
Open the new chat in `/Users/divya/Downloads/GAB/gab-env-platform`, then say:
> "Read docs/HANDOFF.md and docs/PLATFORM_LOGIC.md, then continue: [next task]."

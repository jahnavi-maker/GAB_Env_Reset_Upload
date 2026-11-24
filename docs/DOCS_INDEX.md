# Docs index

Start with [README](../README.md) → [ARCHITECTURE](ARCHITECTURE.md) → [RUNBOOK](RUNBOOK.md) → [STATUS](STATUS.md).
Google Docs/Sheets, Drive folders and access details are listed with links in the team's private handover sheet (tab "Handover index").

## Docs in this repo

| Doc | Status | Notes |
|---|---|---|
| `README.md`, `docs/ARCHITECTURE.md`, `docs/RUNBOOK.md`, `docs/STATUS.md` | **Current** (24 Nov 2026) | The handover set |
| `docs/WAVE2_SEEDING.md` | Current (detail) | Wave 2 design. Its deploy notes ("not yet referenced by live service", "test plan") are out of date: Wave 2 is live |
| `docs/MODULE_JOBS.md` | Current (detail) | Keep job API. Its note "does not yet enqueue from the reset flow" is out of date: tasker resets now queue Keep jobs |
| `docs/DEAD_CODE.md` | Current (code map, 1 Oct) | Which modules are live vs legacy |
| `docs/architecture/reset-reconcile-plan.md` | Reference | Reconcile design. Orphans are identified by the baseline manifest |
| `docs/client-auth-requirements.md`, `docs/oauth-user-cap.md` | Reference | Why DWD vs consumer OAuth; 100-user cap; 7-day expiry |
| `docs/deploy-security.md` | Reference | Describes intended network controls; check the private sheet for what is applied |
| `docs/PLATFORM_LOGIC_REFERENCE.md` | Superseded by ARCHITECTURE §3–4 | No reconcile/clean modes |
| `docs/upload-provisioning-changes.md` | Superseded (historical changelog) | |
| `docs/architecture/gab-architecture.html/.json` | Superseded by ARCHITECTURE §1 | Shows the legacy engine only |
| `docs/reference/README_2026-09_superseded.md` | Superseded by README | Previous README |
| `docs/reference/OPERATOR_GUIDE_2026-10_superseded.md` | Superseded by RUNBOOK | Pre-reconcile, pre-Wave 2 |
| `seeder/README.md` | Reference | The standalone `:8765` seeder tool only |
| `engine/README.md`, `engine/docs/*` | Legacy | `gab-seed` CLI; not used in production |
| `seeder/pilot10/README.md` | Current | Pilot-10 reference seeder |

## Corrections to older docs (checked against the code, 24 Nov 2026)

| Older docs say | Fact |
|---|---|
| "Two Supabase tables" | Eight tables (ARCHITECTURE §5) |
| Same persona → `delta` | Same persona → `reconcile` (delete additions + restore). `delta` = restore-only, explicit only |
| `reconcile` "just fixes what is missing, no wipe" | It also deletes items not in the baseline |
| Deployed branch is `unified` | Host runs `9ee1429` (branch `feat/wave2-keep-reset`) on a local branch named `unified` |
| Keep / Contacts / Tasks are manual or "CLI only" | Contacts and Tasks are seeded by the platform; Keep by the Clickless worker via the job queue |
| DWD scope `gmail.modify` | Must be `https://mail.google.com/` (grant is all-or-nothing) |
| Logs at one place | Both are used: per-account run logs in `GAB_LOGS_DIR` (`~/gab-state/logs`); activity logs (`events-<date>.jsonl`, `summary.log`, `errors.log`) and `purge_runs.log` in `~/gab-env-platform/logs` |
| Password sent in the reset request | Passwords stay in the operator's browser; never sent to the server |
| Reset Keep job limit "10 minutes" | `JOB_MAX_RUN_S` default 480 s (8 min); the reset link lives 600 s |

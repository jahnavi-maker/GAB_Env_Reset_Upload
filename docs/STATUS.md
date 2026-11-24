# Status — 24 Nov 2026

Point-in-time facts. Update the date when you change this file.

## Deployed
- Host runs commit `9ee1429` (branch `feat/wave2-keep-reset`; the host's local branch is named `unified`). Restarted 24 Nov.
- **This commit is not on GitHub.** GitHub's newest related branches: `feat/wave2-contacts-tasks` (`6c07f07`), `unified` (`ce747c4`).
- Host settings (non-secret):
  - `SEED_CONCURRENCY=12`, `RESET_CONCURRENCY=16`
  - `JOB_LEASE_S=180`; `JOB_MAX_RUN_S` not set (default 480 s)
  - `LOG_RETENTION_DAYS=5`, `GAB_DEPLOY_MODE=ec2`
- Wave 2 personas: 13, from "GAB v2 Wave 2 collaterals final" (uploaded 24 Nov; previous set kept as a dated backup on the host).
- Classic personas: 18 folders under `GAB_PERSONA_ROOT`.
- Keep pack in use: version `sha256:8210c93007a80f62` (built from the 24 Nov collaterals).
  - Its notes differ from the previous pack for David Chen-Ramos, Joseph Sholotan and Nadia Kapoor.

## Phase 10 accounts ("Phase 10 (persona details)" sheet, tab "Account Upload Status")
- 98 rows; **92 Pick**: seeded from the 24 Nov collaterals, all six services verified, name and birthday match.
- 894, 908, 909, 917: Don't pick (phone-verification issue). Replaced by 557, 576, 577, 579 (all Pick).
- 857, 858 (Bianca Moretti): **not done**. Reseed hit Google's per-account daily quota on calendar writes. Retry after the quota resets, or replace the accounts.

## Tested on 24 Nov (new data)

| Account | Reset | Keep answer | Result |
|---|---|---|---|
| 894 | Real clicks on `/reset` | Yes | Session ID after ~1 min 40 s; account verified back to baseline |
| 844 | Same server calls as the page | No | Session ID after 2 min 39 s |
| 845 | Same server calls as the page | Yes | Passed after the account was added to the worker's list |

## Keep workers
- 2 workers (`…-Air-2`, `…-Air-3`) on one MacBook.
- Started from a terminal session with `worker-extra.mjs`; the Clickless app's own worker is off.
- All 98 Phase 10 accounts are on the worker's account list.

## Open risks and gaps
1. **Live code not in GitHub**, and the GitHub repo (`jahnavi-maker/GAB_Env_Reset_Upload`) is **public** and under a personal account.
2. **Access control of the operator pages:** see the "Open risks" section of the private handover sheet. Not described in this public repo.
3. **Keep depends on one Mac** staying awake with the workers running.
   - If it sleeps, Keep "Yes" resets wait, and a reset's Keep job is cancelled after 8 min.
4. **Locally patched apps.** Clickless and GAB Authorizer have local patches that a reinstall wipes.
   - Clickless runtime snapshot: `keep-automation.zip`. Authorizer backups: the "GAB-consolidated" folder.
5. **OAuth limits.** Consumer OAuth projects in Testing status expire sign-ins after 7 days, and each project allows 100 test users.
6. **Host disk.** The root disk is 28 GB (55% used on 24 Nov).
   - The `seeder/runs` purge cron (`scripts/purge_runs.sh`, daily 02:00 UTC) exists only on the host, not in git.
   - Other untracked files on the host: `api/*schema.sql`, `api/static/authorize_all.html`, `seeder/tests/test_drive_wipe.py`.
7. **No backup or restore procedure** is documented for Supabase, host tokens or persona data.
8. **Credential files and sheet password columns are spread across** personal folders and several sheets (see DOCS_INDEX "credentials").
9. **One handoff doc not reviewed.** The Google Doc "Daily Notes / Handoff Doc" was not readable by the documentation tooling; its contents are not reflected here.
10. **Minor code issue.** `/home` and `/privacy` routes are defined twice in `api/reset_service/app.py`.

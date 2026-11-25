# Runbook

Step-by-step operations. How the pieces fit: [ARCHITECTURE.md](ARCHITECTURE.md). Current state and risks: [STATUS.md](STATUS.md).

**Golden rule:** never seed, reseed or reset an account a tasker is using. It wipes their work.

## 0. Access you need first
Details (hosts, where keys are kept, who grants each) are in the team's private handover sheet.
- VPN (the EC2 host is private; SSH only over VPN) + the host's SSH key
- Supabase project (service-role key is in the host's `.env`)
- Google Cloud Console: the OAuth client projects
- Google Admin console: for Workspace domains using domain-wide delegation (DWD)
- Cosmo admin
- The Mac that runs the Keep worker (Clickless)

## 1. Google setup

### 1a. Consumer `@gmail.com` accounts: one OAuth client project per ≤100 accounts
1. console.cloud.google.com → new project (e.g. `gab-wave2-1`), Organization "No organization".
2. Enable APIs: Gmail, Google Calendar, Google Drive, Google Sheets, People, Google Tasks.
3. Google Auth Platform → Get started. App name, Audience **External**. Branding: home page `https://<your-domain>`.
4. Data Access → add scopes: openid, userinfo.email, `https://mail.google.com/`, calendar, drive, contacts, tasks.
5. Audience → Test users → add the account emails. **Max 100 per project**, so ~100 accounts per project.
   - In **Testing** status, sign-ins expire after **7 days** (accounts must be re-authorized).
   - Publishing status is per project; check each project's Audience page.
6. Credentials → OAuth client ID → **Web application**.
   - JavaScript origin `https://<your-domain>`; redirect URI `https://<your-domain>/oauth/callback`.
   - Download the JSON. This is the **client.json**.
7. Record which accounts belong to which client.json in the **"GAB | Client JSON Info"** sheet. A token only refreshes under the client it was issued by.

### 1b. Workspace domains: domain-wide delegation (no per-account sign-in)
1. Cloud Console (as domain admin): project → enable Gmail, Calendar, Drive (+ People, Tasks for Wave 2) → service account → JSON key → note its numeric **Client ID**.
2. admin.google.com → Security → Access and data control → API controls → Manage Domain-Wide Delegation → Add:
   - Client ID + scopes `https://mail.google.com/`, `https://www.googleapis.com/auth/calendar`, `https://www.googleapis.com/auth/drive`.
   - For Wave 2 also contacts and tasks.
   - **Use `mail.google.com`, not `gmail.modify`.** The grant is all-or-nothing: one missing scope = every token fails (`unauthorized_client`).
3. The key goes on the host (`seeder/gab-sa.json` or `ENV_LOADER_SA_KEY`). Users must exist in Admin → Directory first (otherwise `invalid_grant: Invalid email or User ID`).

## 2. Accounts sheet and CSV
- Classic CSV (`/onboard`): `email, persona, password, mode`. Headers any order. Persona = folder name under `GAB_PERSONA_ROOT`; blank persona = `clean`.
- Wave 2 / Authorizer CSV: `email,persona,password,mode,name,home_address,birthday`.
  - Address in double quotes, birthday `YYYY-MM-DD`, one row per account.
  - Persona = id (`marcus_vance`) or the sheet name ("Dr. Marcus Vance : The …").
- `mode` blank = automatic (see ARCHITECTURE §4).
- The password stays in the browser; it is never sent to the server. Never put passwords in docs.

## 3. Authorize
1. Open `/onboard` (classic) or `/wave2` (Wave 2). Drop the account's client.json; it applies to this browser only.
2. Load the CSV → **Authorize** each row (or **Authorize all**). In Google's consent screen tick **Select all**.
   - "Google hasn't verified this app": Advanced → Go to … → Continue.
   - "Access blocked" with no Advanced link: the account is not a test user of that project, or the project is at 100.
3. Re-authorize accounts on Testing projects before 7 days pass.
4. **GAB Authorizer app** (Mac) automates step 2 for a CSV. It also sets the Google profile name, home address and birthday (Wave 2 CSV columns), and shows ✓/✗ for each against the CSV.

## 4. Seed

### Classic (`/onboard`)
- Load CSV → Upload per row, or **Bulk upload**.
- Status column + **Explain** on failed rows.
- Hard-refresh the page after a deploy (the page is browser-cached).

### Wave 2 (`/wave2`), order per account
1. **Authorize** (Contacts + Tasks permissions shown as ✓).
2. **Keep** (§5).
3. **Run** (`reseed` for a full wipe + seed).
- Live counts per service, then "seeded" with ✓ per service when verify passes.
- Use **one tab**. The server runs `SEED_CONCURRENCY` seeds at a time (12 now) and queues the rest; more tabs do not go faster.

### Timing seen on 24 Nov (12 at a time)
- Light personas take ~8 min per account. Bianca Moretti (477 events) takes ~20 min.

### Before reseeding many accounts
- Tasking on them must be paused.
- Each full seed uses Google's per-account daily calendar-write allowance. Repeated reseeds of heavy personas on the same day can hit "Google API quota reached" (§10).

## 5. Keep (Clickless on a Mac)
1. Install Clickless (DMG). Sign each account in once inside Clickless; this creates its browser profile.
2. Keep pack = the persona notes (`keep-pack.json`).
   - Build it from the persona folders with `tools/build_keep_pack.py`.
   - Install it with `tools/install_pack.mjs` (validates first).
   - Both tools are in `keep-automation.zip`.
   - The Wave 2 page and the reset check compare against the persona files on the server, so rebuild the pack whenever the collaterals change.
3. Worker settings: worker key (`WORKER_API_KEY`), server URL, accounts list.
   - **Every account a tasker can reset must be on the worker's account list.** Otherwise a Keep "Yes" reset never finishes (NO_WORKER after 10 min).
   - Add with `tools/add_accounts.mjs` or by signing the account in through the app.
4. Start workers: Start in the app, or `CLICKLESS_WORKER_ID=<name> node worker-extra.mjs` (run from the app's `app-runtime` folder).
   - **2 workers per Mac worked reliably. 3 workers plus the app's own worker on the same profiles caused "Keep did not open" failures.**
5. **The Mac must stay awake** during tasking: plugged in, lid open, `caffeinate -ims`.
   - When it sleeps, Keep jobs wait. A running job's lease expires and the job is retried.
6. Check Keep on accounts read-only with `tools/keep_count.mjs` (found / missing / extra / Archive / Trash).

## 6. Verify and mark the sheet
0. Coverage check:
   - Count tasks per persona in the Wave 2 task sheet (`[Internal][Wave 2] GAB v2`, column "Synthetic person").
   - Count accounts per persona in the accounts sheet. They must match.
   - On 24 Nov this found Samuel Okafor (3 tasks) with no accounts and Nadia Kapoor with 3 too many.
1. Services check on the host: `scripts/verify_wave2_accounts.py <email>:<persona> …` (read-only). Expect `ok: true`: nothing missing or extra, Trash/Spam empty.
2. Keep check: `tools/keep_count.mjs`. Expect found = want, extra 0, Archive 0, Trash empty.
3. Profile check: `tools/profile_read.mjs` (name, birthday, home address from myaccount.google.com).
4. Phase 10 sheet:
   - **"secondary check"**: write the per-service counts, then Done / Not done.
   - **"Account Upload Status"**: Upload Status = Done, and **Pick** only when all of these hold:
     - column J is "Added - All" (or the team accepts the listed issue);
     - Authorization Status is "Completed";
     - all six services are verified;
     - name and birthday match.

## 7. Tasker reset (what happens, what to tell taskers)
1. In Cosmo the tasker clicks **Reset Environment** → `/reset` link (valid 10 min, single use).
2. Signs in with their allowed email. If they're not allowed, a QM adds them on `/managers`, or an operator adds them on `/logins`.
3. Answers **"Did this task use Google Keep?"**. Not sure → Yes.
4. Clicks **Reset environment** and waits for the green steps and the **Session ID**, then pastes it into Cosmo.
   - Seen times: Keep "No" 2–3 min; Keep "Yes" ~2–6 min.
   - If a step fails, start a **new reset from Cosmo**. A link works once; "Reset again" only reloads the page.
5. With 2 Keep workers, 2 Keep "Yes" resets run at once; more queue. A reset's Keep job is cancelled after 8 min (`JOB_MAX_RUN_S`).

## 8. Deploy a change
1. Commit on a branch → push to the host's git remote → on the host fast-forward the deployed branch.
   Never edit files directly on the host (it has drifted before).
2. Check it compiles: `python -m py_compile` on changed files; run `api/tests`.
3. **Restart only when no seed or reset is running.** Check `reset_sessions` for `queued` / `running` / `in_progress`.
   - Restart with `sudo systemctl restart gab-api`, then check `curl 127.0.0.1:8791/healthz`.
   - A restart interrupts running seeds; they are re-driven up to 3 times, then marked failed.
4. Static pages (`api/static/*.html`) are read on each request; no restart needed. Operators must hard-refresh.

## 9. Daily checks
- `systemctl is-active gab-api`; `journalctl -u gab-api --since '1 hour ago' | grep -E 'ERROR|FAILED'`.
- Failed `reset_sessions` today; `module_jobs` in `needs_manual`.
- Keep workers' `last_seen` in `module_workers` (should be seconds old).
- Disk on the host (`df -h /`; 28 GB root disk).

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| 409 "an operation is already running" | One active operation per account | Wait for it, or check `reset_sessions` for a stuck row |
| "Google API quota reached for this account" | Google's per-account daily limit (seen on calendar inserts after many reseeds of a 477-event persona in one day) | Stop retrying (each retry uses more). Retry the next day, or use a fresh account |
| `BrokenPipeError` on a seed | Dropped connection to Google mid-write (writes are not auto-retried, to avoid duplicates) | Re-run the seed |
| Drive trash `409 Conflict` | Two empty-trash calls overlapping | Retried automatically since 24 Nov |
| `unauthorized_client` | DWD grant missing a scope | Add all scopes for that client ID (§1b) |
| `invalid_grant: Invalid email or User ID` | Workspace user does not exist | Create the user in Admin |
| "Access blocked" / 403 on consent | Not a test user, project at 100, or Testing 7-day expiry | Add test user / use another project / re-authorize |
| Wave 2 "needs Wave 2 sign-in" | Token lacks Contacts/Tasks | Re-authorize on `/wave2` |
| Keep job `needs_manual` NO_WORKER | Account not on any worker's list, or no worker running | Add the account to the worker; start a worker |
| Keep "Keep did not open" | Too many browsers on the same profiles, or the account signed out in Clickless | Run ≤2 workers; sign the account in again in Clickless |
| Keep job `needs_manual` LEASE_EXPIRED | Worker stopped mid-job (Mac slept / restarted) | Keep the Mac awake; retry the job |
| One extra calendar event "<Name> (me)'s birthday" | Google creates it from the profile birthday | Not seed data. The verify script ignores it |
| Classic: "N baseline items failed permanently" + Drive 404 | Drive 404 cascade under load | Run `reconcile` 2–3 accounts at a time |
| A page shows old behaviour after a deploy | Browser cache | Hard-refresh (Cmd+Shift+R) |

## 11. Do not
- Reset, reseed or write to an account a tasker is using.
- Put keys, tokens or passwords in docs, sheets or commits. The GitHub repo is public.
- Delete Gemini chat history on demo accounts (the tasking FAQ says never delete conversations).
- Edit code directly on the host, or restart during active seeds or resets.
- Run more than 2 Keep workers on one Mac, or the app's worker together with the extra workers.

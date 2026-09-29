# Baseline reconcile — diff-based reset (label-independent)

## Problem
Raters run a task on Model A (Gemini), reset, then Model B (GPT). During a task the
models create real content — emails sent, calendar events, Drive files ("The required
state change occurred in the environment", L2 1.2). That content is created as a normal
user, so it carries **no `GAB-SEED` marker**. Today's reset only removes marker-tagged
items, so **the models' content survives** → Model B starts contaminated → benchmark
invalid.

## Goal
A reset returns the account to the **pristine baseline** by computing the **difference**
against a manifest — delete what isn't baseline (the agent's additions), restore what's
missing — rather than a full wipe. Surfaces: Gmail, Calendar, Drive, GitHub(-in-Drive).
No labels required.

## Source of truth: the manifest
Persistent per-account job store `RUNS/acct-<email>__<persona>/provision.sqlite`, table
`jobs`. Each seeded item has `(service, action, synthetic_id, google_object_id, status)`;
SUCCESS rows carry the **live Google ID** (the worker calls
`store.persist_success(job_id, result["id"])`). Baseline = the set of these IDs. Manifest
membership answers "baseline or agent-created?", so the `GAB-SEED` label is unnecessary.

Baseline ID sets (status=SUCCESS, google_object_id not in {'wiped',''}):
- gmail:    action = `insert_message`  → message ids
- calendar: action = `insert_event`    → event ids
- drive:    action in {`create_folder`,`upload`} → folder + file ids (GitHub zip included)

## Core operation: reconcile(account)
Per surface, run **sweep orphans → then restore missing**:
1. B = baseline IDs (from manifest).
2. L = live IDs (list the account).
3. Orphans = L − B → **delete** (agent content).  ← the new piece
4. Missing = manifest items not present in L → re-seed (existing delta).
Result: account == baseline exactly.

## Per-surface listing
- **Gmail:** `users().messages().list(userId=me, includeSpamTrash=false)` paginated → ids.
- **Calendar:** `events().list(calendarId=primary, singleEvents=false, showDeleted=false)`
  paginated → event ids (recurring masters). Holidays/birthdays are separate calendars →
  out of scope.
- **Drive:** `files().list(q="'me' in owners and trashed=false")` paginated → ids
  (files + folders). Only owned items; skips "shared with me". GitHub zip is a Drive file.
- **GitHub:** covered by Drive (zip). Real-GitHub-connector writes are a flagged follow-up.

## Deletes (phase 2, after dry-run is validated)
- Gmail: trash (or batchDelete 1000/chunk) the orphan message ids.
- Calendar: delete each orphan event id.
- Drive: trash orphan file/folder ids (top-down).

## First-run / empty-manifest rule
Accounts seeded by old code have no stored google_object_ids. Reconciling with an empty
manifest would treat everything as an orphan. Rule: **if the manifest is empty/absent →
run a reseed (wipe+seed)**, which populates the manifest with live IDs. Every reconcile
after that is a true, cheap diff.

## Wiring
- New `sweep_orphans(creds, baseline_ids)` per surface (alongside `wipe_seeded_*`).
- Engine mode `reconcile` = sweep orphans → restore missing, reading the persistent store.
- Reset path (`POST /api/environment/reset`, rater reset link) → `reconcile` (not plain
  delta). Onboard Upload "same persona" → `reconcile` too (TBD; see decision).
- Labels no longer used for wipe; Gmail/Calendar re-seed dedup switches to the manifest.
- **API contract unchanged** (same endpoint/request/response); only reset *behavior* changes.

## Safety
- Destructive sweep runs only for emails registered in `gab_accounts`, only these
  surfaces, only owned items.
- **Dry-run first**: `reconcile_preview` lists exactly what would be deleted, deletes
  nothing. Validate on one account before enabling deletes.

## Validation (user410)
1. Seed baseline (populates manifest with IDs).
2. Add agent content: an unlabeled Sent email, a calendar event, a Drive file.
3. Dry-run → orphan list should be exactly those 3.
4. Enable deletes → run → the 3 are gone, baseline intact.
5. Run again → no-op.

## Confidence
Deterministic given the manifest holds live IDs (confirmed). ~90%; remaining risk =
API scopes (trash/delete) and Drive ownership edges — both de-risked by the dry-run.

## Gmail identity note (validated)
Gmail message ids proved unstable (threading / re-insert) → the manifest-id diff left a
residual ~46 orphans on user410. Fixed by identifying Gmail baseline via the **GAB-SEED
label** (`label:GAB-SEED` = keep, `-label:GAB-SEED` = orphan → delete). Calendar/Drive
keep the manifest-id diff (validated clean). After the fix, user410 converged to
orphans=0 on all three surfaces (gmail 170/170, calendar 170/170, drive 231/231).

## Status
- [x] Persistent per-account manifest store (shipped)
- [x] Dry-run `reconcile_preview` + endpoint (shipped)
- [x] Real sweep + `reconcile` mode + `POST /ui/reconcile` (shipped)
- [x] First-run empty-manifest → reseed rule
- [x] Gmail baseline by GAB-SEED label (message ids unstable)
- [x] Live validation on user410 — orphans=0 on gmail/calendar/drive
- [x] Wire `reconcile` into the reset path — `_decide_mode` same-persona → reconcile, so
      POST /api/environment/reset, the rater reset link, and account-reset all reconcile.
      Validated: an API reset ran `mode=reconcile by=cosmo`. "Retry skipped" (explicit
      delta) stays restore-only.
- [x] Upload "same persona" → reconcile (onboard Upload / Bulk upload)

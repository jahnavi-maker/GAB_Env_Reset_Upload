> **Note (24 Nov 2026):** tasker resets now queue Keep jobs too (see `docs/ARCHITECTURE.md` §3.3–3.4).

# Module jobs: browser-driven workflows through the API

Some parts of an environment have no API for consumer Google accounts (Google Keep today, Chat next). Clickless drives them through a
real browser on a **worker machine** that holds the signed-in browser profile of each account. This feature lets this platform hand
such work to a worker through a queue, and lets people see and finish whatever could not be done automatically.

```
 caller (reset flow, QC tools)           this platform (API + Supabase)              worker machine (Clickless)
 ─────────────────────────────           ───────────────────────────────             ──────────────────────────
 POST /api/jobs  ───────────────────────▶ module_jobs row: queued
                                                                  ◀── POST /api/worker/claim  (every 5-10 s, when idle)
                                          atomic claim + lease  ────▶ job { workflow, account, persona, mode }
                                                                      opens the account's own browser profile, does the work
                                                                  ◀── POST …/heartbeat  (extends the lease)
                                                                  ◀── POST …/result  done | needs_manual | retry | failed
 GET /api/jobs/{id}, GET /api/followups ◀ status, summary, follow-up for a person
```

The worker only makes outbound calls, so it can sit anywhere. It holds **no passwords**: each account is signed in once on that
machine, and a job only names the account. If a session has expired, the job becomes a follow-up ("needs sign-in").

## What a job carries

```json
POST /api/jobs      (Authorization: Bearer <RESET_API_KEY>)
{
  "request_id": "req-2026-11-23-0001",      // idempotency key: the same id with the same body is the same job
  "workflow": "keep-seed",                  // keep-seed | keep-wipe
  "account": "worker.account.two@example.com",
  "persona": "arthur_pendelton",            // keep-seed only; must exist in the worker's own data
  "mode": "reseed",                         // keep-seed: clean (add what is missing, default) | reseed (wipe first)
  "correlation": { "reset_session_id": "…", "task_allocation_id": "…" },
  "max_attempts": 3,                        // 1-5, default 3
  "pack_version": "sha256:…"                // optional: only a worker holding this data version may take it
}
→ 201 { "job": {…}, "created": true, "routable": false, "routing_note": "no worker has declared this account" }
```

Unknown fields are refused (422, naming the field and never echoing the value), so a password or a URL can not be sent by mistake.
`routable` says straight away whether a worker that is alive right now could take the job.

## States

`queued → running → done | needs_manual | failed | cancelled`, and `needs_manual → resolved` (a person did it) or back to `queued` (retry).

* **Claim** (`POST /api/worker/claim`): the worker sends `{worker_id, accounts, workflows, personas, pack_version}`. It gets the oldest
  queued job whose account, workflow, persona (and pinned data version) it can handle and whose account has no job running, with a lease
  (`JOB_LEASE_S`, 600 s). The claim is a compare-and-set, and a partial unique index allows one running job per account, so two workers can
  never both win. `204` means nothing for you.
* **Lease**: a heartbeat extends it. A worker that crashes or sleeps stops heartbeating; the next claim finds the expired lease and puts the
  job back in the queue (or to `needs_manual`, `LEASE_EXPIRED`, once `max_attempts` is used up). **No background job is needed**: expiry is
  checked when a worker asks for work.
* **Result**: `done` (with a summary), `retry` (back to the queue while attempts remain), `needs_manual`, or `failed`. All but `done` carry a
  `follow_up`: `{code, reason, needs: sign-in | verification | check | none, step, screenshot}`.
* **Nobody can take it**: a job queued for `JOB_UNROUTABLE_AFTER_S` (600 s) with no live worker able to take it is *shown* as `needs_manual`
  (`NO_WORKER`, with the reason: "no worker has declared this account" or "the worker for this account (mac-1) is offline: last seen …").
  This is worked out when the job is read; nothing is written, so the job is still claimed the moment a capable worker appears.

## Endpoints

| Who | Method and path | Purpose |
|---|---|---|
| caller | `POST /api/jobs` | enqueue (201 new, 200 same `request_id`, 409 a different body under the same id, 422 invalid) |
| caller | `GET /api/jobs`, `GET /api/jobs/{id}` | read (`?status=`, `?reset_session_id=`) |
| caller | `GET /api/followups` | everything a person has to look at |
| caller | `POST /api/jobs/{id}/resolve` `{note}` · `/retry` · `/cancel` | a person's actions |
| worker | `POST /api/worker/claim` | get a job (200) or nothing (204) |
| worker | `POST /api/worker/jobs/{id}/heartbeat` `{worker_id}` | keep the lease; the reply says if a cancel was requested |
| worker | `POST /api/worker/jobs/{id}/result` `{worker_id, attempt, outcome, summary, follow_up, pack_version, inventory}` | report |
| worker | `POST /api/worker/jobs/{id}/screenshot` (raw PNG, header `X-Worker-Id`) | upload what the window showed; returns `{url}` |
| caller | `GET /api/jobs/{id}/screenshot` | the image (PNG, `no-store`); 404 if none |

### Attempt number and Keep inventory (worker → platform)

* **`attempt`** (heartbeat and result): send back the `attempt` the claim returned. A report for an earlier attempt is refused (409),
  so a late or retried report can never close a newer attempt, even from the same worker. Optional: a worker that leaves it out
  still works, without that protection.
* **`inventory`** (result of `keep-seed` / `keep-wipe`): what Keep showed after the run, one title per note or list (the title, or its
  first line / first item when it has none), per view: `{"home": [...], "archive": [...], "trash": [...]}` (at most 500 titles per view,
  300 characters each). The `/wave2` page compares it with the persona's notes: every note there **once**, nothing extra, Archive and
  Trash empty → "Keep verified". Without it the page can only check the note count in `summary` and shows extras as not checked.

## Wave 2 page: Keep is step 2

`/wave2` runs, per account: **1 Authorize → 2 Keep → 3 Run** (Gmail, Calendar, Drive, Contacts, Tasks). "Keep all" queues a `keep-seed`
job in mode `reseed` for every authorized account (an account with a Keep job already queued or running gets that job back, not a second
one); "Run all ready" only runs accounts whose Keep is ✓ unless that box is unticked. Same-origin routes: `GET /ui/wave2/keep`,
`POST /ui/wave2/keep` (queue), `POST /ui/wave2/keep/status` (many rows), `POST /ui/wave2/keep/retry`. The check is in
`api/reset_service/wave2_keep.py`; the expected notes come from the persona world with the same rules as Clickless's `pack.js`.

## Keys and security

* Callers use the existing `RESET_API_KEY`. Workers use their **own** `WORKER_API_KEY`; neither opens the other's routes. With
  `WORKER_API_KEY` unset the worker routes answer 503, never open.
* The worker routes are not IP-allow-listed (a worker machine's address is not fixed). `/api/jobs` is not in `access.PROTECTED_PREFIXES`
  either, like `/api/accounts`; add `"/api/jobs"` there if callers should be limited to the whitelisted servers.
* A job holds no secret. The worker reads persona data from its own machine and reports which version it used (`pack_version_used`).
* Only the worker holding the lease can heartbeat or report; a worker that lost its lease cannot overwrite a newer attempt.

## Configuration and schema

`.env.example` lists `WORKER_API_KEY`, `JOBS_TABLE`, `WORKERS_TABLE`, `JOB_LEASE_S`, `JOB_WORKER_LIVE_S`, `JOB_UNROUTABLE_AFTER_S`.
Screenshots are kept in `JOB_FILES_DIR` (default `api/.job-files`, git-ignored; **set it to a persistent folder on EC2**, or they are lost when the instance
is replaced), at most `JOB_SCREENSHOT_MAX_BYTES` (2 MB) each, PNG only, only from the worker that holds the job. They show whatever the browser showed, including
the account's name and email. `jobs_files.py` is a two-method interface (`put`/`get`), so S3 or Supabase Storage can replace the folder later.
Run the new part of `db/schema.sql` once (`module_jobs`, `module_workers`). Without Supabase configured the queue is kept in memory (dev only).

## Smoke test

`api/scripts/jobs_smoke.py` plays both roles against a running platform with a made-up account and worker (a real worker never takes its
jobs) and leaves nothing open:

```
BASE_URL=https://<platform> CALLER_KEY=<RESET_API_KEY> WORKER_KEY=<WORKER_API_KEY> python api/scripts/jobs_smoke.py
```

`--fast` also checks lease expiry and the unroutable view; it needs `JOB_LEASE_S=3 JOB_UNROUTABLE_AFTER_S=3 JOB_WORKER_LIVE_S=2`, so use it
locally or on staging only. It has been run against the app with the in-memory store and with a real Postgres + PostgREST
(this schema applied unchanged), including 8 workers racing for one account: exactly one wins and the database never holds two
running jobs for the same account.

## Code

`api/reset_service/jobs.py` (the rules, no database and no timers) · `jobs_store.py` (memory and Supabase/PostgREST) · `jobs_api.py` (routes).
Tests: `tests/test_jobs.py`, `test_jobs_api.py`, `test_jobs_supabase_store.py`.

## Not in this change yet

* Enqueuing from the reset flow (after the engine finishes, one job per workflow and account, with the `reset_session_id`).
* A page for people to work through follow-ups (today: `GET /api/followups` and the resolve/retry calls).
* The worker itself (a mode of the Clickless app) and a one-off timer for proactive alerts, if wanted.

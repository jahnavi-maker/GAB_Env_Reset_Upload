# What changed in this branch — upload & provisioning rework

_A plain-English summary of the work on `feature/upload-provisioning-pipeline`._

## The short version

The old setup ran two things: a separate seeder app for the first upload, and the
reset API. They didn't always share the same Google tokens or persona files, and a
big upload could stall or half-fail with little to show for it. This branch pulls
everything into **one server** and rebuilds the upload path into a proper, resilient
job pipeline. It also adds simple ways to manage which accounts get seeded and who's
allowed to sign in, and makes the whole thing more forgiving about messy input.

## The big pieces

### 1. Upload and reset now live in one app
You no longer need a second app on `:8765` just to seed an account. The seeder code
is imported straight into the main API, so upload, authorize, and reset all run in
the same process, off the same tokens and the same persona folders. If no engine
config is set, reset simply reuses the seeder's upload pipeline instead of dying.

### 2. A real upload engine (the `provision/` package)
This is the heart of the branch. Instead of pushing everything for one account in a
single long loop, the upload is now broken into thousands of small **jobs** (create a
folder, upload a file, insert an email, add a calendar event…) that are:

- **Saved to a local SQLite queue**, so an upload can be **resumed** if it's
  interrupted — finished work is never redone, and a crash mid-run picks up where it
  left off.
- **Run by separate worker pools** for Drive, Gmail, and Calendar at the same time,
  with **fair scheduling** so one giant mailbox can't starve the other accounts.
- **Rate-limited with an adaptive throttle** — it speeds up when Google is happy and
  automatically backs off when it starts hitting quota, instead of blasting through
  the daily limit.
- **Retried with backoff** on transient errors (429s, 5xx, quota blips), while
  genuinely bad records (duplicate IDs, malformed events, oversized files) fail on
  their own **without taking down the whole account**.

The net effect: uploads are faster, survive interruptions, and don't fall over
because of one bad row.

### 3. Smart delta vs. reseed
When you reset an account, it checks the last persona on record. Same persona → a
**delta** that only fixes what drifted. Different persona (or a brand-new account) →
a full **reseed**. During a delta it actually skips files/events/emails that are
already correct, so repeat resets are quick.

### 4. A working Drive cache
The persona's Drive files are now decoded to disk **once** and reused across every
account, keyed by a fingerprint so it rebuilds only when the source changes. That
avoids re-reading the big environment archive on every single upload (which was slow
and burned extra API calls).

### 5. Easier account & login management
- **Accounts** (the Gmail inboxes we seed/reset) can be added one at a time or via
  **CSV**, through the API or the onboarding page. The CSV parser is forgiving about
  headers and encodings, and it maps persona names loosely (see below).
- **Logins** (the people allowed to open the reset page) are managed the same way,
  and are kept clearly separate from the accounts being reset — no more mixing the
  two up.

### 6. Google sign-in for the reset page
There's a proper server-side Google login flow so a person signs in with their real
Google account and we verify their email against the allow-list. (It's wired up but
switched off for now so reset can be tested without the login step.)

### 7. Friendlier input handling
Persona names no longer have to match exactly — `"Startup Founder"`,
`"startup founder"`, and `"Startup_founder"` all resolve to the same environment
instead of failing. Bad rows are flagged clearly rather than crashing the run.

### 8. A nicer onboarding UI
The onboarding page got a manual "add account" form (email + persona, no CSV needed),
a clearer CSV option, and a **live progress bar** that shows real per-service counts
(done / left / retrying / failed) while an upload runs.

## What's still in progress

A few things are set up but not fully proven yet, and are worth finishing before
calling this production-ready:

- The Google sign-in gate on the reset page is currently **off** (for testing).
- Delta needs the Supabase **service_role** key in place and the account rows present
  to route correctly — otherwise it falls back to a full reseed.
- A clean persona-switch reseed (e.g. Backend → Student) hasn't been fully tested.

## Where things live (unchanged)

Google tokens, persona files, the job queue, and the Drive cache all stay on the
machine that does the work. Supabase only holds the shared record: which accounts
exist, who may sign in, and whether a reset started/finished. Nothing heavy or
secret was moved into the cloud database.

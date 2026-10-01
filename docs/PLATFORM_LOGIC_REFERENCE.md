# GAB Platform — Logic Reference

One page. Tables only. What each operation does, how the manifest/cache work, per-service behavior, and every fallback.

---

## Core concepts (one line each)

| Term | What it is |
|---|---|
| **Manifest** | Per-`(account, persona)` JSON at `state_dir/<email>/<persona>.manifest.json`. Records every seeded object's **ID + fingerprint** for Gmail/Drive/Calendar/GitHub, plus the source **archive catalog fingerprint**. `completed_at` is set only after a full successful seed. |
| **Persona (Drive) cache** | Decodes a persona's Drive files from the 1.6 GB zip **once** to disk, then reuses them for every account of that persona. Byte-identical fingerprints. Opt-in `GAB_DRIVE_CACHE=1` (on by default in the platform). Keyed on archive `(path,size,mtime)` → auto-rebuilds if the archive changes. |
| **Retry skipped** | Re-push **only** the items that were missed/failed last time (not a full run, no wipe). Runs as a **delta/recover** that checks each missing item individually. |
| **Seed tag** | Unique marker stamped on every object the platform creates, so a wipe/delta only ever touches platform-owned objects. |

---

## The 5 operations

| Op | What it does | When | Manifest interaction |
|---|---|---|---|
| **Upload** (first upload) | OAuth authorize → **wipe + fresh seed** → write a complete manifest. | Onboarding a brand-new account. | **Writes** a new complete manifest (unlocks future delta). |
| **Bulk upload** | Authorize accounts, then seed all authorized ones in **bounded parallel** (one engine subprocess per account). | Onboarding many accounts. | Each account writes its own manifest. Persona cache makes repeats fast. |
| **Reset** | Restore one account to baseline (removes model/user changes). **Auto-routes**: delta if a complete manifest exists, else reseed. | Between benchmark runs. | **Reads** the manifest to decide route; delta uses it, reseed rewrites it. |
| **Reseed** | **Full wipe → fresh seed.** Guaranteed-correct baseline. | No/incomplete/mismatched manifest, changed archive, or as a fallback. | **Rewrites** a complete manifest. |
| **Delta** | Sparse reconcile vs the manifest: delete extras, restore deleted, revert modified — only the diff. Fast. **Fail-closed** (refuses an incomplete/mismatched manifest). | Reset when a complete, matching manifest exists. | **Reads** the manifest as the source of truth. |

**Route decision (reset):** complete + matching manifest → **delta**; otherwise → **reseed**. Any delta that can't proceed safely **auto-falls back to reseed** (see Fallbacks).

---

## How each service is handled (wipe / seed / delta)

| Service | Wipe | Seed | Delta (reconcile) |
|---|---|---|---|
| **Gmail** | **Permanent** `batchDelete` (full `mail.google.com` scope), loop-until-empty. | Import messages + attachments. | Compare messages vs manifest; re-add missing, remove extras. Fail-closed on safety issues. |
| **Drive** | Delete manifest-tracked file/folder IDs (`reset_drive_all`). | Create files/folders with **client-supplied IDs** (idempotent create-retries). | Match by `path_sha`/`content_sha`; create missing, delete extras, replace changed. |
| **GitHub** | Same as Drive — git files live inside Drive as a **`Github` folder**. | The model edits git files as ordinary Drive files. | Handled **identically to Drive** (manifest-tracked). No live github.com repo. |
| **Calendar** | `Calendars.clear()` (whole primary calendar) or delete tracked events. | Insert events, **paced ~1/s** for quota. | Compare events vs manifest; re-add/remove. **Quota-sensitive** (daily write limit). |

**Note:** the platform always operates on **all four** (Drive+GitHub+Gmail+Calendar) for every op. Calendar is never silently skipped.

---

## Manifest lifecycle

| Stage | State |
|---|---|
| Seed starts | Manifest written incrementally (`checkpoint`), `completed_at = null`. |
| Seed finishes fully | `completed_at` set → manifest is **complete** → delta allowed. |
| Seed interrupted (timeout/crash) | `completed_at` stays null → **incomplete** → delta refuses → reseed. |
| Archive changed | Catalog fingerprint mismatch → delta refuses → reseed (rebuilds from new archive). |
| Wrong account/persona | Manifest mismatch → delta refuses → reseed. |

---

## Fallbacks — what happens when something fails

| Failure | Detection | Fallback |
|---|---|---|
| **Delta can't proceed** (incomplete/missing/mismatched manifest, **changed archive**, drive/gmail/calendar delta-safety stop) | Engine emits `"explicit full reset required"` | **Auto full reseed** (wipe + seed, rewrites a clean manifest). |
| **Delta post-apply verify fails** (often eventual-consistency) | `"verification failed"` | **Auto reseed** (baseline is the goal anyway). |
| **Transient** network/socket/5xx (delta, wipe, or seed) | timeout/socket/5xx text | **Retry once** at the platform; engine also retries HTTP with backoff+jitter. |
| **Google API quota** (403 `quotaExceeded`, daily) | quota text | **Not retried** — clear message: *"daily limit, retry after ~24h."* Calendar paces writes to avoid it. |
| **Auth expired/revoked** (`invalid_grant` / `RefreshError`) | auth text | Clear message: *"re-authorize this account, then reset again."* |
| **Reseed wipe fails** | wipe non-zero | Return failure with classified detail (no half-seed reported as success). |
| **Subprocess timeout** | `TimeoutExpired` | Fail cleanly with timeout message. |
| **Engine binary / `GAB_CONFIG` missing** | pre-checks | Fail cleanly with a specific message. |
| **Drive/Calendar batch 409** (concurrent nested delete) | 409 | Serialized per-item re-check (engine). |
| **Drive cache error / corrupt / disk issue** | any exception | **Fall back to reading the zip directly** (correctness never at risk). |
| **Route-decision DB read fails** | exception | Default to **reseed** (safe). |
| **Malformed engine JSON on success** | parse returns none | Success still honored by exit code; warning logged. |

---

## Concurrency / bulk

| Concern | Handling |
|---|---|
| Parallel seeds | Bounded semaphores: `SEED_CONCURRENCY` (heavy: seed/reseed), `RESET_CONCURRENCY` (light: delta/reset). One engine subprocess per account. |
| Same account twice | DB partial-unique index → second reset gets **409** (one active per email). |
| Persona cache under parallelism | Cross-process **file lock**; one build per persona, others wait then read. |
| Deployment | Run **one uvicorn worker** so the semaphores are the true global cap. |

---

## Known hardening still in progress (not yet shipped)
- Reaper for session rows stuck `queued`/`running` after a mid-write DB outage or process restart (so an account can't be blocked forever).
- Batch-level "quota tripped → defer remaining accounts" instead of each failing independently.
- Reseed **resume** (vs re-wipe) for very large personas that exceed the subprocess timeout.

# GAB Environment Platform — Core Logic (definitive reference)

The platform sits on two battle-tested engines. This document captures exactly how each
operation works, per module, so the reset/reseed/upload flows are first-class and predictable.

- **Seeder** = `gab-workspace-seed` (`seeder/`): CSV → authorize → push. Threaded bulk pusher.
- **Engine** = Vishal's `gab_seeder` (`engine/`): manifest-based seed / delta / reseed / reset.

> ⚠️ **The single most important fact:** the Seeder and the Engine use **different markers and
> different state**, so **an account uploaded by the Seeder cannot be `delta`-reset by the
> Engine** (the Engine needs its own manifest + markers). See "Critical decision" at the end.

---

## 0. Markers & state, side by side

| | Seeder (upload) | Engine (reset) |
|---|---|---|
| Gmail marker | label **`GAB-SEED`**; Message-ID `…@gab.ultraevals.local` | hidden label **`GAB_BASELINE_<seed_tag>`** + header **`X-GAB-Seed-ID`** |
| Calendar marker | `extendedProperties.private.gabSeeded=true` (+`gabEventId`) | `extendedProperties.private.gabSeed=<seed_tag>` (+`sourceEventId`) |
| Drive marker | folder **`GAB_UltraEvals__<persona>`**, `properties.gabSeeded=true` | `appProperties.gabSeed=<seed_tag>` + `gabPathSha256` + `gabContentSha256` |
| State | none (stateless; dedupe by re-reading account) | **manifest** `state/<email>/<persona>.manifest.json` (IDs + fingerprints) |
| Gmail write | `messages().insert` | `messages().import_` |
| Auth | DWD service account **or** consumer OAuth | consumer OAuth (full `mail.google.com`) |

---

## 1. FIRST UPLOAD (per module)

Upload = the **Seeder**'s push. Order per account: **wipe (if enabled) → then Calendar/Gmail/Drive
in parallel** (`run_populate`, `seeder/materialize/runner.py`). Each module dedupes by re-reading
the account, so a re-run is safe.

### Gmail — `populate_gmail`
- Ensures the `GAB-SEED` label. Builds MIME, `messages().insert` with labels `[GAB-SEED] + INBOX|SENT (+UNREAD)`.
- **Dedupe key:** normalized **Message-ID** OR **`subject|sizeEstimate`**. Existing match → skipped.
- Attachments come from the filesystem JSON; missing → omitted (not placeholders).

### Calendar — `populate_calendar`
- `events().insert` on `primary`, stamps `private.gabSeeded=true` + `gabEventId`. `sleep(1.25s)` per write.
- **Dedupe key:** `gabEventId` match, else `(title, start_epoch)`. Match+changed → `patch`; match+same → skip.
- Attendees folded into description (never real invites — avoids Gmail invite caps).

### Drive — `populate_drive_from_cache`  ← reads the pre-parsed cache, no JSON parse
- Ensures folder `GAB_UltraEvals__<persona>`. Uploads each file from **`persona_drive_cache/<persona>/`**
  via `files().create` (resumable ≥5 MiB), stamps `properties.gabSeeded=true`.
- **Dedupe key:** relative **path + size** (walks the seed folder once). Same size → skip; different → replace. Files >40 MB skipped as ineligible.

### Engine's own seed (`seed_persona`) — used only when the platform seeds via the Engine
- Writes a **manifest** recording every object's Google ID + fingerprint; `messages().import_`,
  `files().create` with client-supplied IDs, `events().insert`. **Checkpoints after every object**
  (atomic manifest write) → an interrupted seed resumes and skips already-seeded objects.

---

## 2. FIRST RESET = DELTA (Engine only; needs a complete manifest)

`reconcile_persona` compares live account state against **manifest + archive** and applies the
**minimal** changes. Requires a **complete** manifest (`completed_at` set, matching account/persona/
archive fingerprint) or it **fails closed**: *"explicit full reset required"*. After applying, it
**reads back and verifies**; on failure it raises and does **not** fall back to a destructive wipe.

| Module | Drift → Action |
|---|---|
| **Gmail** | missing → `import_message`; header/thread/immutable-label drift → `reimport_message` (delete+import); mutable-label drift → `patch_labels`; extra msg → `delete_message`; any draft → `delete_draft`; stray user label → `delete_label` |
| **Drive** | missing folder/file → `create_folder`/`create_file`; content/mime change → `replace_file`; metadata/marker drift → `patch_metadata`; extra → `delete_extra` (folders before files; deletes deepest-first) |
| **Calendar** | missing → `insert_event`; field drift (semantic compare) → `update_event`; duplicate/extra → `delete_event` |

"Ours" is matched by manifest ID → then by marker (`X-GAB-Seed-ID` / `appProperties.gabSeed==seed_tag`
/ `private.gabSeed==seed_tag`). Ambiguous identity → fail closed. Delta is **idempotent** and
checkpointed, so it doubles as skip/error recovery.

---

## 3. RESEED (Engine)

Sequence (`reset_persona` → `seed_persona` → `verify_persona`):
1. **Full delete** of the account (see §4). `reset_persona` then **deletes the manifest file**.
2. **Seed** fresh from the archive → writes a brand-new manifest (new `seed_tag`), checkpointing per object.
3. **Verify** read-back; mismatch → raise (fail closed).

Use reseed for **first-time baseline** and **persona switch**. After it, a complete manifest exists → `delta` is unlocked.

---

## 4. DELETE / FULL WIPE (per module)

### Engine — `reset_*_all` (destructive, permanent)
- **Gmail** `reset_gmail_all`: `messages().batchDelete` (1000/chunk) — **permanent** (needs full `https://mail.google.com/`) — then deletes all user labels.
- **Calendar** `reset_calendar_all`: `events().delete` each (tombstoned by Google), 1 s pace, 404 tolerated.
- **Drive** `reset_drive_all`: lists `'me' in owners` **incl. trash**, deletes files then folders **deepest-first**; permanent `files().delete`; **409-conflict tolerant** (re-check + backoff, the fix added earlier).

### Seeder — `wipe_*` (current edited state = FULL wipe, not marker-scoped)
- **Gmail** `wipe_seeded_mail`: `batchModify addLabelIds=[TRASH]` on **all** mail → **Trash only** (DWD scope is `gmail.modify`, cannot hard-delete). Loop-until-empty.
- **Calendar** `wipe_seeded_events`: `events().delete` on **all** primary events. Loop-until-empty.
- **Drive** `wipe_seed_folder`: trash **all** owned items. Loop-until-empty.

> **Scope reality:** permanent Gmail delete needs `mail.google.com`. The **DWD service account has only
> `gmail.modify`** → it can **trash** but not permanently delete. Per-account **consumer OAuth** (the
> Engine's path) has full scope → true permanent delete.

---

## 5. SKIP / ERROR RECOVERY

### Seeder — `only_skipped` (cheap, targeted)
- `GET /api/run/{run}/skipped` summarizes skipped items per account by **parsing the account log**
  (`Skip email …`, `Skip Drive file …`, `Skip large file …`, `Skip GitHub file …`).
- A push with `only_skipped:true` builds a `retry_plan` from those logs and re-pushes **only those
  items** per module (`populate_gmail(only_ids=…)`, `populate_drive_from_cache(only_paths=…)`),
  skipping Calendar and the full folder scan. **This is the "push skipped only" button.**

### Engine — `delta` is the recovery
- Because delta is idempotent + checkpointed, re-running it restores exactly what's missing/changed.
  No separate skip list needed.

---

## 6. VERIFY & fail-closed (both engines)

- **Seeder** `verify_seed`: counts read-back vs source per module → `Verify calendar X/Y · gmail X/Y · drive X/Y`.
  `overall=failed` **only when a checked module reads back 0 against a non-zero source**; short-by-some = `partial` (warning).
- **Engine** `verify_persona`: exact per-object match (IDs, markers, labels, content hashes, timezone). Any mismatch → not ok. Used as the post-apply gate for delta and reseed.

---

## 7. PARALLELIZATION — plan for reset/reseed

### What the code does today
- **Seeder (proven fast):** `push_all` → outer `ThreadPoolExecutor(max_workers=threads=10)` over chunks
  of `users_per_thread=20`; inner pool per chunk runs accounts concurrently; a **process-global
  `Semaphore(12)`** ("global Google slots") caps total Calendar/Gmail/Drive inserts in flight; a
  **per-email lock** serializes same-account pushes. Within an account: wipe serial → 3 modules parallel.
- **Engine (safe but serial):** **no threading**; one account fully serial (Drive→Gmail→Calendar,
  object-by-object, checkpoint each), with quota pacers (**Gmail 75 units/s**, **Calendar 1 s/write**,
  **Drive exp-backoff**). The Sheet queue caps **3 concurrent accounts** and enforces one-per-account.

### The design for parallel reset (max throughput, safe)
Parallelize **across accounts**, keep each account **serial internally** (the Engine is serial + quota-safe
per account; quotas are per-account so cross-account parallelism is safe up to project-global limits):

1. **Worker pool** over accounts — reuse the Seeder's shape: `ThreadPoolExecutor(max_workers=N)` (or an
   asyncio task pool in the API) running N resets concurrently.
2. **One active reset per email** — enforced by the DB partial-unique index
   `uniq_active_reset_per_email` (already in `schema.sql`) + an in-process per-email lock.
3. **Global API-slot semaphore** — mirror the Seeder's `Semaphore(12)` to cap total in-flight Google
   calls across all workers, so we don't trip project quotas.
4. **Concurrency cap N ≈ 8–12** to start (tune against observed Gmail 75-unit/s + Calendar daily caps).
   Calendar is the bottleneck (1 s/write + daily per-account cap), so expect it to gate throughput.
5. **Backpressure:** on quota errors, the account's reset checkpoints and reschedules (delta resumes);
   don't fail the whole batch.

Net effect: many accounts reset in parallel, each internally ordered and quota-paced — the Seeder's
concurrency model wrapped around the Engine's per-account correctness.

---

## 8. LOCAL LOGGING for reset/reseed + QC + auto-delete (plan)

**Goal:** QC can verify a reset by `task_allocation_id`, then the logs are purged — minimal storage.

- **Write a compact per-op record** (not the full verbose engine log) to
  `state/logs/<task_allocation_id>.json`:
  ```json
  {"reset_session_id":"…","task_allocation_id":"…","email":"…","persona":"…",
   "op":"delta|reseed|upload","status":"completed|failed",
   "started_at":"…","completed_at":"…",
   "modules":{"gmail":{"expected":170,"got":170},"calendar":{...},"drive":{...}},
   "actions":{"created":N,"patched":N,"deleted":N},"error":null}
  ```
  ~1–2 KB per reset. Keep the **verbose** engine/seeder log only transiently (or skip persisting it).
- **QC read:** `GET /api/qc/{task_allocation_id}` → returns that JSON (and, if kept, a tail of the verbose log).
- **QC confirm → purge:** `POST /api/qc/{task_allocation_id}/confirm` → deletes
  `state/logs/<task_allocation_id>.json` (and any verbose log), and optionally marks the `reset_sessions`
  row `qc_confirmed=true`. This is the "delete logs once QC confirms" step.
- **Storage math:** 200 accounts × a few resets each × ~2 KB ≈ **low single-digit MB**, and it trends to
  ~0 as QC confirms and purges. Add a safety cap (e.g. auto-delete records older than 7 days).

---

## 9. API INSERTS DURING BULK (upload & reset)

Our Supabase tables must fill **while** bulk runs — without slowing the batch.

- **Bulk upload (Seeder):** the merged seeder's **`db_hooks`** fire per account:
  `on_authorize → gab_accounts` (email, persona, token, authorized), `on_push_success → reset_sessions`
  (`mode=upload`, placeholder `task_allocation_id`) + sets `gab_accounts.last_reset_persona`.
  *(The standalone old bulk code has no hooks — that's why the current bulk run isn't populating the
  tables. Bulk must run through the merged seeder to insert.)*
- **Bulk reset (API/worker):** each reset writes `reset_sessions` (`queued→running→completed/failed`,
  timestamps, mode) and updates `gab_accounts.last_reset_persona`.
- **Don't block the batch:** perform DB writes **fire-and-forget / best-effort** (like `db_hooks`, which
  swallow errors) or via a tiny queue, so a slow/failed Supabase write never stalls a push/reset.

---

## Critical decision (must resolve before "first-class" is true)

**Pick ONE seeding system for accounts we intend to reset:**

- **Option A — Engine for both upload and reset.** Upload via `seed_persona` (creates the manifest) →
  `delta` works (fast, surgical). Costs: slower first upload, consumer-OAuth per account (full Gmail scope).
- **Option B — Seeder for upload, Engine only for reseed.** Keep the fast threaded bulk upload; "reset"
  = **reseed** (full wipe + engine seed), since Seeder-seeded accounts have no engine manifest for delta.
  Simpler, but no fast delta and every reset is a full rebuild.

Everything else (parallelization, logging, API inserts) is ready to build on top once A or B is chosen.

### Secondary items
- **Port today's Seeder fixes** (full wipe, `_fixed_push_body` honoring `wipe`, drive-wipe-all,
  full-Gmail scope) into `seeder/` — they were made on the standalone old code.
- **Gmail permanent delete** needs `mail.google.com`; DWD SA has only `gmail.modify` (trash only).
- **Calendar daily write cap** is the throughput ceiling for bulk — pace/queue accordingly.

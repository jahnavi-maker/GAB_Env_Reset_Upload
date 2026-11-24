> **Note (24 Nov 2026):** Wave 2 is live in production; the deploy/test-plan notes below are historical. Operations: `docs/RUNBOOK.md`.

# Wave 2 persona seeding (GAB Phase 9 · Wave 2)

Branch `feat/wave2-contacts-tasks`, based on the live EC2 branch `unified` (6d370a9).

Wave 2 personas are a different format (GAB v2 turn-split: `turn_1/turn_1_world.json`) and
cover seven surfaces instead of three. This branch adds a separate, additive path for them.
**With `GAB_WAVE2_ROOT` unset, nothing changes**: no persona matches, no extra scopes are
requested, and clean is exactly as before.

## What it does

| Surface | Seed | Wipe | Restore (delta / reconcile) |
|---|---|---|---|
| Gmail | ✅ messages, threads, labels (custom + starred/important/unread/sent), drafts | ✅ full mailbox, drafts, user labels | ✅ |
| Calendar | ✅ secondary calendars, recurrence, time zones | ✅ primary cleared, secondary calendars deleted, subscriptions removed | ✅ |
| Drive | ✅ folders; Docs/Sheets from text; PDF/Office via Google export; text files | ✅ all owned files, trash emptied, shared-in files removed | ✅ |
| Contacts | ✅ (new) | ✅ contacts + contact groups (new) | ✅ |
| Tasks | ✅ (new) | ✅ task lists + tasks (new) | ✅ |
| Keep | ❌ no consumer API → counted, reported as manual | ❌ manual | ❌ |
| Google Chat | ❌ no consumer API → counted, reported as manual | ❌ manual | ❌ |

Seeding reuses Google's reference seeder (`seeder/pilot10/gab_seed.py`, unchanged), driven
with the platform's tokens. The synthetic persona email (e.g. `diana.kowalczyk.den.0@gmail.com`)
is replaced by the account's real address in all seeded content, so accounts keep their
current `geminiapp…` emails.

Not recreatable from the zip (no file bytes, only summaries): 30 Drive files across the 13
personas: 28 images, 1 audio file and 1 PowerPoint (the reference seeder can export Docs and
Sheets, not Slides). PDF, Word and Excel files are generated from their `content_summary` text;
code/script files are uploaded as text.

## Modes

| Platform mode | Wave 2 action |
|---|---|
| upload / `seed` / `reseed` | full API wipe → seed → baseline snapshot |
| `delta` / `reconcile` | restore to baseline (delete agent additions, recreate changed/missing); full reseed if no baseline yet |
| `clean` (empty universe) | existing clean, **plus** Contacts/Tasks/labels/drafts/extra calendars when Wave 2 is enabled and the account has a Wave 2 grant |

Result detail example:
`wave2 reseed arthur_pendelton: gmail 156/156, calendar 109/109, drive 14/14, contacts 24/24, tasks 4/4; manual (no API): keep 8 items, chat 6 messages`

## Code map

| File | Change |
|---|---|
| `seeder/wave2/personas.py` | new: persona discovery (`GAB_WAVE2_ROOT`), loose name matching (titles, `Name : Archetype`), seeding context (timezone, email rewrite) |
| `seeder/wave2/creds.py` | new: credentials with Contacts + Tasks; clear re-authorize error when the grant is missing |
| `seeder/wave2/runner.py` | new: seed / restore / full wipe / verify / clean extras |
| `seeder/materialize/auth.py` | added `WAVE2_EXTRA_SCOPES`, `WAVE2_SCOPES`; `make_flow(extra_scopes=None)` (default unchanged) |
| `api/reset_service/upload.py` | authorize URL asks for Contacts + Tasks only for a Wave 2 persona; callback uses the same scopes |
| `api/reset_service/engine.py` | `run_reset` routes Wave 2 personas to `_run_wave2`; clean calls the Wave 2 extra wipe |
| `seeder/pilot10/*` | the pilot seeder (from `pilot10-seeder`), imported as a library |
| tests | `seeder/tests/test_wave2.py` (22), `api/tests/test_wave2_routing.py` (13) |

## Configuration

| Env var | Purpose |
|---|---|
| `GAB_WAVE2_ROOT` | folder of Wave 2 persona folders. **Unset = feature off.** |
| `GAB_WAVE2_STATE_DIR` | seed state + baselines (default `$GAB_STATE_DIR/wave2`) |
| `GAB_REQUEST_WAVE2_SCOPES=1` | optional: ask for Contacts + Tasks on every authorization |

Persona folder layout (exactly as unzipped from the tasking zip):

```
$GAB_WAVE2_ROOT/arthur_pendelton/manifest.json
$GAB_WAVE2_ROOT/arthur_pendelton/turn_1/turn_1_world.json
```

On EC2 the 13 personas are staged at `/home/ubuntu/gab-data/wave2/personas` (not yet
referenced by the live service).

## Before testing (one-time, Google Cloud)

1. In **each** OAuth client's Google Cloud project: enable **People API**, **Google Tasks API**
   and **Google Sheets API** (Gmail, Calendar, Drive are already on).
2. OAuth consent screen: add the `contacts` and `tasks` scopes (Testing mode allows them for
   test users).
3. Workspace (domain-wide delegation) accounts only: add
   `https://www.googleapis.com/auth/contacts,https://www.googleapis.com/auth/tasks` to the
   service account's DWD scopes. DWD is all-or-nothing, so add them alongside the existing three.
4. Re-authorize each Wave 2 account on `/onboard` **with its Wave 2 persona in the row**; the
   consent screen will list Contacts and Tasks. Accounts authorized before this keep working
   for existing personas (verified: Google refuses only the wider refresh, `invalid_scope`;
   the normal refresh still succeeds).

## Test plan (tomorrow)

1. Deploy the branch to a test checkout (not the live service) or locally; set `GAB_WAVE2_ROOT`.
2. Pick ONE spare account. Authorize it on `/onboard` with persona `Arthur Pendelton`
   (smallest calendar). Confirm the consent screen shows Contacts + Tasks.
3. Upload → expect `gmail 156/156, calendar 109/109, drive 14/14, contacts 24/24, tasks 4/4`.
   Spot-check in the account: a recurring event, a starred email, a custom label, a Google
   Sheet, a contact, a task. Confirm the persona email shows as the account's own address.
4. Make agent-style edits (new email, new event, delete a contact, edit a doc) → run
   **reconcile** → confirm additions gone and originals back.
5. Run **clean** on it → Gmail/Drive/Calendar/Contacts/Tasks empty.
6. Regression: one existing persona account (e.g. `Student`) upload + reconcile, and one
   blank-persona clean, with `GAB_WAVE2_ROOT` set. Same results as today.
7. Then the 13 personas.

## Known gaps / follow-ups

- **Keep and Google Chat**: not seedable on consumer accounts (decision pending; see the
  Phase 9 Wave 2 requirements doc).
- **Account settings** (display name, home address, Gemini Apps Activity / extensions) are
  manual; the run result includes the persona's name, home address and timezone for this.
- **Progress bar**: Wave 2 runs show status (running → done/failed + the per-surface counts
  in the detail), not the per-item live progress the classic pipeline shows.
- Seeding time per account is driven by Calendar (up to ~500 events); expect several minutes.

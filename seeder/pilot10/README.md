# Pilot 10 persona seeder (GAB v2)

- `gab_seed.py`: Google's reference seeder, **unchanged** (byte-identical copy).
- `gab_seed_v2.py`: Deccan extension. Imports `gab_seed.py` and only adds or overrides behaviour. Use this one.

## What v2 adds over gab_seed.py
| | gab_seed.py | gab_seed_v2.py |
|---|---|---|
| Drive PDFs (30) | regenerated from text summary | **original PDF uploaded** |
| Drive docx/xlsx/pptx bytes (22) | text summary → Doc/Sheet; pptx skipped | **original converted** to Doc / Sheet / Slides (persona email rewritten inside) |
| Drive images (4) | skipped | **uploaded** |
| Home address | regex misses table format | reads both formats |
| Wipe | mail, owned Drive, calendars, contacts, tasks | + drafts, custom labels, Drive Trash, shared-in files, calendar subscriptions, contact groups, completed tasks |
| `wipe-check` | — | shows anything left after wipe |
| `manual` | — | writes `_seed_state/<persona>_MANUAL.md` |
| Run log | — | `_seed_state/runlog.csv` (for the Eval Sheet "Email id" column) |

## One-time setup
1. Google Cloud project with Gmail, Calendar, Drive, Sheets, People and Tasks APIs enabled.
2. OAuth consent screen: External, Testing. Add all 6 scopes and the 7 demo accounts as test users.
3. Desktop OAuth client → save as `client_secret.json` in the ROOT folder (next to the persona folders).
4. `pip install google-api-python-client google-auth-oauthlib markdown` (or use `api/.venv`).

ROOT = the folder with the unzipped persona folders. Pass `--root "<folder>"` or set `GAB_PILOT_ROOT`.

## Per account
```
python3 gab_seed_v2.py init --root "$R"                  # then fill account_email in accounts.csv
python3 gab_seed_v2.py plan   elena_alvarez_gomez --root "$R"
python3 gab_seed_v2.py auth   elena_alvarez_gomez --root "$R"   # sign in in the browser yourself
python3 gab_seed_v2.py audit  elena_alvarez_gomez --root "$R"
python3 gab_seed_v2.py wipe   elena_alvarez_gomez --root "$R" --confirm <account email>
python3 gab_seed_v2.py wipe-check elena_alvarez_gomez --root "$R"
#   ...do the manual wipe steps (Keep, Trash folders, Other contacts, Gemini)...
python3 gab_seed_v2.py seed   elena_alvarez_gomez --root "$R"
python3 gab_seed_v2.py verify elena_alvarez_gomez --root "$R"
python3 gab_seed_v2.py manual elena_alvarez_gomez --root "$R"
# between agent runs:
python3 gab_seed_v2.py reset  elena_alvarez_gomez --root "$R" && python3 gab_seed_v2.py verify elena_alvarez_gomez --root "$R"
```
Passwords never go in any file. You type them only in the browser during `auth`.

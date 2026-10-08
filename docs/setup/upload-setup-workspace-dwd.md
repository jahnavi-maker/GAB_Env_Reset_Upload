# Upload setup — **teamdeccan.us / Deccan Experts** accounts (Domain-Wide Delegation)

For Google **Workspace** accounts on a domain we administer (e.g. `@teamdeccan.us`).
One service-account key authorizes the whole domain — **no per-account sign-in**.
Goal: reach **Bulk upload** on `/onboard`.

Scopes used (seeding + reset):
`https://mail.google.com/`, `https://www.googleapis.com/auth/calendar`, `https://www.googleapis.com/auth/drive`

---

## A. Google Cloud Console — one-time (gives you the service-account key)

1. Open https://console.cloud.google.com → create or pick a project.
2. **APIs & Services → Enable APIs** — enable all three:
   - Gmail API
   - Google Calendar API
   - Google Drive API
3. **APIs & Services → Credentials → Create credentials → Service account** → create.
4. Open the service account → **Keys → Add key → Create new key → JSON** → download.
   This JSON is the **admin service-account key** you'll drop on the page.
5. On the service account, note its **Client ID** (a long number) — needed in step 6.
   (Domain-wide delegation is enabled by registering it in Admin, next.)

## B. Google Admin Console — one-time (admin.google.com)

6. **Security → Access and data control → API controls → Domain-wide delegation →
   Add new**:
   - **Client ID**: the service account's Client ID from step 5.
   - **OAuth scopes** (comma-separated, paste exactly):
     ```
     https://mail.google.com/,https://www.googleapis.com/auth/calendar,https://www.googleapis.com/auth/drive
     ```
   - **Authorize**.
7. **(Only if passwords need resetting) Bulk password reset** —
   **Directory → Users**: use **Bulk update users** (download the CSV, set new
   passwords, re-upload), or reset a single user from their row. Not required for
   seeding (DWD needs no per-account password), only if the client will log in.

## C. On our page — https://gab-seed.soulhq.ai/onboard

8. **Step 1 · Accounts** — load the CSV. Columns: `email, persona, mode`
   (password column can be left blank — DWD doesn't sign in per account).
   - `mode` = `upload` for a first seed (blank = auto).
9. **Step 2 · OAuth client** — drop the **admin service-account key** JSON in the dropzone.
   Should read "🔑 Domain delegation active ✓" — all `@teamdeccan.us` accounts are now
   authorized automatically (no per-account step).
10. **Step 3 · Authorize & upload** — click **Bulk upload all** to seed every account.

Done — uploads write `reset_sessions` + the manifest, with per-service progress on the row.

> Mixed list? Any `@gmail.com` rows still need individual **Authorize** (see the Gmail doc);
> Workspace rows are already authorized by the key.

# Upload setup — Client-given **Gmail** accounts (per-account OAuth)

For consumer `@gmail.com` accounts the client gives us. Each account is authorized
individually by signing in with its password. Goal: reach **Bulk upload** on `/onboard`.

Scopes used (seeding + reset):
`https://mail.google.com/`, `https://www.googleapis.com/auth/calendar`, `https://www.googleapis.com/auth/drive`

---

## A. Google Cloud Console — one-time (gives you `client.json`)

1. Open https://console.cloud.google.com → create or pick a project.
2. **APIs & Services → Enable APIs** — enable all three:
   - Gmail API
   - Google Calendar API
   - Google Drive API
3. **APIs & Services → OAuth consent screen**:
   - User type: **External** → create.
   - Add the 3 scopes above.
   - Under **Test users**, add every client Gmail address you'll authorize
     (or publish the app). Unlisted accounts can't consent.
4. **APIs & Services → Credentials → Create credentials → OAuth client ID**:
   - Application type: **Web application**.
   - **Authorized redirect URIs → Add**: `https://gab-seed.soulhq.ai/oauth/callback`
   - Create → **Download JSON** → this is your `client.json`.

> If the onboard page later says "register these redirect URIs", add exactly what it lists.

## B. On our page — https://gab-seed.soulhq.ai/onboard

5. **Step 1 · Accounts** — paste or load the CSV. Columns: `email, persona, password, mode`
   - `mode` = `upload` for a first seed (blank = auto).
   - Password stays in your browser; it's only used to sign in during consent.
6. **Step 2 · OAuth client** — drop your **`client.json`** in the dropzone.
   Should read "OAuth web client configured ✓".
7. **Step 3 · Authorize & upload**:
   - Click **Authorize all (one by one)** → a Google consent tab opens per account →
     sign in (copy the password from the row) → **Allow** → the row flips to **authorized**.
   - Click **Bulk upload all** to seed every authorized account.

Done — uploads write `reset_sessions` + the manifest, with per-service progress on the row.

---

**Note on passwords:** consumer Gmail has no admin bulk reset. Passwords come from the
client (in the CSV). If a password is wrong, consent fails for that row — fix it and
re-authorize just that account.

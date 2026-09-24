# What we need from the client for easier authentication

_GAB Environment Platform · account authentication at scale (~200 accounts)_

## The goal
To seed / reset a Google account (Gmail, Drive, Calendar, GitHub-as-Drive) the platform must **authenticate as that account**. Doing this for ~200 accounts, repeatedly, needs the right account setup on the client's side. There are two paths — one is dramatically easier.

---

## ✅ Option A — Google Workspace + Domain-Wide Delegation (recommended)
If the accounts live under a **Google Workspace domain the client controls**, a single service account can act on behalf of every user automatically.

**Result:** no passwords, no consent screens, **no 100-account cap, no 7-day token expiry, no re-authorization ever.** Fully automated, set up once.

**What we need from the client:**
1. The ~200 accounts provisioned under a **dedicated Workspace domain or subdomain** (e.g. `gab.<client>.com`).
2. A Workspace **admin** to authorize our service account (we provide the client ID + scopes) for **Domain-Wide Delegation**.
3. The **list of account emails**.

> This is the same proven mechanism our internal `@deccanexperts.us` bulk path already uses. It removes the authentication problem entirely.

---

## ⚠️ Option B — Consumer `@gmail.com` accounts
If the accounts must remain personal `@gmail.com`, Google requires a **human consent per account**, and (until the app is verified) enforces a 100-account cap and a 7-day token expiry.

**What we need from the client to make this as smooth as possible:**
1. **Verify/publish the OAuth app** with Google — removes the **100-account cap** and the **7-day expiry**. (Needs app ownership, a privacy-policy URL, and a domain; review takes days–weeks, so start early.)
2. The accounts **pre-signed into a single browser profile** — then authorizing is just clicking **"Allow"** per account, with **no password typing**.
3. Someone available to **click "Allow" once per account** (Google always requires a human consent click for consumer accounts — this cannot be automated).
4. The **list of account emails** (added as test users while the app is still in Testing).

> Note: we **cannot** enter Google passwords programmatically — Google blocks scripted logins and it is not permitted. Passwords stay with the client; the browser-profile approach avoids typing them repeatedly.

---

## Also helpful (either option)
- A **stable HTTPS URL** for the platform. If the client can provide a **subdomain they own** (e.g. `gab.<client>.com` → our server), authentication and sign-in are cleaner and the setup is production-grade. Without it we use a temporary hostname, which is fine for testing but not ideal long-term.

---

## The one decision that drives everything
**Can the ~200 accounts live under a Google Workspace domain (→ Option A, DWD), or must they stay consumer `@gmail.com` (→ Option B, verify the app)?**

- **Workspace/DWD:** easiest, fully automated, recommended.
- **Consumer gmail:** workable, but needs app verification + a one-time human consent per account.

Please confirm this so we can start the setup on the right track — both paths have lead time.

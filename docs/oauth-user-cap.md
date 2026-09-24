# GAB — OAuth User Cap & Authorization

_2026-09-23 · Divya_

## The problem

Scaling the GAB platform to ~200 **consumer `@gmail.com`** accounts is blocked by two hard Google OAuth limits that apply while the OAuth app is in **Testing** mode:

1. **100 test-user cap** — an unverified app can list at most **100 test users**, and only listed accounts can complete consent. 200 accounts cannot all be authorized at once.
2. **7-day refresh-token expiry** — a token granted in Testing mode is revoked after **7 days**, forcing a fresh human consent ("Allow") for every account, every week.

Neither is a defect in our code — both are Google policy for unverified consumer-OAuth apps using sensitive scopes (Gmail/Drive/Calendar).

## What each limit actually is

The test-user list and the token are two different things (commonly conflated):

|            | Test-user list | Refresh token |
|------------|----------------|---------------|
| What it is | who is _allowed_ to consent to the unverified app | the credential granted _after_ they consent |
| Lifetime   | **persistent** — stays until manually removed (max 100) | **expires after 7 days** in Testing mode |

Confirmed behavior:

- **Test users are NOT cleared after 7 days.** Once a token expires the account is still a test user — you only need to **re-consent**, not re-add it.
- **Using the refresh token does not renew it** in Testing mode — the 7-day clock is absolute. (In _production_ mode a refresh token only dies after ~6 months of non-use, so regular use keeps it alive indefinitely.)
- **An expired refresh token cannot re-authorize.** A refresh token only mints short-lived access tokens _while valid_; once revoked, the only path to a new one is a fresh human consent.

## Why rotating test users doesn't work

Adding 100 → authorizing → removing them → adding the next 100 fails on three counts:

- **Removal may revoke tokens** (undocumented) — removing batch 1 to make room for batch 2 could kill batch 1's tokens on the spot.
- **It doesn't dodge the 7-day expiry** — every token still dies weekly and needs re-consent, which needs test-user membership at that moment.
- **100 slots can't keep 200 alive** — you'd juggle add / re-consent / remove for both halves every week, indefinitely.

Not viable for production.

## The two real fixes

**1. Publish + verify the OAuth app.** Moves it out of Testing mode → **no 100-cap, no 7-day expiry** (tokens live as long as they're used). Needs Google's OAuth verification review for the sensitive scopes (Gmail/Drive/Calendar) — days to weeks, so start early.

**2. Workspace domain + domain-wide delegation (DWD).** Move the accounts under a **dedicated Google Workspace domain/subdomain**; a service account impersonates each user with **no consent, no expiry, no cap, no re-auth ever** — exactly how the `@deccanexperts.us` bulk path already works. Requires the accounts to be Workspace accounts, not consumer gmail.

> DWD is **domain-scoped, not per-OU** — a DWD grant can't be limited to one Organizational Unit. Isolation comes from a **dedicated domain/subdomain** holding only these accounts, not an OU inside a shared company domain.

## Recommendation & decision needed

**Recommended:** if the accounts can be Workspace accounts, take **Option 2 (DWD)** — it removes the OAuth problem entirely and reuses the proven bulk path. If they must stay consumer `@gmail.com`, **Option 1 (verify the app) is required**; rotation is not a substitute.

**Decision from client/manager:** can the 200 accounts live under a dedicated Workspace domain (→ DWD), or do they stay consumer gmail (→ verify the app)? Start the chosen track **now** — verification and domain provisioning both have long lead times.

**Interim (Testing mode, ≤100 accounts):** authorize via the onboarding page; when a token expires at day 7, click **Re-authorize** on that account (still a test user) — the new refresh token is saved automatically.

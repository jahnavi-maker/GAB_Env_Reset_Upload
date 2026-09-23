# OAuth and scaling notes

## What can and cannot be automated

The seeder can automate account enumeration, opening the installed-app flow, callback handling, identity verification, encrypted-at-rest host storage decisions, token health checks, and operational status. It intentionally cannot type passwords, choose accounts, or consent on a user's behalf.

For ordinary consumer Gmail accounts, plan one initial human authorization per account. Stored refresh tokens should make that a one-time activity unless the user revokes access, credentials are invalidated, or the OAuth app remains in a mode where grants expire periodically.

## Google Cloud setup

- Use a dedicated Google Cloud project.
- Enable Gmail, Drive, and Calendar APIs.
- Create an OAuth Desktop-app client.
- Configure an External OAuth audience for consumer Gmail accounts.
- Add accounts as test users while the app is in Testing.
- Review Google's verification and publishing requirements before moving beyond Testing, especially for restricted Gmail scopes.
- Never distribute the client-secret JSON in source archives.

Required scopes in the current implementation:

```text
https://mail.google.com/
https://www.googleapis.com/auth/drive
https://www.googleapis.com/auth/calendar
```

## Two hundred accounts

Recommended operating model:

1. Produce an account onboarding ledger without passwords.
2. Authorize accounts in controlled batches and record only success/failure plus token-file presence.
3. Verify the returned Gmail profile after every consent.
4. Keep token files mode 0600 in a restricted host directory.
5. Run a read-only account/profile check before scheduling seeds.
6. Seed different accounts with bounded parallelism, while serializing each account.
7. Pace Gmail by quota units rather than request count. HTTP batching does not reduce Gmail quota cost.
8. Preserve manifests/checkpoints so retries resume instead of replaying successful work.
9. Move recurring resets to sparse delta reconciliation rather than wipe/reseed.

If all accounts are in an administratively controlled Google Workspace domain, separately evaluate domain-wide delegation with security/admin approval. Do not assume it is available, and do not use it for consumer Gmail accounts.

## Runtime expectations

- Full first seed: previously observed/communicated estimate of approximately 50–60 minutes per account, strongly dependent on content size.
- Sparse reset: generally under 10 minutes when drift is small; large Gmail inventories may spend several minutes in quota-shaped discovery and verification.
- Different accounts may run concurrently within a project-wide budget; the same account should have one active worker.

# GAB Environment Reset Pipeline — Source Handoff

Sanitized developer handoff of the GAB account seeding and sparse reset pipeline.

Source revision: `0b7f48192cebd68e13e8d847779af50579d1da75`

## Included

- Gmail, Drive, and Calendar baseline seeding and verification.
- Sparse desired-state reset: discover drift, plan deterministic changes, mutate only changed objects, and verify the final state.
- One-time per-account OAuth onboarding with exact account-identity verification.
- Checkpointed resume, quota-safe Gmail pacing, and bounded Google API retries.
- CLI and guarded reset wrapper.
- Optional, sanitized Apps Script queue/UI and local worker templates.
- Unit and integration-style tests that use fakes; they do not contact Google.

## Deliberately excluded

This archive contains no OAuth client-secret JSON, refresh/access tokens, live account emails, passwords, source environment archives, rubrics/account workbooks, manifests/checkpoints, cached attachments, live spreadsheet/script/deployment IDs, Git history, or virtual environment.

## Install and test

Requires Python 3.11 or newer.

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest -q
```

## Configure

```bash
cp config.example.json config.json
```

Set paths for the source archive, rubrics workbook, account workbook, OAuth Desktop-client JSON, token directory, state directory, and persona-to-account mappings. Keep `config.json`, the token directory, and the state directory private and outside source control.

Generate a proposed configuration from source inputs:

```bash
.venv/bin/gab-seed init-config   --archive /secure/path/GAB_Ultra_Core_Environments.zip   --rubrics /secure/path/GAB_Task_Rubrics.xlsx   --accounts /secure/path/Bench_Test_Accounts.xlsx   --client-secret /secure/path/oauth_desktop_client_secret.json   --token-dir "$HOME/.config/gab-seeder/tokens"   --state-dir "$PWD/.state"   --output config.json
```

## OAuth behavior

For consumer Gmail accounts, this implementation uses Google's installed-app OAuth flow. Each account must be selected by a human once on Google's consent page. The code does not read account passwords and does not bypass Google's consent controls.

```bash
.venv/bin/gab-seed auth --config config.json --account test-account@example.com
```

The callback alone is not success: the CLI reads the authenticated Gmail profile, requires it to match the requested account exactly, and only then stores a mode-0600 refresh token. Revoked or expired grants are removed only for that account and reauthorized interactively.

For a large pool, automate the queue, account selection, progress tracking, and token-health checks, but retain the human consent step unless the accounts belong to a managed Workspace domain where an administrator has deliberately approved a separate domain-wide-delegation design. Domain-wide delegation is not implemented in this package and does not apply to ordinary consumer Gmail accounts.

See `docs/OAUTH_AND_SCALING.md`.

## First-time seed

Dry-run is the default:

```bash
.venv/bin/gab-seed seed --config config.json --persona Example_persona
```

After reviewing the plan:

```bash
.venv/bin/gab-seed seed --config config.json --persona Example_persona --execute
.venv/bin/gab-seed verify --config config.json --persona Example_persona
```

First-time setup duration is account-size dependent. The observed working estimate previously communicated for the GAB accounts was roughly 50–60 minutes per user for a full first seed. Large Gmail/Drive inventories and Google quota windows dominate runtime.

## Sparse reset between model runs

Preview:

```bash
.venv/bin/gab-seed delta --config config.json --persona Example_persona
```

Execute and verify:

```bash
.venv/bin/gab-seed delta --config config.json --persona Example_persona --execute
```

Typical sparse resets were communicated as under 10 minutes, but duration depends on drift volume and Gmail quota-safe scans. The reset never silently escalates into a destructive full reset.

A destructive reseed is a separate, explicitly confirmed fallback for dedicated disposable accounts only.

## Optional shared reset console

1. Copy `reset_control.example.json` to `reset_control.json` and fill only your own Sheet/Apps Script deployment values.
2. Replace placeholder account mappings and spreadsheet ID in `apps-script/Code.gs` before deployment.
3. Adapt `scripts/gab_reset_worker_tick.sh` paths for the worker host.
4. Deploy with `scripts/deploy_reset_console.py` only after reviewing domain and execution permissions.

The web UI queues work; the local worker owns credentials and performs Google mutations. Do not place OAuth secrets or refresh tokens in Apps Script or the control Sheet.

## Safety model

- Dry-run by default; live writes require `--execute`.
- Exact authenticated-account verification.
- No passwords read or stored.
- No email sending and no Calendar attendee notifications.
- No Drive sharing/permission changes.
- Sparse reset fails closed on ambiguous identities.
- Same-account jobs are serialized.
- Retryable failures preserve checkpoints and resume in the same mode.
- Full destructive reset requires explicit account confirmation.

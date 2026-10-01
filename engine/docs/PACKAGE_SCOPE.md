# Package scope and exclusions

This handoff was built from tracked source at the commit recorded in `SOURCE_REVISION.txt`.

Included source areas:

- `src/gab_seeder/`
- `scripts/`
- `apps-script/` with live values replaced by placeholders
- `tests/` with synthetic account fixtures
- `pyproject.toml`
- `config.example.json`

Excluded:

- live `reset_control.json`
- live `config.json`
- `.state/` manifests, plans, and checkpoints
- OAuth client secrets and account token files
- source ZIP/workbooks and cached attachment bodies
- screenshots and generated artifacts
- `.git/`, virtual environments, caches, and logs

`PACKAGE_MANIFEST.json` records SHA-256 checksums of all files in this archive.

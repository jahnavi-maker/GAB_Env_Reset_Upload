#!/usr/bin/env python3
"""Preview or reset/reseed one dedicated GAB test account."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gab_seeder.config import load_config  # noqa: E402
from gab_seeder.seeder import reconcile_persona, reset_persona, seed_persona, verify_persona  # noqa: E402


def resolve_account(config: dict[str, Any], value: str) -> tuple[str, str]:
    needle = value.casefold()
    matches: list[tuple[str, str]] = []
    for persona, account in config["accounts"].items():
        email = str(account["email"])
        short = email.split("@", 1)[0].rsplit(".", 1)[-1]
        if needle in {persona.casefold(), email.casefold(), short.casefold()}:
            matches.append((persona, email))
    if len(matches) != 1:
        choices = ", ".join(
            f"{str(account['email']).split('@', 1)[0].rsplit('.', 1)[-1]} ({persona})"
            for persona, account in config["accounts"].items()
        )
        raise ValueError(f"unknown account {value!r}; choose one of: {choices}")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Restore a persona's GAB environment incrementally, with an explicit full-reset fallback."
    )
    parser.add_argument("account", help="Short account such as test-account-411, full email, or persona")
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.json"))
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Apply the incremental restore; without this flag the command is a read-only preview",
    )
    parser.add_argument(
        "--full-reset",
        action="store_true",
        help="Explicit fallback: wipe and reseed the selected services instead of reconciling drift",
    )
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    persona, email = resolve_account(config, args.account)
    services = {"gmail", "drive", "calendar"}

    if args.full_reset and not args.execute:
        parser.error("--full-reset is destructive and requires --execute")

    if not args.execute:
        result = reconcile_persona(
            config_path,
            persona=persona,
            services=services,
            dry_run=True,
        )
        result["next_command"] = " ".join(
            shlex.quote(str(value))
            for value in [sys.executable, Path(__file__).resolve(), args.account, "--execute"]
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.full_reset:
        print(f"DESTRUCTIVE FULL RESET: {email} ({persona})", file=sys.stderr)
        print(
            "This permanently removes every Gmail message/draft, owned Drive item, and primary-calendar event.",
            file=sys.stderr,
        )
    else:
        print(f"INCREMENTAL RESTORE: {email} ({persona})", file=sys.stderr)
        print(
            "This restores changed or missing Gmail, Drive, and Calendar baseline objects and removes classified extras.",
            file=sys.stderr,
        )
    confirmation = input("Type the complete account email to continue: ").strip()
    if confirmation.casefold() != email.casefold():
        print("Confirmation did not match; nothing was changed.", file=sys.stderr)
        return 2

    if not args.full_reset:
        print("Reconciling Gmail, Drive, and Calendar with the verified baseline...", file=sys.stderr, flush=True)
        delta = reconcile_persona(
            config_path,
            persona=persona,
            services=services,
            dry_run=False,
        )
        print(json.dumps({"delta": delta}, indent=2, default=str))
        return 0 if delta.get("verify", {}).get("ok") else 3

    print("1/3 Resetting account...", file=sys.stderr, flush=True)
    reset = reset_persona(
        config_path,
        persona=persona,
        services=services,
        confirm_account=confirmation,
        dry_run=False,
    )
    print("2/3 Replaying baseline...", file=sys.stderr, flush=True)
    seed = seed_persona(config_path, persona=persona, services=services, dry_run=False)
    print("3/3 Verifying baseline...", file=sys.stderr, flush=True)
    verify = verify_persona(config_path, persona=persona)
    print(json.dumps({"reset": reset, "seed": seed, "verify": verify}, indent=2, default=str))
    return 0 if verify["ok"] else 3


if __name__ == "__main__":
    raise SystemExit(main())

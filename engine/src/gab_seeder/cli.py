from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path
from typing import Any

from .archive import EnvironmentArchive
from .config import load_config
from .github_seed import extract_repository, push_repository
from .preflight import run_preflight, write_report
from .seeder import authenticate, reconcile_persona, reset_persona, seed_persona, verify_persona


def _print(value: Any) -> None:
    print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _services(value: str) -> set[str]:
    result = {item.strip() for item in value.split(",") if item.strip()}
    unknown = result - {"drive", "gmail", "calendar"}
    if unknown:
        raise argparse.ArgumentTypeError(f"unknown services: {sorted(unknown)}")
    return result


def cmd_preflight(args: argparse.Namespace) -> int:
    report = run_preflight(args.archive, args.rubrics, args.accounts)
    if args.output:
        write_report(args.output, report)
    _print(
        {
            "task_count": report["task_count"],
            "account_count": report["account_count"],
            "relevant_personas": report["relevant_personas"],
            "task_environment_counts": report["task_environment_counts"],
            "totals": report["totals"],
            "warnings": [task for task in report["tasks"] if task.get("warning")],
            "report": str(Path(args.output).resolve()) if args.output else None,
        }
    )
    return 0


def cmd_init_config(args: argparse.Namespace) -> int:
    report = run_preflight(args.archive, args.rubrics, args.accounts)
    mapping = report["suggested_mapping"]
    data = {
        "archive": str(Path(args.archive).expanduser().resolve()),
        "rubrics": str(Path(args.rubrics).expanduser().resolve()),
        "accounts_workbook": str(Path(args.accounts).expanduser().resolve()),
        "client_secret": str(Path(args.client_secret).expanduser().resolve()),
        "token_dir": str(Path(args.token_dir).expanduser().resolve()),
        "state_dir": str(Path(args.state_dir).expanduser().resolve()),
        "missing_attachment_policy": "error",
        "missing_attachment_policies": {
            "Student": "omit",
            "Applied_ML_and_data_scientist": "omit",
        },
        "accounts": mapping["accounts"],
        "spare_accounts": mapping["spares"],
        "task_environment_overrides": {
            "6a84bc5ab47b41eee8a5a796": "Applied_ML_and_data_scientist"
        },
        "github": {
            "Backend_software_engineer": {
                "extract_dir": str(Path(args.state_dir).expanduser().resolve() / "github" / "Backend_software_engineer"),
                "remote_url": ""
            }
        },
    }
    output = Path(args.output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(data, indent=2), encoding="utf-8")
    _print({"config": str(output), "accounts": len(data["accounts"]), "spares": data["spare_accounts"]})
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    rows = [
        {"persona": persona, "email": account.get("email"), "timezone": account.get("timezone")}
        for persona, account in config["accounts"].items()
    ]
    _print({"accounts": rows, "spare_accounts": config.get("spare_accounts", [])})
    return 0


def cmd_accounts(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    rows = [
        {"persona": persona, "email": account.get("email"), "timezone": account.get("timezone")}
        for persona, account in config["accounts"].items()
    ]
    _print({"accounts": rows, "spare_accounts": config.get("spare_accounts", [])})
    return 0


def cmd_auth(args: argparse.Namespace) -> int:
    _print(authenticate(args.config, account_email=args.account))
    return 0


def cmd_seed(args: argparse.Namespace) -> int:
    _print(
        seed_persona(
            args.config,
            persona=args.persona,
            services=args.services,
            dry_run=not args.execute,
            missing_attachment_policy=args.missing_attachments,
        )
    )
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    result = verify_persona(args.config, persona=args.persona)
    _print(result)
    return 0 if result["ok"] else 2


def cmd_delta(args: argparse.Namespace) -> int:
    result = reconcile_persona(
        args.config,
        persona=args.persona,
        services=args.services,
        dry_run=not args.execute,
        missing_attachment_policy=args.missing_attachments,
    )
    _print(result)
    return 0


def cmd_reset(args: argparse.Namespace) -> int:
    _print(
        reset_persona(
            args.config,
            persona=args.persona,
            services=args.services,
            confirm_account=args.confirm_account,
            dry_run=not args.execute,
        )
    )
    return 0


def cmd_reseed(args: argparse.Namespace) -> int:
    if not args.execute:
        raise RuntimeError("reseed is destructive and requires --execute")
    reset = reset_persona(
        args.config,
        persona=args.persona,
        services=args.services,
        confirm_account=args.confirm_account,
        dry_run=False,
    )
    seed = seed_persona(
        args.config,
        persona=args.persona,
        services=args.services,
        dry_run=False,
        missing_attachment_policy=args.missing_attachments,
    )
    verify = verify_persona(args.config, persona=args.persona)
    _print({"reset": reset, "seed": seed, "verify": verify})
    return 0 if verify["ok"] else 2


def cmd_github_extract(args: argparse.Namespace) -> int:
    archive = EnvironmentArchive(args.archive)
    _print(extract_repository(archive, persona=args.persona, destination=args.destination))
    return 0


def cmd_github_push(args: argparse.Namespace) -> int:
    _print(
        push_repository(
            args.repository,
            remote_url=args.remote,
            confirm_remote=args.confirm_remote,
            dry_run=not args.execute,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="gab-seed")
    sub = parser.add_subparsers(dest="command", required=True)

    preflight = sub.add_parser("preflight", help="inspect source artifacts without credentials")
    preflight.add_argument("--archive", required=True)
    preflight.add_argument("--rubrics", required=True)
    preflight.add_argument("--accounts", required=True)
    preflight.add_argument("--output")
    preflight.set_defaults(func=cmd_preflight)

    init = sub.add_parser("init-config", help="generate a safe config; never copies passwords")
    init.add_argument("--archive", required=True)
    init.add_argument("--rubrics", required=True)
    init.add_argument("--accounts", required=True)
    init.add_argument("--client-secret", required=True)
    init.add_argument("--token-dir", required=True)
    init.add_argument("--state-dir", required=True)
    init.add_argument("--output", required=True)
    init.set_defaults(func=cmd_init_config)

    accounts = sub.add_parser("accounts", help="list configured persona/account assignments")
    accounts.add_argument("--config", required=True)
    accounts.set_defaults(func=cmd_accounts)

    auth = sub.add_parser("auth", help="perform one-time OAuth for a configured account")
    auth.add_argument("--config", required=True)
    auth.add_argument("--account", required=True)
    auth.set_defaults(func=cmd_auth)

    seed = sub.add_parser("seed", help="dry-run by default; --execute writes to Google")
    seed.add_argument("--config", required=True)
    seed.add_argument("--persona", required=True)
    seed.add_argument("--services", type=_services, default={"drive", "gmail", "calendar"})
    seed.add_argument("--missing-attachments", choices=["error", "omit"])
    seed.add_argument("--execute", action="store_true")
    seed.set_defaults(func=cmd_seed)

    verify = sub.add_parser("verify", help="read back seeded Google objects")
    verify.add_argument("--config", required=True)
    verify.add_argument("--persona", required=True)
    verify.set_defaults(func=cmd_verify)

    delta = sub.add_parser("delta", help="inspect and restore only changed Drive/Gmail/Calendar objects")
    delta.add_argument("--config", required=True)
    delta.add_argument("--persona", required=True)
    delta.add_argument("--services", type=_services, default={"drive", "gmail", "calendar"})
    delta.add_argument("--missing-attachments", choices=["error", "omit"])
    delta.add_argument("--execute", action="store_true")
    delta.set_defaults(func=cmd_delta)

    reset = sub.add_parser("reset", help="wipe configured services in a dedicated test account")
    reset.add_argument("--config", required=True)
    reset.add_argument("--persona", required=True)
    reset.add_argument("--services", type=_services, default={"drive", "gmail", "calendar"})
    reset.add_argument("--confirm-account", required=True)
    reset.add_argument("--execute", action="store_true")
    reset.set_defaults(func=cmd_reset)

    reseed = sub.add_parser("reseed", help="wipe, seed, and verify; always requires --execute")
    reseed.add_argument("--config", required=True)
    reseed.add_argument("--persona", required=True)
    reseed.add_argument("--services", type=_services, default={"drive", "gmail", "calendar"})
    reseed.add_argument("--missing-attachments", choices=["error", "omit"])
    reseed.add_argument("--confirm-account", required=True)
    reseed.add_argument("--execute", action="store_true")
    reseed.set_defaults(func=cmd_reseed)

    extract = sub.add_parser("github-extract", help="safely extract the GitHub service snapshot")
    extract.add_argument("--archive", required=True)
    extract.add_argument("--persona", default="Backend_software_engineer")
    extract.add_argument("--destination", required=True)
    extract.set_defaults(func=cmd_github_extract)

    push = sub.add_parser("github-push", help="push only local heads and tags; dry-run by default")
    push.add_argument("--repository", required=True)
    push.add_argument("--remote", required=True)
    push.add_argument("--confirm-remote", required=True)
    push.add_argument("--execute", action="store_true")
    push.set_defaults(func=cmd_github_push)
    return parser


def main(argv: list[str] | None = None) -> int:
    # Global socket timeout so a stalled Google API request fails fast (and the
    # per-op _retry backs off + retries) instead of blocking a run forever. Without
    # this, one hung response freezes the whole reset/seed (and holds a worker slot
    # in a batch) until the outer subprocess timeout. Tunable via GAB_HTTP_TIMEOUT.
    try:
        socket.setdefaulttimeout(int(os.environ.get("GAB_HTTP_TIMEOUT", "120")))
    except (TypeError, ValueError):
        socket.setdefaulttimeout(120)
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)}, indent=2), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

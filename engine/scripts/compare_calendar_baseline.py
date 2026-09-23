#!/usr/bin/env python3
"""Compare a seeded Calendar against the archive event-by-event."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from gab_seeder.archive import EnvironmentArchive  # noqa: E402
from gab_seeder.calendar_compare import compare_calendar_events  # noqa: E402
from gab_seeder.calendar_seed import list_calendar_events  # noqa: E402
from gab_seeder.config import account_for_persona, load_config, state_path  # noqa: E402
from gab_seeder.google_auth import credentials_for_account, services_for_credentials  # noqa: E402
from gab_seeder.manifest import load_manifest  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default=str(PROJECT_ROOT / "config.json"))
    parser.add_argument("--persona", required=True)
    parser.add_argument("--output")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    account = account_for_persona(config, args.persona)
    email = str(account["email"])
    archive = EnvironmentArchive(config["archive"])
    source_events = archive.load_events(args.persona)
    manifest_path = state_path(config, email, args.persona)
    manifest = load_manifest(manifest_path)
    if manifest is None:
        raise RuntimeError(f"no live manifest found at {manifest_path}")

    credentials = credentials_for_account(
        client_secret=config["client_secret"],
        token_dir=config["token_dir"],
        account_email=email,
        interactive=False,
    )
    calendar = services_for_credentials(credentials)["calendar"]
    all_remote = list_calendar_events(calendar, singleEvents=False)
    seed_tag = str(manifest["seed_tag"])
    seeded_remote = list_calendar_events(
        calendar,
        privateExtendedProperty=f"gabSeed={seed_tag}",
        singleEvents=False,
    )
    comparison = compare_calendar_events(source_events, seeded_remote)
    seeded_ids = {str(item.get("id") or "") for item in seeded_remote}
    nonseed = [
        {"google_event_id": str(item.get("id") or ""), "title": str(item.get("summary") or "")}
        for item in all_remote
        if str(item.get("id") or "") not in seeded_ids
    ]
    source_payload = json.dumps(
        source_events,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    comparison["ok"] = bool(comparison["ok"] and not nonseed and len(all_remote) == len(source_events))
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "account": email,
        "persona": args.persona,
        "archive": str(archive.path),
        "manifest": str(manifest_path),
        "manifest_completed_at": manifest.get("completed_at"),
        "seed_tag": seed_tag,
        "source_payload_bytes": len(source_payload),
        "source_payload_sha256": hashlib.sha256(source_payload).hexdigest(),
        "remote_all_events": len(all_remote),
        "remote_nonseed_events": nonseed,
        "comparison": comparison,
    }
    output = (
        Path(args.output).expanduser().resolve()
        if args.output
        else PROJECT_ROOT
        / "artifacts"
        / "verification"
        / f"{args.persona}-calendar-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    summary = {
        "ok": report["comparison"]["ok"],
        "account": email,
        "persona": args.persona,
        "output": str(output),
        "source_payload_bytes": report["source_payload_bytes"],
        "source_payload_sha256": report["source_payload_sha256"],
        "remote_all_events": report["remote_all_events"],
        "remote_nonseed_events": len(nonseed),
        **comparison["summary"],
        "source_inventory_sha256": comparison["source_inventory_sha256"],
        "final_inventory_sha256": comparison["final_inventory_sha256"],
    }
    print(json.dumps(summary, indent=2))
    return 0 if summary["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

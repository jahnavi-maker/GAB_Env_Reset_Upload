from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from materialize.auth import build_service
from materialize.bytes_util import decode_seed_bytes
from materialize.drive_sync import (
    download_file_bytes,
    find_seed_folder,
    index_folder_tree,
)
from materialize.fail import StageError, log_fail
from materialize.json_util import inspect_and_normalize

_FS_JSON_CACHE: dict[str, dict[str, Any]] = {}
_DRIVE_ATT_CACHE: dict[str, dict[str, bytes]] = {}
_DRIVE_ATT_MISS: dict[str, set[str]] = {}
_DRIVE_ATT_GUARD = threading.Lock()


def load_kind(
    path: Path | None,
    kind: str,
    log: Callable[[str], None],
    *,
    account: str = "",
    run_id: str | None = None,
    job_id: str | None = None,
    required: bool = False,
) -> dict[str, Any] | None:
    if path is None or not Path(path).exists():
        err = f"file not found: {path}"
        log_fail(
            log,
            account,
            kind,
            err,
            run_id=run_id,
            job_id=job_id,
            path=Path(path).name if path else None,
        )
        if required:
            raise StageError(kind, err)
        return None
    path = Path(path)
    log(f"Reading {kind}: {path.name} ({path.stat().st_size / 1e6:.1f} MB)")
    inspected = inspect_and_normalize(path, expected=kind)
    if not inspected["ok"]:
        err = inspected.get("error") or "invalid JSON"
        log_fail(
            log,
            account,
            kind,
            err,
            run_id=run_id,
            job_id=job_id,
            path=path.name,
        )
        if required:
            raise StageError(kind, err)
        return None
    log(f"{kind} OK — {inspected.get('count')} records")
    return inspected["data"]


def attachment_message_count(mail_data: dict[str, Any] | None) -> int:
    if not mail_data:
        return 0
    n = 0
    for item in mail_data.get("emails") or []:
        if not isinstance(item, dict):
            continue
        atts = item.get("attachments")
        if atts:
            n += 1
    return n


def gmail_attachment_block(
    mail_data: dict[str, Any] | None,
    *,
    has_filesystem: bool,
    allow: bool,
) -> str | None:
    count = attachment_message_count(mail_data)
    if count and not has_filesystem and not allow:
        return (
            f"{count} messages reference attachments; select the filesystem JSON too, "
            "or those files will be omitted from the emails"
        )
    return None


def _file_basenames(item: dict[str, Any]) -> set[str]:
    keys: set[str] = set()
    for field in ("filename", "name", "path"):
        raw = item.get(field)
        if not raw:
            continue
        text = str(raw).replace("\\", "/")
        keys.add(text)
        keys.add(text.rsplit("/", 1)[-1])
    return {k for k in keys if k}


def attachment_filenames(mail_data: dict[str, Any] | None) -> set[str]:
    wanted: set[str] = set()
    for item in (mail_data or {}).get("emails") or []:
        if not isinstance(item, dict):
            continue
        atts = item.get("attachments") or {}
        if isinstance(atts, dict):
            wanted.update(str(k) for k in atts)
    return wanted


def attachment_fs_matches(mail_data: dict[str, Any] | None, fs_data: dict[str, Any] | None) -> set[str]:
    wanted = attachment_filenames(mail_data)
    if not wanted:
        return set()
    index = file_index(fs_data or {}, wanted)
    hits: set[str] = set()
    for name in wanted:
        base = name.replace("\\", "/").rsplit("/", 1)[-1]
        if name in index or base in index:
            hits.add(base or name)
    return hits


def should_repair_gmail_attachments(
    *,
    has_fs_bytes: bool,
    already_done: bool,
    interrupted: bool,
) -> bool:
    if already_done:
        return False
    if has_fs_bytes:
        return True
    return bool(interrupted)


def attachment_email_ids(mail_data: dict[str, Any] | None) -> set[str]:
    ids: set[str] = set()
    for item in (mail_data or {}).get("emails") or []:
        if not isinstance(item, dict):
            continue
        atts = item.get("attachments")
        if atts and item.get("email_id"):
            ids.add(str(item["email_id"]))
    return ids


def fill_index_from_drive(
    drive,
    persona: str,
    wanted: set[str],
    index: dict[str, bytes],
    log: Callable[[str], None],
) -> dict[str, bytes]:
    missing = [
        name.replace("\\", "/").rsplit("/", 1)[-1]
        for name in wanted
        if name not in index and name.replace("\\", "/").rsplit("/", 1)[-1] not in index
    ]
    missing = [n for n in missing if n]
    if not missing:
        return index
    with _DRIVE_ATT_GUARD:
        cached = _DRIVE_ATT_CACHE.setdefault(persona, {})
        misses = _DRIVE_ATT_MISS.setdefault(persona, set())
        still: list[str] = []
        for base in missing:
            if base in misses:
                continue
            raw = cached.get(base)
            if raw:
                index[base] = raw
            else:
                still.append(base)
        if not still:
            return index
        root = find_seed_folder(drive, persona, log)
        if not root:
            for base in still:
                misses.add(base)
                log(f"No Drive seed file named {base} for Gmail attachment")
            return index
        log(f"Looking up {len(still)} Gmail attachments in Drive by basename")
        tree = index_folder_tree(drive, root, log)
        by_base = {
            rel.replace("\\", "/").rsplit("/", 1)[-1]: fid
            for rel, (_size, fid) in tree.items()
        }
        for base in still:
            fid = by_base.get(base)
            if not fid:
                misses.add(base)
                log(f"No filesystem/Drive file named {base} for Gmail attachment")
                continue
            try:
                raw = download_file_bytes(drive, str(fid), log)
            except Exception as exc:
                log(f"Could not download Drive attachment {base}: {exc}")
                continue
            if raw:
                cached[base] = raw
                index[base] = raw
                log(f"Gmail attachment {base} taken from Drive ({len(raw)} bytes)")
    return index


def file_index(fs_data: dict[str, Any], wanted: set[str]) -> dict[str, bytes]:
    index: dict[str, bytes] = {}
    if not wanted:
        return index
    wanted_base = {w.replace("\\", "/").rsplit("/", 1)[-1] for w in wanted}
    for item in fs_data.get("files") or []:
        if not isinstance(item, dict):
            continue
        keys = _file_basenames(item)
        if not (keys & wanted) and not (keys & wanted_base):
            continue
        try:
            raw = decode_seed_bytes(item)
        except Exception:
            continue
        if not raw:
            continue
        for key in keys:
            prev = index.get(key)
            if prev is None or len(raw) > len(prev):
                index[key] = raw
    return index


def _group_skips(log: Callable[[str], None]) -> tuple[Callable[[str], None], dict[str, list[str]]]:
    grouped: dict[str, list[str]] = defaultdict(list)

    def wrapped(message: str) -> None:
        log(message)
        if message.startswith("Skip "):
            cause = message.split(":", 1)[-1].strip() if ":" in message else message
            grouped[cause[:120]].append(message)

    return wrapped, grouped


def _fmt_range(ts: float) -> str:
    if not ts:
        return "n/a"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def run_populate(
    creds,
    *,
    calendar_json: Path | None,
    gmail_json: Path | None,
    drive_json: Path | None,
    github_dir: Path | None,
    persona: str,
    do_calendar: bool,
    do_gmail: bool,
    do_drive: bool,
    do_github: bool,
    wipe: bool,
    log: Callable[[str], None],
    target_email: str | None = None,
    rebase_gmail: bool = True,
    rebase_calendar: bool = False,
    do_github_zip: bool = False,
    run_id: str | None = None,
    job_id: str | None = None,
    retry_plan: dict[str, Any] | None = None,
    replace_gmail_attachments: bool = False,
    mode: str = "seed",
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    del rebase_gmail, rebase_calendar
    from materialize.provision.pipeline import AccountWork, provision_accounts

    log, skips = _group_skips(log)
    account = target_email or ""
    if retry_plan is not None:
        do_calendar = False
        do_gmail = bool(do_gmail and retry_plan.get("gmail"))
        do_drive = bool(do_drive and retry_plan.get("drive"))
        do_github = False
        do_github_zip = bool(do_github_zip and retry_plan.get("github"))
        log(
            "Retry skipped items only: "
            f"github={len(retry_plan.get('github') or [])} drive={len(retry_plan.get('drive') or [])} "
            f"gmail={len(retry_plan.get('gmail') or [])}"
        )
        if not (do_gmail or do_drive or do_github_zip):
            log("No skipped Calendar / Gmail / Drive / Github items for this account")
            result: dict[str, Any] = {
                "calendar": None,
                "gmail": None,
                "drive": None,
                "folder_id": None,
                "skips": {},
                "expect": {"calendar": None, "gmail": None, "drive": None},
                "services": {
                    "gmail": build_service("gmail", "v1", creds),
                    "calendar": build_service("calendar", "v3", creds),
                    "drive": build_service("drive", "v3", creds),
                },
            }
            return result

    log(
        f"Queue pipeline for {account or 'signed-in account'} "
        f"cal={do_calendar} mail={do_gmail} drive={do_drive} github={do_github or do_github_zip}"
    )
    result = provision_accounts(
        [
            AccountWork(
                email=account or "unknown@local",
                persona=persona,
                creds=creds,
                calendar_json=calendar_json,
                gmail_json=gmail_json,
                drive_json=drive_json,
                github_dir=github_dir,
                environment_id=persona,
                do_calendar=do_calendar,
                do_gmail=do_gmail,
                do_drive=do_drive,
                do_github=bool(do_github or do_github_zip),
                wipe=wipe,
                retry_plan=retry_plan,
                replace_gmail_attachments=replace_gmail_attachments,
                mode=mode,
                log=log,
                ui_job_id=job_id or "",
            )
        ],
        run_id=run_id,
        log=log,
        verify=False,
        on_progress=on_progress,
    )
    if skips:
        result.setdefault("skips", {})
        result["skips"].update({k: len(v) for k, v in skips.items()})
        log("Skip summary:")
        for cause, rows in skips.items():
            log(f"  {len(rows)} × {cause}")
    log("Done. This Google account now has the seeded Calendar / Gmail / Drive.")
    return result

from __future__ import annotations

import threading
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from materialize.auth import build_service
from materialize.bytes_util import decode_seed_bytes
from materialize.calendar_sync import (
    dedupe_primary_events,
    populate_calendar,
    wipe_seeded_events,
)
from materialize.drive_sync import (
    download_file_bytes,
    find_seed_folder,
    drive_attempt_stats,
    index_folder_tree,
    populate_drive,
    populate_drive_from_cache,
    wipe_seed_folder,
)
from materialize.fs_cache import (
    cache_file_entries,
    ensure_persona_drive_cache,
    file_index_from_cache,
)
from materialize.github_sync import upload_github_folder, wipe_my_drive_github
from materialize.gmail_sync import populate_gmail, wipe_seeded_mail
from materialize.fail import StageError, log_fail, log_warn
from materialize.identity import rewrite_identity
from materialize.json_util import inspect_and_normalize
from materialize.rebase import rebase, rebase_calendar_events
from materialize.runstate import module_slot_limit

_MODULE_SLOTS: threading.Semaphore | None = None
_MODULE_SLOTS_GUARD = threading.Lock()
_FS_JSON_CACHE: dict[str, dict[str, Any]] = {}
_DRIVE_ATT_CACHE: dict[str, dict[str, bytes]] = {}
_DRIVE_ATT_MISS: dict[str, set[str]] = {}
_DRIVE_ATT_GUARD = threading.Lock()


def _module_slots() -> threading.Semaphore:
    global _MODULE_SLOTS
    with _MODULE_SLOTS_GUARD:
        if _MODULE_SLOTS is None:
            _MODULE_SLOTS = threading.Semaphore(module_slot_limit())
        return _MODULE_SLOTS


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
) -> dict[str, Any]:
    log, skips = _group_skips(log)
    account = target_email or ""
    only_github = set(retry_plan.get("github") or []) if retry_plan else None
    only_drive = set(retry_plan.get("drive") or []) if retry_plan else None
    only_gmail = set(retry_plan.get("gmail") or []) if retry_plan else None
    if retry_plan is not None:
        do_calendar = False
        do_gmail = bool(do_gmail and only_gmail)
        do_drive = bool(do_drive and only_drive)
        do_github = False
        do_github_zip = bool(do_github_zip and only_github)
        log(
            "Retry skipped items only: "
            f"github={len(only_github or [])} drive={len(only_drive or [])} "
            f"gmail={len(only_gmail or [])}"
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
            }
            gmail = build_service("gmail", "v1", creds)
            calendar = build_service("calendar", "v3", creds)
            drive = build_service("drive", "v3", creds)
            result["services"] = {"gmail": gmail, "calendar": calendar, "drive": drive}
            return result

    def stage(name: str, fn: Callable[[], Any]) -> Any:
        try:
            return fn()
        except Exception as exc:
            log_fail(log, account, name, exc, run_id=run_id, job_id=job_id)
            raise
    gmail = build_service("gmail", "v1", creds)
    calendar = build_service("calendar", "v3", creds)
    drive = build_service("drive", "v3", creds)

    result: dict[str, Any] = {
        "calendar": None,
        "gmail": None,
        "drive": None,
        "folder_id": None,
        "skips": {},
        "expect": {"calendar": None, "gmail": None, "drive": None},
    }

    fs_data = None
    drive_cache_ready = False
    drive_json_path = Path(drive_json) if drive_json else None
    need_drive_cache = bool(do_drive or do_gmail)
    if need_drive_cache and drive_json_path and drive_json_path.exists():
        if ensure_persona_drive_cache(persona, log, source_path=drive_json_path):
            drive_cache_ready = True
        elif do_drive:
            raise StageError(
                "drive",
                f"Drive cache missing for persona {persona}. "
                "Pre-build with: python -m materialize.fs_cache --all",
            )
        if not drive_cache_ready and do_gmail:
            fs_key = str(drive_json_path.resolve())
            if fs_key and fs_key in _FS_JSON_CACHE:
                fs_data = _FS_JSON_CACHE[fs_key]
                log("filesystem JSON memory cache hit (attachments fallback)")
            else:
                fs_data = load_kind(
                    drive_json,
                    "filesystem",
                    log,
                    account=account,
                    run_id=run_id,
                    job_id=job_id,
                    required=False,
                )
                if fs_key and fs_data:
                    _FS_JSON_CACHE[fs_key] = fs_data

    if not wipe:
        log(
            "Comparing existing Calendar, Gmail, Drive and Github; "
            "unchanged items kept, different items replaced, missing items uploaded"
        )
    if do_calendar and (wipe or rebase_calendar):
        if rebase_calendar and not wipe:
            log("Calendar rebase is on — wiping seeded calendar events so dedupe keys stay unique")
        try:
            wipe_seeded_events(calendar, log)
            dedupe_primary_events(calendar, log)
        except Exception as exc:
            log_warn(log, account, "wipe", f"calendar wipe failed, continuing: {exc}", job_id=job_id)
    if wipe:
        log("Clearing previous seed from this Google account…")
        if do_gmail:
            try:
                wipe_seeded_mail(gmail, log)
            except Exception as exc:
                log_warn(log, account, "wipe", f"gmail wipe failed, continuing: {exc}", job_id=job_id)
        if do_drive:
            try:
                wipe_seed_folder(drive, persona, log)
            except Exception as exc:
                log_warn(log, account, "wipe", f"drive wipe failed, continuing: {exc}", job_id=job_id)
        if github_dir and Path(github_dir).exists():
            try:
                wipe_my_drive_github(drive, log)
            except Exception as exc:
                log_warn(log, account, "wipe", f"Github wipe failed, continuing: {exc}", job_id=job_id)

    mail_data = None
    if do_gmail or (do_calendar and gmail_json):
        mail_data = load_kind(
            gmail_json,
            "gmail",
            log,
            account=account,
            run_id=run_id,
            job_id=job_id,
            required=do_gmail,
        )

    cal_data = None
    if do_calendar:
        cal_data = load_kind(
            calendar_json,
            "calendar",
            log,
            account=account,
            run_id=run_id,
            job_id=job_id,
            required=True,
        )

    rewrite_identity(
        gmail_data=mail_data,
        calendar_data=cal_data,
        target_email=target_email or "",
        log=log,
    )

    folder_id = None

    jobs: dict[str, Callable[[], Any]] = {}
    if do_calendar and cal_data:
        events = cal_data.get("events") or []
        result["expect"]["calendar"] = len(events)
        if rebase_calendar:
            delta, old_max, new_max = rebase_calendar_events(events)
            log(
                f"Calendar rebase delta={delta:.0f}s  { _fmt_range(old_max) } → { _fmt_range(new_max) }"
            )
        jobs["calendar"] = lambda: populate_calendar(build_service("calendar", "v3", creds), cal_data, log)
    if do_gmail and mail_data:
        emails = mail_data.get("emails") or []
        att_ids = attachment_email_ids(mail_data)
        if replace_gmail_attachments:
            only_gmail = att_ids if only_gmail is None else (only_gmail & att_ids)
            result["expect"]["gmail"] = None
            log(f"Replacing attachments on {len(only_gmail or [])} emails (other messages left as-is)")
        else:
            result["expect"]["gmail"] = len(emails)
        if rebase_gmail:
            delta, old_max, new_max = rebase(emails, "timestamp")
            log(
                f"Gmail rebase delta={delta:.0f}s  { _fmt_range(old_max) } → { _fmt_range(new_max) }"
            )
        wanted = attachment_filenames(mail_data)
        if wanted and drive_cache_ready:
            att_index = file_index_from_cache(persona, wanted)
        else:
            att_index = file_index(fs_data or {}, wanted)
        if wanted and (att_index or not replace_gmail_attachments):
            if not att_index or len(att_index) < len(wanted):
                att_index = fill_index_from_drive(drive, persona, wanted, att_index, log)
        elif wanted and replace_gmail_attachments and not att_index:
            log("Skipping Drive attachment lookup — none of the filenames exist in the filesystem")
        jobs["gmail"] = lambda: populate_gmail(
            build_service("gmail", "v1", creds),
            mail_data,
            log,
            file_index=att_index,
            only_ids=only_gmail,
            replace_attachments=replace_gmail_attachments,
        )
    if do_drive and drive_cache_ready:
        cached = cache_file_entries(persona)
        max_bytes = 40 * 1024 * 1024
        result["expect"]["drive"] = sum(
            1 for e in cached if int(e.get("size") or 0) <= max_bytes
        )
        jobs["drive"] = lambda: populate_drive_from_cache(
            build_service("drive", "v3", creds),
            persona,
            log,
            only_paths=only_drive,
        )
    if jobs:
        slots = _module_slots()
        log(
            f"Seeding {', '.join(jobs)} in parallel for this account "
            f"(global Google slots={module_slot_limit()})"
        )
        errors: list[BaseException] = []

        def run_job(name: str, fn: Callable[[], Any]) -> Any:
            slots.acquire()
            try:
                return stage(name, fn)
            finally:
                slots.release()

        with ThreadPoolExecutor(max_workers=min(3, len(jobs)), thread_name_prefix="gab-mod") as pool:
            futs = {pool.submit(run_job, name, fn): name for name, fn in jobs.items()}
            for fut in as_completed(futs):
                name = futs[fut]
                try:
                    payload = fut.result()
                except BaseException as exc:
                    errors.append(exc)
                    continue
                if name == "calendar":
                    result["calendar"] = payload
                elif name == "gmail":
                    result["gmail"] = payload
                elif name == "drive":
                    folder_id = payload.get("folder_id")
                    result["drive"] = payload.get("uploaded")
                    result["folder_id"] = folder_id
                    result["expect"]["drive"] = payload.get("attempted")
                    result["drive_ineligible"] = payload.get("ineligible") or 0
        if errors:
            raise errors[0]

    if do_github or do_github_zip:
        if not github_dir or not Path(github_dir).exists():
            log("No github/ folder in this persona — skipped Drive Github folder")
        else:
            try:
                log("Uploading GitHub tree into My Drive / Github")
                upload_github_folder(
                    drive,
                    Path(github_dir),
                    None,
                    log,
                    creds=creds,
                    only_relpaths=only_github,
                )
            except Exception as exc:
                log_fail(log, account, "github_zip", exc, run_id=run_id, job_id=job_id)

    if skips:
        log("Skip summary:")
        for cause, rows in skips.items():
            log(f"  {len(rows)} × {cause}")
    result["services"] = {"gmail": gmail, "calendar": calendar, "drive": drive}
    result["skips"] = {k: len(v) for k, v in skips.items()}
    log("Done. This Google account now has the seeded Calendar / Gmail / Drive.")
    return result

from __future__ import annotations

import io
import time
from collections.abc import Callable
from typing import Any

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload
from materialize.bytes_util import decode_seed_bytes
from materialize.fail import next_for
from materialize.fs_cache import cache_file_entries, read_cached_bytes

SEED_FOLDER = "GAB_UltraEvals"
SEED_PROP = "gabSeeded"


def _retry(fn, log: Callable[[str], None], tries: int = 6):
    delay = 1.0
    for i in range(tries):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (403, 429, 500, 503) and i < tries - 1:
                log(f"Drive API {status}, retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise


def find_seed_folder(drive, persona: str, log: Callable[[str], None]) -> str | None:
    name = f"{SEED_FOLDER}__{persona}"
    resp = _retry(
        lambda: drive.files()
        .list(
            q=(
                f"name = '{name}' and mimeType = 'application/vnd.google-apps.folder' "
                "and trashed = false"
            ),
            spaces="drive",
            fields="files(id, name)",
        )
        .execute(),
        log,
    )
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def wipe_seed_folder(drive, persona: str, log: Callable[[str], None]) -> int:
    """FULL wipe: trash ALL files/folders owned by the account (no marker match).

    Loop-until-empty: trashing a folder does not flip its children's `trashed`
    flag, so we re-list `trashed = false` owned items until none remain.
    """
    trashed = 0
    while True:
        resp = _retry(
            lambda: drive.files()
            .list(
                q="'me' in owners and trashed = false",
                spaces="drive",
                fields="files(id, name)",
                pageSize=200,
            )
            .execute(),
            log,
        )
        files = resp.get("files") or []
        if not files:
            break
        progressed = False
        for f in files:
            try:
                _retry(
                    lambda fid=f["id"]: drive.files().update(fileId=fid, body={"trashed": True}).execute(),
                    log,
                )
                trashed += 1
                progressed = True
            except Exception:
                pass  # skip undeletable items so the loop can't spin forever
        log(f"Trashed {trashed} Drive items so far (full wipe)")
        if not progressed:
            break
    log(f"Full Drive wipe: trashed {trashed} items")
    return trashed


def _ensure_folder(
    drive,
    name: str,
    parent_id: str,
    cache: dict[str, str],
    log: Callable[[str], None],
) -> str:
    key = f"{parent_id}/{name}"
    if key in cache:
        return cache[key]
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    resp = _retry(
        lambda: drive.files()
        .list(
            q=(
                f"name = '{safe}' and '{parent_id}' in parents "
                "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            ),
            fields="files(id, name)",
            pageSize=5,
        )
        .execute(),
        log,
    )
    files = resp.get("files") or []
    if files:
        cache[key] = files[0]["id"]
        return files[0]["id"]
    created = _retry(
        lambda: drive.files()
        .create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id",
        )
        .execute(),
        log,
    )
    cache[key] = created["id"]
    return created["id"]


def _ensure_path(
    drive,
    rel_path: str,
    root_id: str,
    cache: dict[str, str],
    log: Callable[[str], None],
) -> str:
    current = root_id
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p]
    for part in parts:
        current = _ensure_folder(drive, part, current, cache, log)
    return current


def _rfc3339(ts: float) -> str:
    from datetime import datetime, timezone

    if ts > 1e12:
        ts = ts / 1000.0
    return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def drive_item_ineligible(item: Any, max_file_bytes: int) -> str | None:
    if not isinstance(item, dict):
        return "not a file record"
    try:
        raw = decode_seed_bytes(item)
    except Exception:
        return "undecodable"
    if len(raw) > max_file_bytes:
        return "oversize"
    return None


def drive_attempt_stats(files: list[Any], max_file_bytes: int = 40 * 1024 * 1024) -> dict[str, int]:
    ineligible = 0
    for item in files:
        if drive_item_ineligible(item, max_file_bytes):
            ineligible += 1
    total = len(files)
    return {"total": total, "ineligible": ineligible, "attempted": total - ineligible}


def ensure_seed_folder(drive, persona: str, log: Callable[[str], None]) -> str:
    name = f"{SEED_FOLDER}__{persona}"
    existing = find_seed_folder(drive, persona, log)
    if existing:
        return existing
    created = _retry(
        lambda: drive.files()
        .create(
            body={
                "name": name,
                "mimeType": "application/vnd.google-apps.folder",
                "description": "Seeded by GAB env loader. Safe to trash.",
                "properties": {SEED_PROP: "true"},
            },
            fields="id",
        )
        .execute(),
        log,
    )
    return created["id"]


def index_folder_tree(drive, root_id: str, log: Callable[[str], None]) -> dict[str, tuple[int, str]]:
    """Relative path → (size, file_id) for files already under a Drive folder."""
    out: dict[str, tuple[int, str]] = {}

    def walk(folder_id: str, prefix: str) -> None:
        page = None
        while True:
            resp = _retry(
                lambda: drive.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed = false",
                    fields="nextPageToken, files(id, name, size, mimeType)",
                    pageSize=200,
                    pageToken=page,
                )
                .execute(),
                log,
            )
            for item in resp.get("files") or []:
                name = str(item.get("name") or "")
                rel = f"{prefix}/{name}" if prefix else name
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    walk(item["id"], rel)
                else:
                    try:
                        size = int(item.get("size") or 0)
                    except (TypeError, ValueError):
                        size = 0
                    out[rel] = (size, str(item.get("id") or ""))
            page = resp.get("nextPageToken")
            if not page:
                break

    walk(root_id, "")
    return out


def find_child_file(drive, parent_id: str, name: str, log: Callable[[str], None]) -> tuple[int, str] | None:
    safe = name.replace("\\", "\\\\").replace("'", "\\'")
    resp = _retry(
        lambda: drive.files()
        .list(
            q=(
                f"name = '{safe}' and '{parent_id}' in parents "
                "and mimeType != 'application/vnd.google-apps.folder' and trashed = false"
            ),
            fields="files(id, name, size)",
            pageSize=5,
        )
        .execute(),
        log,
    )
    files = resp.get("files") or []
    if not files:
        return None
    try:
        size = int(files[0].get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    return (size, str(files[0].get("id") or ""))


def download_file_bytes(drive, file_id: str, log: Callable[[str], None]) -> bytes:
    buf = io.BytesIO()

    def pull() -> bytes:
        buf.seek(0)
        buf.truncate(0)
        req = drive.files().get_media(fileId=file_id)
        loader = MediaIoBaseDownload(buf, req)
        done = False
        while not done:
            _status, done = loader.next_chunk()
        return buf.getvalue()

    return _retry(pull, log)


def trash_file(drive, file_id: str, log: Callable[[str], None]) -> None:
    if not file_id:
        return
    _retry(
        lambda: drive.files().update(fileId=file_id, body={"trashed": True}).execute(),
        log,
    )


def populate_drive_from_cache(
    drive,
    persona: str,
    log: Callable[[str], None],
    max_file_bytes: int = 40 * 1024 * 1024,
    only_paths: set[str] | None = None,
) -> dict[str, int]:
    """Upload Drive files from persona_drive_cache (no filesystem JSON parse)."""
    entries = cache_file_entries(persona)
    if not entries:
        log(f"No drive cache entries for {persona}; run materialize or push will build cache")
        return {"uploaded": 0, "skipped": 0, "folder_id": None, "attempted": 0, "ineligible": 0}
    ineligible = sum(
        1 for e in entries if int(e.get("size") or 0) > max_file_bytes
    )
    stats = {
        "total": len(entries),
        "ineligible": ineligible,
        "attempted": len(entries) - ineligible,
    }
    name = f"{SEED_FOLDER}__{persona}"
    root_id = ensure_seed_folder(drive, persona, log)
    log(f"Using Drive folder {name} (upload from local persona cache)")
    cache: dict[str, str] = {}
    uploaded = skipped = 0
    if only_paths is not None:
        wanted = {p.replace("\\", "/").lstrip("/") for p in only_paths}
        entries = [e for e in entries if str(e.get("rel") or "") in wanted]
        log(f"Retrying {len(entries)} skipped Drive files only (cache)")
        existing: dict[str, tuple[int, str]] = {}
    else:
        existing = index_folder_tree(drive, root_id, log)
        log(
            f"Drive already has {len(existing)} files; same path+size kept, "
            "different size replaced, missing uploaded (cache)"
        )
    for i, entry in enumerate(entries, 1):
        rel = str(entry.get("rel") or "")
        try:
            size = int(entry.get("size") or 0)
            if size > max_file_bytes:
                log(f"Skip large file {rel} ({size} bytes). next={next_for('drive', 'large file')}")
                skipped += 1
                continue
            raw = read_cached_bytes(persona, rel)
            path = rel.replace("\\", "/")
            parent_rel = "/".join(path.split("/")[:-1])
            filename = str(entry.get("filename") or path.split("/")[-1])[:200]
            parent_id = (
                _ensure_path(drive, parent_rel, root_id, cache, log) if parent_rel else root_id
            )
            have = existing.get(path.lstrip("/"))
            if have is None and only_paths is not None:
                found = find_child_file(drive, parent_id, filename, log)
                if found:
                    have = found
                    existing[path.lstrip("/")] = found
            if have is not None:
                have_size, have_id = have
                if have_size == len(raw):
                    skipped += 1
                    continue
                log(f"Drive {rel} size {have_size} ≠ seed {len(raw)}; replacing that file only")
                trash_file(drive, have_id, log)
            meta: dict[str, Any] = {
                "name": filename,
                "parents": [parent_id],
                "properties": {SEED_PROP: "true"},
            }
            modified = entry.get("modified")
            if modified is not None:
                try:
                    stamp = _rfc3339(float(modified))
                    meta["modifiedTime"] = stamp
                    meta["createdTime"] = stamp
                except (TypeError, ValueError):
                    pass
            if raw:
                media = MediaIoBaseUpload(
                    io.BytesIO(raw),
                    mimetype=entry.get("mime_type") or "application/octet-stream",
                    resumable=len(raw) > 5 * 1024 * 1024,
                )
                _retry(
                    lambda m=meta, med=media: drive.files()
                    .create(body=m, media_body=med, fields="id")
                    .execute(),
                    log,
                )
            else:
                _retry(
                    lambda m=meta: drive.files().create(body=m, fields="id").execute(),
                    log,
                )
            uploaded += 1
            existing[path.lstrip("/")] = (len(raw), "")
        except Exception as exc:
            skipped += 1
            log(f"Skip Drive file {i} ({rel}): {exc}. next={next_for('drive', str(exc))}")
        if i % 15 == 0 or i == len(entries):
            log(f"Drive files {i}/{len(entries)} (uploaded {uploaded}, skipped {skipped})")
    log(f"Drive done: uploaded {uploaded}, skipped {skipped}")
    return {
        "uploaded": uploaded,
        "skipped": skipped,
        "folder_id": root_id,
        "attempted": stats["attempted"],
        "ineligible": stats["ineligible"],
    }


def populate_drive(
    drive,
    data: dict[str, Any],
    persona: str,
    log: Callable[[str], None],
    max_file_bytes: int = 40 * 1024 * 1024,
    only_paths: set[str] | None = None,
) -> dict[str, int]:
    name = f"{SEED_FOLDER}__{persona}"
    root_id = ensure_seed_folder(drive, persona, log)
    log(f"Using Drive folder {name}")
    cache: dict[str, str] = {}
    uploaded = skipped = 0
    files = data.get("files") or []
    if only_paths is not None:
        wanted = {p.replace("\\", "/").lstrip("/") for p in only_paths}
        files = [
            item
            for item in files
            if isinstance(item, dict)
            and str(item.get("path") or item.get("filename") or "").replace("\\", "/").lstrip("/") in wanted
        ]
        log(f"Retrying {len(files)} skipped Drive files only (no full folder scan)")
        existing: dict[str, tuple[int, str]] = {}
    else:
        existing = index_folder_tree(drive, root_id, log)
        log(f"Drive already has {len(existing)} files; same path+size kept, different size replaced, missing uploaded")
    stats = drive_attempt_stats(files, max_file_bytes)
    for i, item in enumerate(files, 1):
        if not isinstance(item, dict):
            skipped += 1
            continue
        try:
            raw = decode_seed_bytes(item)
            if len(raw) > max_file_bytes:
                log(f"Skip large file {item.get('path')} ({len(raw)} bytes). next={next_for('drive', 'large file')}")
                skipped += 1
                continue
            path = str(item.get("path") or item.get("filename") or f"file-{i}").replace("\\", "/")
            parent_rel = "/".join(path.split("/")[:-1])
            filename = str(item.get("filename") or item.get("name") or path.split("/")[-1])[:200]
            rel = path.lstrip("/")
            parent_id = (
                _ensure_path(drive, parent_rel, root_id, cache, log) if parent_rel else root_id
            )
            have = existing.get(rel)
            if have is None and only_paths is not None:
                found = find_child_file(drive, parent_id, filename, log)
                if found:
                    have = found
                    existing[rel] = found
            if have is not None:
                have_size, have_id = have
                if have_size == len(raw):
                    skipped += 1
                    continue
                log(f"Drive {rel} size {have_size} ≠ seed {len(raw)}; replacing that file only")
                trash_file(drive, have_id, log)
            meta: dict[str, Any] = {
                "name": filename,
                "parents": [parent_id],
                "properties": {SEED_PROP: "true"},
            }
            modified = item.get("modified") or item.get("mtime") or item.get("created")
            try:
                if modified is not None:
                    stamp = _rfc3339(float(modified))
                    meta["modifiedTime"] = stamp
                    meta["createdTime"] = stamp
            except (TypeError, ValueError):
                pass
            if raw:
                media = MediaIoBaseUpload(
                    io.BytesIO(raw),
                    mimetype=item.get("mime_type") or "application/octet-stream",
                    resumable=len(raw) > 5 * 1024 * 1024,
                )
                _retry(
                    lambda: drive.files()
                    .create(body=meta, media_body=media, fields="id")
                    .execute(),
                    log,
                )
            else:
                _retry(
                    lambda: drive.files().create(body=meta, fields="id").execute(),
                    log,
                )
            uploaded += 1
            existing[rel] = (len(raw), "")
        except Exception as exc:
            skipped += 1
            log(f"Skip Drive file {i} ({item.get('path')}): {exc}. next={next_for('drive', str(exc))}")
        if i % 15 == 0 or i == len(files):
            log(f"Drive files {i}/{len(files)} (uploaded {uploaded}, skipped {skipped})")
    log(f"Drive done: uploaded {uploaded}, skipped {skipped}")
    return {
        "uploaded": uploaded,
        "skipped": skipped,
        "folder_id": root_id,
        "attempted": stats["attempted"],
        "ineligible": stats["ineligible"],
    }

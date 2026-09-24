from __future__ import annotations

import io
import mimetypes
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from googleapiclient.http import MediaIoBaseUpload

from materialize.auth import build_service
from materialize.drive_sync import (
    _ensure_folder,
    _ensure_path,
    _retry,
    find_child_file,
    index_folder_tree,
    trash_file,
)
from materialize.runstate import github_upload_slots

GITHUB_FOLDER = "Github"
MY_DRIVE_ROOT = "root"
MAX_FILE_BYTES = 40 * 1024 * 1024

_GH_SLOTS: threading.Semaphore | None = None
_GH_SLOTS_GUARD = threading.Lock()


def _github_slots() -> threading.Semaphore:
    global _GH_SLOTS
    with _GH_SLOTS_GUARD:
        if _GH_SLOTS is None:
            _GH_SLOTS = threading.Semaphore(github_upload_slots())
        return _GH_SLOTS


def find_my_drive_github(drive, log: Callable[[str], None]) -> str | None:
    resp = _retry(
        lambda: drive.files()
        .list(
            q=(
                f"name = '{GITHUB_FOLDER}' and '{MY_DRIVE_ROOT}' in parents "
                "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
            ),
            fields="files(id, name)",
            pageSize=5,
        )
        .execute(),
        log,
    )
    files = resp.get("files") or []
    return files[0]["id"] if files else None


def wipe_my_drive_github(drive, log: Callable[[str], None]) -> int:
    folder_id = find_my_drive_github(drive, log)
    if not folder_id:
        log("No previously seeded My Drive Github folder found")
        return 0
    _retry(
        lambda: drive.files().update(fileId=folder_id, body={"trashed": True}).execute(),
        log,
    )
    log("Moved previous My Drive Github folder to trash")
    return 1


def _wipe_all_github_folders(drive, log: Callable[[str], None], max_rounds: int = 8) -> int:
    """Trash EVERY root-level 'Github' folder, retrying until two consecutive
    empty listings. Drive's files.list is eventually-consistent and can return
    an empty page while a folder still exists; a single find-and-trash therefore
    left stale folders behind, and the next upload stacked a duplicate. Looping
    with a confirm makes the wipe reliable so a full push always starts clean.
    """
    trashed = 0
    empties = 0
    for _ in range(max_rounds):
        resp = _retry(
            lambda: drive.files()
            .list(
                q=(
                    f"name = '{GITHUB_FOLDER}' and '{MY_DRIVE_ROOT}' in parents "
                    "and mimeType = 'application/vnd.google-apps.folder' and trashed = false"
                ),
                fields="files(id)",
                pageSize=100,
            )
            .execute(),
            log,
        )
        ids = [f["id"] for f in (resp.get("files") or [])]
        if not ids:
            empties += 1
            if empties >= 2:
                break
            time.sleep(2)
            continue
        empties = 0
        for fid in ids:
            _retry(
                lambda i=fid: drive.files().update(fileId=i, body={"trashed": True}).execute(),
                log,
            )
            trashed += 1
        time.sleep(2)
    if trashed:
        log(f"Wiped {trashed} existing Github folder(s) before fresh upload")
    return trashed


def iter_github_files(github_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(github_dir.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(github_dir)
        if ".git" in rel.parts:
            continue
        files.append(path)
    return files


def _upload_one(creds, parent_id: str, path: Path, log: Callable[[str], None]) -> str:
    raw = path.read_bytes()
    if len(raw) > MAX_FILE_BYTES:
        return "oversize"
    mime, _ = mimetypes.guess_type(path.name)
    name = path.name[:200]
    slots = _github_slots()
    slots.acquire()
    try:
        svc = build_service("drive", "v3", creds)
        if not raw:
            _retry(
                lambda: svc.files()
                .create(body={"name": name, "parents": [parent_id]}, fields="id")
                .execute(),
                log,
            )
        else:
            media = MediaIoBaseUpload(
                io.BytesIO(raw),
                mimetype=mime or "application/octet-stream",
                resumable=len(raw) > 5 * 1024 * 1024,
            )
            _retry(
                lambda: svc.files()
                .create(
                    body={"name": name, "parents": [parent_id]},
                    media_body=media,
                    fields="id",
                )
                .execute(),
                log,
            )
    finally:
        slots.release()
    return "ok"


def upload_github_folder(
    drive,
    github_dir: Path,
    parent_folder_id: str | None,
    log: Callable[[str], None],
    creds=None,
    only_relpaths: set[str] | None = None,
) -> str:
    del parent_folder_id
    cache: dict[str, str] = {}
    files = iter_github_files(github_dir)
    if only_relpaths is not None:
        # Recovery / top-up: reuse the existing folder and upload ONLY the files
        # that were skipped last time. find_child_file (below) checks each one,
        # so no full-tree scan and no wipe.
        root_id = _ensure_folder(drive, GITHUB_FOLDER, MY_DRIVE_ROOT, cache, log)
        wanted = {p.replace("\\", "/") for p in only_relpaths}
        files = [p for p in files if str(p.relative_to(github_dir)).replace("\\", "/") in wanted]
        log(f"Retrying {len(files)} skipped GitHub files only (no full folder scan)")
    else:
        # Full push: ALWAYS wipe every existing Github folder first, then upload
        # fresh. Because the folder is empty afterwards there is nothing to skip
        # and no index_folder_tree scan — that scan is what duplicated files (on a
        # stale-empty wipe read) and hung on partial trees. Clean every time.
        _wipe_all_github_folders(drive, log)
        cache.clear()
        root_id = _ensure_folder(drive, GITHUB_FOLDER, MY_DRIVE_ROOT, cache, log)
        log(f"Uploading {len(files)} GitHub files into My Drive / {GITHUB_FOLDER} (fresh, parallel)")
    folder_ids = {"": root_id}
    parents = sorted(
        {"/".join(p.relative_to(github_dir).parts[:-1]) for p in files if len(p.relative_to(github_dir).parts) > 1},
        key=lambda rel: (rel.count("/"), rel),
    )
    for parent_rel in parents:
        folder_ids[parent_rel] = _ensure_path(drive, parent_rel, root_id, cache, log)
    # Fresh folder on a full push -> nothing exists to skip. Only the recovery
    # path consults per-file existence (find_child_file inside work()).
    existing: dict[str, tuple[int, str] | int] = {}
    if creds is None:
        creds = getattr(getattr(drive, "_http", None), "credentials", None)
    if creds is None:
        raise RuntimeError("No Google credentials available to upload Github")
    uploaded = skipped = 0
    lock = threading.Lock()
    workers = min(2 if only_relpaths is not None else 8, max(1, github_upload_slots()))

    def work(path: Path) -> None:
        nonlocal uploaded, skipped
        rel = path.relative_to(github_dir)
        rel_s = str(rel).replace("\\", "/")
        have = existing.get(rel_s)
        seed_size = path.stat().st_size
        parent_rel = "/".join(rel.parts[:-1])
        parent_id = folder_ids.get(parent_rel, root_id)
        probe = build_service("drive", "v3", creds) if creds is not None else drive
        if have is None and only_relpaths is not None:
            found = find_child_file(probe, parent_id, path.name[:200], log)
            if found:
                have = found
                with lock:
                    existing[rel_s] = found
        if have is not None:
            have_size, have_id = have if isinstance(have, tuple) else (have, "")
            if have_size == seed_size:
                with lock:
                    skipped += 1
                    done = uploaded + skipped
                    if done % 100 == 0 or done == len(files):
                        log(f"GitHub Drive files {done}/{len(files)} (uploaded {uploaded}, skipped {skipped})")
                return
            if have_id:
                try:
                    trash_file(probe, str(have_id), log)
                except Exception as exc:
                    log(f"Could not replace GitHub file {rel}: {exc}")
        try:
            status = _upload_one(creds, parent_id, path, log)
            if status == "ok":
                with lock:
                    existing[rel_s] = (seed_size, "")
        except Exception as exc:
            with lock:
                skipped += 1
            log(f"Skip GitHub file {rel}: {exc}")
            return
        with lock:
            if status == "ok":
                uploaded += 1
            else:
                skipped += 1
            done = uploaded + skipped
            if done % 100 == 0 or done == len(files):
                log(f"GitHub Drive files {done}/{len(files)} (uploaded {uploaded}, skipped {skipped})")

    if not files:
        log(f"GitHub folder done: uploaded 0, skipped 0")
        return root_id
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gab-gh") as pool:
        futs = [pool.submit(work, path) for path in files]
        for fut in as_completed(futs):
            fut.result()
    log(f"GitHub folder done: uploaded {uploaded}, skipped {skipped}")
    return root_id


# Older name used by tests / docs that still mention zip.
def upload_github_zip(drive, github_dir: Path, parent_folder_id: str | None, log: Callable[[str], None], creds=None) -> str:
    return upload_github_folder(drive, github_dir, parent_folder_id, log, creds=creds)

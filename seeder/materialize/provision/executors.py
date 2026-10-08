from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from materialize.auth import build_service
from materialize.calendar_sync import (
    event_needs_update,
    insert_event,
    match_seeded_event,
    seeded_event_index,
    update_event,
    wipe_seeded_events,
)
from materialize.drive_sync import (
    SEED_FOLDER,
    ensure_child_folder,
    list_owned_files_index,
    patch_file_metadata,
    trash_file,
    update_file_media,
    upload_bytes,
    wipe_seed_folder,
)
from materialize.fs_cache import ensure_persona_drive_cache, file_index_from_cache, read_cached_bytes
from materialize.gmail_sync import (
    _normalize_msgid,
    ensure_label,
    insert_one_message,
    list_seeded_mail,
    trash_seeded_message,
    wipe_seeded_mail,
)
from materialize.provision.route import DELTA
from materialize.provision.env_builder import EnvironmentBuilder
from materialize.provision.store import Job, JobStore

_THREAD = threading.local()


def service_for(email: str, name: str, version: str, creds) -> Any:
    cache = getattr(_THREAD, "services", None)
    if cache is None:
        cache = {}
        _THREAD.services = cache
    key = (email, name, version, id(creds))
    svc = cache.get(key)
    if svc is None:
        svc = build_service(name, version, creds)
        cache[key] = svc
    return svc


def parent_folder_id(store: JobStore, job: Job, payload: dict[str, Any]) -> str:
    parent_sid = payload.get("parent_sid")
    if parent_sid:
        found = store.google_id(job.account_id, "drive", parent_sid)
        if found:
            return found
    parent = payload.get("parent")
    if parent == "root" or not parent:
        return "root"
    return str(parent)


class JobExecutor:
    def __init__(
        self,
        *,
        creds_for: Callable[[str], Any],
        builder: EnvironmentBuilder,
        store: JobStore,
        log: Callable[[str], None],
        attachment_index: Callable[[str], dict[str, bytes]],
        limiters: Any = None,
    ):
        self.creds_for = creds_for
        self.builder = builder
        self.store = store
        self.log = log
        self.attachment_index = attachment_index
        self.limiters = limiters  # pipeline ServiceLimiters -> rate-controls the parallel wipe
        self._label_cache: dict[str, str] = {}
        self._label_lock = threading.Lock()
        self._folder_cache: dict[str, dict[str, str]] = {}
        self._cal_index: dict[str, tuple[dict, dict]] = {}
        self._mail_index: dict[str, dict[str, dict[str, str]]] = {}
        # (account, parent_id) -> {name: (size, id, md5)} so a full reconcile lists each
        # Drive folder once instead of one API call per file.
        # One bulk Drive listing per account, reused by every drive delta job:
        #   _owned_by_id: {file_id: meta}         -> identify a baseline file by its manifest id
        #                                            even after an agent rename/move
        #   _owned_by_loc: {(parent_id, name): meta} -> name/location lookup for the restore path
        self._owned_by_id: dict[str, dict[str, dict]] = {}
        self._owned_by_loc: dict[str, dict[tuple[str, str], dict]] = {}
        self._owned_lock = threading.Lock()

    def _drive_owned(self, drive, account_id: str) -> tuple[dict[str, dict], dict[tuple[str, str], dict]]:
        """Lazily build (and cache) the account's owned-file indexes: by id and by (parent,name)."""
        with self._owned_lock:
            by_id = self._owned_by_id.get(account_id)
            by_loc = self._owned_by_loc.get(account_id)
        if by_id is None:
            by_id = list_owned_files_index(drive, self.log)
            by_loc = {}
            for fid, meta in by_id.items():
                if meta.get("is_folder"):
                    continue  # by_loc is for file restore lookups; folders handled by id/name
                for par in meta.get("parents") or ():
                    by_loc[(str(par), meta["name"])] = {**meta, "id": fid}
            with self._owned_lock:
                self._owned_by_id[account_id] = by_id
                self._owned_by_loc[account_id] = by_loc
        return by_id, by_loc

    def execute(self, job: Job) -> dict[str, Any]:
        if job.service == "generate":
            return self._generate(job)
        creds = self.creds_for(job.account_id)
        if job.service == "drive":
            return self._drive(job, creds)
        if job.service == "calendar":
            return self._calendar(job, creds)
        if job.service == "gmail":
            return self._gmail(job, creds)
        raise RuntimeError(f"unknown service {job.service}")

    def _generate(self, job: Job) -> dict[str, Any]:
        persona = job.payload.get("persona") or job.persona_id
        src = Path(job.payload.get("drive_json") or job.source_path)
        root = ensure_persona_drive_cache(persona, self.log, source_path=src if src.exists() else None)
        if root is None:
            raise RuntimeError(f"environment materialize failed for {persona}")
        return {"id": str(root), "path": str(root)}

    def _drive(self, job: Job, creds) -> dict[str, Any]:
        drive = service_for(job.account_id, "drive", "v3", creds)
        if job.action == "wipe":
            # Parallel wipe (per-thread Drive services) instead of one-by-one — the single-
            # threaded wipe of a ~9000-file account was the dominant cost of a reseed. The
            # limiter rate-controls the deletes so the parallelism can't cause a 429 storm.
            n = wipe_seed_folder(
                drive, job.environment_id, self.log,
                creds=creds, workers=10, limiter=self.limiters, account_id=job.account_id,
            )
            return {"id": "wiped", "trashed": n}
        if job.action == "create_folder":
            parent = parent_folder_id(self.store, job, job.payload)
            name = str(job.payload.get("name") or job.source_path or "folder")
            if (job.extra or {}).get("mode") == DELTA:
                # If the seeded folder still exists by its manifest id, repair an agent
                # rename/move in place (keeps the id, so child files stay linked) instead of
                # creating a duplicate empty folder.
                by_id, _ = self._drive_owned(drive, job.account_id)
                gid = str(job.google_object_id or "")
                live = by_id.get(gid) if gid else None
                if live and live.get("is_folder"):
                    if live["name"] != name or parent not in (live.get("parents") or ()):
                        remove = [p for p in (live.get("parents") or ()) if p != parent] or None
                        patch_file_metadata(
                            drive, gid, self.log,
                            name=name if live["name"] != name else None,
                            add_parent=parent if parent not in (live.get("parents") or ()) else None,
                            remove_parents=remove,
                        )
                    return {"id": gid}
            cache = self._folder_cache.setdefault(job.account_id, {})
            folder_id = ensure_child_folder(drive, name, parent, self.log, cache)
            return {"id": folder_id}
        if job.action == "upload":
            parent = parent_folder_id(self.store, job, job.payload)
            name = str(job.payload.get("filename") or Path(job.source_path).name)
            mime = str(job.payload.get("mime") or "application/octet-stream")
            if job.source_type == "github":
                raw = Path(job.payload.get("abs") or job.source_path).read_bytes()
            else:
                raw = read_cached_bytes(job.environment_id, job.payload.get("rel") or job.source_path)
            if (job.extra or {}).get("mode") == DELTA:
                by_id, by_loc = self._drive_owned(drive, job.account_id)
                want_md5 = hashlib.md5(raw).hexdigest()
                gid = str(job.google_object_id or "")
                live = by_id.get(gid) if gid else None
                if live:
                    # The seeded file still exists (by its manifest id) — the agent may have
                    # RENAMED, MOVED, or edited its CONTENT. Repair each in place (same id).
                    changed = False
                    if live["name"] != name or parent not in (live.get("parents") or ()):
                        remove = [p for p in (live.get("parents") or ()) if p != parent] or None
                        patch_file_metadata(
                            drive, gid, self.log,
                            name=name if live["name"] != name else None,
                            add_parent=parent if parent not in (live.get("parents") or ()) else None,
                            remove_parents=remove,
                        )
                        changed = True
                    if live.get("md5"):
                        # Binary seed (always has an md5): compare content, overwrite on drift.
                        if live["md5"] != want_md5:
                            update_file_media(drive, gid, raw, mime, self.log)
                            changed = True
                    else:
                        # No md5 => the file is now a Google-native/converted type, so its bytes
                        # can't be verified. Seeds are uploaded binaries, so a md5-less live file
                        # means the content drifted (e.g. converted to a Doc). Restore the seeded
                        # bytes in place; if Drive refuses an in-place media update on a native
                        # file, fall back to trash + re-upload (persist_success records the new id).
                        try:
                            update_file_media(drive, gid, raw, mime, self.log)
                            changed = True
                        except Exception as exc:  # noqa: BLE001
                            self.log(f"native-file restore in place failed for {gid} ({name}): "
                                     f"{exc}; re-uploading")
                            try:
                                trash_file(drive, gid, self.log)
                            except Exception as exc2:  # noqa: BLE001
                                self.log(f"could not trash drifted native file {gid}: {exc2}")
                            new_id = upload_bytes(drive, parent, name, raw, mime, self.log)
                            return {"id": new_id, "bytes": len(raw), "updated": True}
                    return {"id": gid, "bytes": len(raw), "updated": changed, "skipped": not changed}
                # Not found by id -> the agent DELETED it (or it predates id capture). Avoid a
                # duplicate: reuse a correct same-name file in the target folder if one exists,
                # else re-upload to restore it.
                loc = by_loc.get((parent, name))
                if loc:
                    live_md5 = loc.get("md5") or ""
                    if loc.get("size") == len(raw) and (live_md5 == want_md5 if live_md5 else True):
                        return {"id": loc["id"], "bytes": len(raw), "skipped": True}
                    try:
                        trash_file(drive, loc["id"], self.log)
                    except Exception as exc:
                        self.log(f"Could not replace Drive {name}: {exc}")
            file_id = upload_bytes(drive, parent, name, raw, mime, self.log)
            return {"id": file_id, "bytes": len(raw)}
        raise RuntimeError(f"unknown drive action {job.action}")

    def _calendar(self, job: Job, creds) -> dict[str, Any]:
        calendar = service_for(job.account_id, "calendar", "v3", creds)
        if job.action == "wipe":
            n = wipe_seeded_events(calendar, self.log)
            return {"id": "wiped", "deleted": n}
        if job.action == "insert_event":
            if (job.extra or {}).get("mode") == DELTA:
                index = self._cal_index.get(job.account_id)
                if index is None:
                    index = seeded_event_index(calendar, self.log)
                    self._cal_index[job.account_id] = index
                by_key, by_gab = index
                match = match_seeded_event(job.payload.get("item") or {"event_id": job.payload.get("event_id")}, by_key, by_gab)
                if match and match.get("id"):
                    # The event still exists — but the agent may have changed its time/title/
                    # description/location. Overwrite it back to the seeded body on drift.
                    body = job.payload["body"]
                    if event_needs_update(body, match):
                        try:
                            update_event(calendar, str(match["id"]), body, self.log)
                            return {"id": str(match["id"]), "updated": True}
                        except Exception as exc:
                            self.log(f"Could not reset drifted event {match['id']}: {exc}")
                    return {"id": str(match["id"]), "skipped": True}
            event_id = insert_event(calendar, job.payload["body"], self.log)
            return {"id": event_id}
        raise RuntimeError(f"unknown calendar action {job.action}")

    def _gmail(self, job: Job, creds) -> dict[str, Any]:
        gmail = service_for(job.account_id, "gmail", "v1", creds)
        if job.action == "wipe":
            n = wipe_seeded_mail(gmail, self.log)
            return {"id": "wiped", "deleted": n}
        if job.action == "ensure_label":
            label_id = ensure_label(gmail, self.log)
            with self._label_lock:
                self._label_cache[job.account_id] = label_id
            return {"id": label_id}
        if job.action == "insert_message":
            with self._label_lock:
                label_id = self._label_cache.get(job.account_id)
            if not label_id:
                label_id = self.store.google_id(job.account_id, "gmail", "gmail/label")
            if not label_id:
                label_id = ensure_label(gmail, self.log)
            parent_id = str(job.payload.get("parent_id") or "")
            thread_id = None
            if parent_id:
                extra = self.store.extra(job.account_id, "gmail", f"mail/{parent_id}")
                thread_id = extra.get("threadId")
            item = job.payload.get("item") or {}
            if (job.extra or {}).get("mode") == DELTA and not job.extra.get("replace_attachments"):
                already = self._mail_index.get(job.account_id)
                if already is None:
                    already = list_seeded_mail(
                        gmail, label_id, self.log,
                        refresh_label=lambda: ensure_label(gmail, self.log),
                    )
                    self._mail_index[job.account_id] = already
                eid = str(item.get("email_id") or "")
                hit = already.get(eid) or already.get(_normalize_msgid(eid))
                if hit:
                    return {"id": hit["id"], "threadId": hit.get("threadId") or hit["id"], "skipped": True}
            if job.extra.get("replace_attachments") and item.get("email_id"):
                existing = self.store.google_id(job.account_id, "gmail", job.synthetic_id)
                if existing:
                    try:
                        trash_seeded_message(gmail, existing, self.log)
                    except Exception as exc:
                        self.log(f"Could not trash email {item.get('email_id')}: {exc}")
            atts = item.get("attachments") or {}
            wanted = {str(k) for k in atts} if isinstance(atts, dict) else set()
            index = dict(self.attachment_index(job.environment_id) or {})
            if wanted:
                index.update(file_index_from_cache(job.environment_id, wanted))
            result = insert_one_message(
                gmail,
                item,
                index,
                self.log,
                label_id=label_id,
                thread_id=thread_id,
            )
            return {"id": result["id"], "threadId": result["threadId"]}
        raise RuntimeError(f"unknown gmail action {job.action}")


def checksum_of(job: Job, builder: EnvironmentBuilder) -> str | None:
    try:
        if job.service != "drive" or job.action != "upload":
            return None
        if job.source_type == "github":
            raw = Path(job.payload.get("abs") or job.source_path).read_bytes()
        else:
            raw = read_cached_bytes(job.environment_id, job.payload.get("rel") or job.source_path)
        # md5, to MATCH Drive's md5Checksum — lets reconcile detect content drift by comparing
        # the stored checksum to the live md5 with NO byte re-read (the bulk-diff fast path).
        return hashlib.md5(raw).hexdigest()
    except Exception:
        return None

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
    _retry as _drive_retry,
    ensure_child_folder,
    find_child_file,
    trash_file,
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


def _list_children_index(drive, parent_id: str, log) -> dict[str, tuple[int, str, str]]:
    """{name: (size, id, md5Checksum)} for every non-folder child of a Drive folder, in one
    paginated listing. md5 lets a reconcile detect same-size content edits without per-file gets."""
    out: dict[str, tuple[int, str, str]] = {}
    safe = str(parent_id).replace("\\", "\\\\").replace("'", "\\'")
    tok = None
    while True:
        resp = _drive_retry(
            lambda t=tok: drive.files()
            .list(
                q=f"'{safe}' in parents and mimeType != 'application/vnd.google-apps.folder' and trashed = false",
                fields="nextPageToken, files(id, name, size, md5Checksum)",
                pageSize=1000,
                pageToken=t,
            )
            .execute(),
            log,
        )
        for f in resp.get("files", []) or []:
            try:
                sz = int(f.get("size") or 0)
            except (TypeError, ValueError):
                sz = 0
            out[str(f.get("name") or "")] = (sz, str(f.get("id") or ""), str(f.get("md5Checksum") or ""))
        tok = resp.get("nextPageToken")
        if not tok:
            break
    return out


class JobExecutor:
    def __init__(
        self,
        *,
        creds_for: Callable[[str], Any],
        builder: EnvironmentBuilder,
        store: JobStore,
        log: Callable[[str], None],
        attachment_index: Callable[[str], dict[str, bytes]],
    ):
        self.creds_for = creds_for
        self.builder = builder
        self.store = store
        self.log = log
        self.attachment_index = attachment_index
        self._label_cache: dict[str, str] = {}
        self._label_lock = threading.Lock()
        self._folder_cache: dict[str, dict[str, str]] = {}
        self._cal_index: dict[str, tuple[dict, dict]] = {}
        self._mail_index: dict[str, dict[str, dict[str, str]]] = {}
        # (account, parent_id) -> {name: (size, id, md5)} so a full reconcile lists each
        # Drive folder once instead of one API call per file.
        self._child_index: dict[tuple[str, str], dict[str, tuple[int, str, str]]] = {}
        self._child_lock = threading.Lock()

    def _child_entry(self, drive, account_id: str, parent_id: str, name: str):
        """(size, id, md5) of a child file by name, from a per-parent cached listing."""
        key = (account_id, parent_id)
        with self._child_lock:
            idx = self._child_index.get(key)
        if idx is None:
            idx = _list_children_index(drive, parent_id, self.log)
            with self._child_lock:
                self._child_index[key] = idx
        return idx.get(name)

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
            n = wipe_seed_folder(drive, job.environment_id, self.log)
            return {"id": "wiped", "trashed": n}
        if job.action == "create_folder":
            parent = parent_folder_id(self.store, job, job.payload)
            name = str(job.payload.get("name") or job.source_path or "folder")
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
                have = self._child_entry(drive, job.account_id, parent, name)
                if have:
                    # Content drift by md5 (same-size edits too), not just size. A native file
                    # with no md5 falls back to size. Matches -> keep; else replace in place.
                    live_size, live_id, live_md5 = have[0], have[1], have[2]
                    want_md5 = hashlib.md5(raw).hexdigest()
                    content_ok = live_size == len(raw) and (
                        live_md5 == want_md5 if live_md5 else True
                    )
                    if content_ok:
                        return {"id": live_id, "bytes": len(raw), "skipped": True}
                    if live_id:
                        try:
                            trash_file(drive, live_id, self.log)
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
                    already = list_seeded_mail(gmail, label_id, self.log)
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
        return hashlib.sha256(raw).hexdigest()
    except Exception:
        return None

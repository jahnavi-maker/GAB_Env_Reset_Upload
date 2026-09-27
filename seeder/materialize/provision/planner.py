from __future__ import annotations

import copy
import hashlib
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

from materialize.calendar_sync import build_event_body
from materialize.identity import rewrite_identity
from materialize.json_util import inspect_and_normalize
from materialize.provision.env_builder import EnvironmentArtifacts, EnvironmentBuilder
from materialize.provision.store import PERMANENT_FAILURE, Job, JobStore
from materialize.runner import attachment_filenames

_SAFE = re.compile(r"[^a-zA-Z0-9._-]+")


def _sid(*parts: str) -> str:
    raw = "/".join(str(p).replace("\\", "/").strip("/") for p in parts if p is not None)
    return raw[:240] or "item"


def _folder_parents(rel: str) -> list[str]:
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    out: list[str] = []
    acc = ""
    for part in parts:
        acc = f"{acc}/{part}" if acc else part
        out.append(acc)
    return out


def _copy_data(data: dict[str, Any] | None) -> dict[str, Any] | None:
    return copy.deepcopy(data) if data else None


def _github_rel(path: Path, root: Path) -> str:
    return str(path.relative_to(root)).replace("\\", "/")


class PlanError(ValueError):
    """Invalid global configuration — fail the run before Google calls."""


def validate_accounts(
    works: list[Any],
    builder: EnvironmentBuilder,
    log: Callable[[str], None],
) -> dict[str, EnvironmentArtifacts]:
    if not works:
        raise PlanError("no accounts to provision")
    artifacts: dict[str, EnvironmentArtifacts] = {}
    emails: set[str] = set()
    for work in works:
        email = (work.email or "").strip().lower()
        if not email or "@" not in email:
            raise PlanError(f"invalid account email: {work.email!r}")
        if not work.persona:
            raise PlanError(f"{email}: persona/environment is required")
        env_id = work.environment_id or work.persona
        if env_id not in artifacts:
            artifacts[env_id] = builder.prepare(
                environment_id=env_id,
                persona=work.persona,
                calendar_json=work.calendar_json,
                gmail_json=work.gmail_json,
                drive_json=work.drive_json,
                github_dir=work.github_dir,
                log=log,
                materialize_drive=False,
            )
        art = artifacts[env_id]
        if work.do_calendar and not art.calendar_path:
            raise PlanError(f"{email}: calendar data.json missing for environment {env_id}")
        if work.do_gmail and not art.gmail_path:
            raise PlanError(f"{email}: email data.json missing for environment {env_id}")
        if work.do_drive and not art.drive_path:
            raise PlanError(f"{email}: filesystem data.json missing for environment {env_id}")
        if work.do_github and (not art.github_dir or not art.github_dir.is_dir()):
            raise PlanError(f"{email}: local GitHub directory missing for environment {env_id}")
        if art.errors and any(e.startswith("calendar:") or e.startswith("gmail:") for e in art.errors):
            raise PlanError(f"{env_id}: {'; '.join(art.errors)}")
        emails.add(email)
    log(f"Validated {len(works)} account(s) across {len(artifacts)} environment(s)")
    return artifacts


def _job(
    work: Any,
    *,
    service: str,
    action: str,
    synthetic_id: str,
    source_type: str,
    source_path: str = "",
    depends_on: list[str] | None = None,
    payload: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
    status: str = "PENDING",
    error: str | None = None,
) -> Job:
    return Job(
        job_id="",
        account_id=work.email,
        persona_id=work.persona_key or work.persona,
        environment_id=work.environment_id or work.persona,
        service=service,
        action=action,
        synthetic_id=synthetic_id,
        source_type=source_type,
        source_path=source_path,
        depends_on=list(depends_on or []),
        payload=payload or {},
        extra={**(extra or {}), "mode": getattr(work, "mode", "seed")},
        status=status,
        error=error,
    )


def _remember(store: JobStore, job: Job, planned: list[Job]) -> Job:
    saved = store.upsert(job)
    planned.append(saved)
    return saved


def plan_generate_jobs(
    works: list[Any],
    artifacts: dict[str, EnvironmentArtifacts],
    store: JobStore,
) -> list[Job]:
    planned: list[Job] = []
    seen: set[str] = set()
    for work in works:
        env_id = work.environment_id or work.persona
        if env_id in seen:
            continue
        art = artifacts[env_id]
        if not art.drive_path:
            continue
        if not (work.do_drive or work.do_gmail):
            continue
        seen.add(env_id)
        _remember(
            store,
            Job(
                job_id="",
                account_id="*",
                persona_id=work.persona,
                environment_id=env_id,
                service="generate",
                action="materialize",
                synthetic_id=f"env:{env_id}",
                source_type="generated",
                source_path=str(art.drive_path),
                payload={"persona": work.persona, "drive_json": str(art.drive_path)},
            ),
            planned,
        )
    return planned


def _drive_folder_jobs(
    work: Any,
    store: JobStore,
    planned: list[Job],
    *,
    root_name: str,
    root_sid: str,
    rel_dirs: set[str],
    source_type: str,
    wipe_sid: str | None,
    extra_deps: list[str],
) -> dict[str, str]:
    """Create folder jobs for root + relative dirs. Returns rel -> synthetic_id."""
    ids: dict[str, str] = {"": root_sid}
    _remember(
        store,
        _job(
            work,
            service="drive",
            action="create_folder",
            synthetic_id=root_sid,
            source_type=source_type,
            source_path=root_name,
            depends_on=([wipe_sid] if wipe_sid else []) + extra_deps,
            payload={"name": root_name, "parent": "root"},
        ),
        planned,
    )
    for rel in sorted(rel_dirs, key=lambda r: (r.count("/"), r)):
        parts = [p for p in rel.split("/") if p]
        parent_rel = "/".join(parts[:-1])
        parent_sid = ids[parent_rel] if parent_rel in ids else root_sid
        sid = _sid(source_type, "folder", rel)
        ids[rel] = sid
        _remember(
            store,
            _job(
                work,
                service="drive",
                action="create_folder",
                synthetic_id=sid,
                source_type=source_type,
                source_path=rel,
                depends_on=[parent_sid],
                payload={"name": parts[-1], "parent_sid": parent_sid},
            ),
            planned,
        )
    return ids


def plan_account_jobs(
    work: Any,
    art: EnvironmentArtifacts,
    store: JobStore,
    *,
    max_file_bytes: int,
    log: Callable[[str], None],
) -> list[Job]:
    planned: list[Job] = []
    retry = work.retry_plan or None
    only_github = set(retry.get("github") or []) if retry else None
    only_drive = set(retry.get("drive") or []) if retry else None
    only_gmail = set(retry.get("gmail") or []) if retry else None

    wipe_cal = wipe_mail = wipe_drive = None
    if work.wipe:
        if work.do_calendar:
            wipe_cal = _sid("wipe", "calendar")
            _remember(
                store,
                _job(work, service="calendar", action="wipe", synthetic_id=wipe_cal, source_type="calendar"),
                planned,
            )
        if work.do_gmail:
            wipe_mail = _sid("wipe", "gmail")
            _remember(
                store,
                _job(work, service="gmail", action="wipe", synthetic_id=wipe_mail, source_type="gmail"),
                planned,
            )
        if work.do_drive or work.do_github:
            wipe_drive = _sid("wipe", "drive")
            _remember(
                store,
                _job(work, service="drive", action="wipe", synthetic_id=wipe_drive, source_type="drive"),
                planned,
            )

    generate_sid = f"env:{work.environment_id or work.persona}"
    generate_dep = [generate_sid] if art.drive_path and (work.do_drive or work.do_gmail) else []

    if work.do_calendar and art.calendar_data:
        data = _copy_data(art.calendar_data) or {}
        rewrite_identity(
            gmail_data=None,
            calendar_data=data,
            target_email=work.email,
            log=log,
        )
        seen: set[str] = set()
        for index, item in enumerate(data.get("events") or []):
            if not isinstance(item, dict):
                _remember(
                    store,
                    _job(
                        work,
                        service="calendar",
                        action="insert_event",
                        synthetic_id=_sid("event", f"bad-{index}"),
                        source_type="calendar",
                        status=PERMANENT_FAILURE,
                        error="malformed calendar record",
                    ),
                    planned,
                )
                continue
            eid = str(item.get("event_id") or f"event-{index}")
            if eid in seen:
                _remember(
                    store,
                    _job(
                        work,
                        service="calendar",
                        action="insert_event",
                        synthetic_id=_sid("event", eid, "dup"),
                        source_type="calendar",
                        status=PERMANENT_FAILURE,
                        error=f"duplicate event_id {eid}",
                    ),
                    planned,
                )
                continue
            seen.add(eid)
            body = build_event_body(item)
            if body is None:
                _remember(
                    store,
                    _job(
                        work,
                        service="calendar",
                        action="insert_event",
                        synthetic_id=_sid("event", eid),
                        source_type="calendar",
                        status=PERMANENT_FAILURE,
                        error="calendar event missing start/end",
                    ),
                    planned,
                )
                continue
            _remember(
                store,
                _job(
                    work,
                    service="calendar",
                    action="insert_event",
                    synthetic_id=_sid("event", eid),
                    source_type="calendar",
                    depends_on=[wipe_cal] if wipe_cal else [],
                    payload={"body": body, "event_id": eid, "item": item},
                ),
                planned,
            )

    if work.do_gmail and art.gmail_data:
        data = _copy_data(art.gmail_data) or {}
        rewrite_identity(
            gmail_data=data,
            calendar_data=None,
            target_email=work.email,
            log=log,
        )
        emails = [e for e in (data.get("emails") or []) if isinstance(e, dict)]
        if only_gmail is not None:
            emails = [e for e in emails if str(e.get("email_id") or "") in only_gmail]
        by_id = {str(e.get("email_id")): e for e in emails if e.get("email_id")}
        seen_mail: set[str] = set()
        visiting: set[str] = set()

        def cycle(eid: str) -> bool:
            if eid in visiting:
                return True
            visiting.add(eid)
            parent = str((by_id.get(eid) or {}).get("parent_id") or "")
            bad = bool(parent) and parent in by_id and cycle(parent)
            visiting.discard(eid)
            return bad

        label_sid = _sid("gmail", "label")
        _remember(
            store,
            _job(
                work,
                service="gmail",
                action="ensure_label",
                synthetic_id=label_sid,
                source_type="gmail",
                depends_on=[wipe_mail] if wipe_mail else [],
            ),
            planned,
        )
        for index, item in enumerate(emails):
            eid = str(item.get("email_id") or f"mail-{index}")
            if eid in seen_mail:
                _remember(
                    store,
                    _job(
                        work,
                        service="gmail",
                        action="insert_message",
                        synthetic_id=_sid("mail", eid, "dup"),
                        source_type="gmail",
                        status=PERMANENT_FAILURE,
                        error=f"duplicate email_id {eid}",
                    ),
                    planned,
                )
                continue
            seen_mail.add(eid)
            deps = [label_sid]
            if item.get("attachments") and generate_dep:
                deps.extend(generate_dep)
            if cycle(eid):
                _remember(
                    store,
                    _job(
                        work,
                        service="gmail",
                        action="insert_message",
                        synthetic_id=_sid("mail", eid),
                        source_type="gmail",
                        status=PERMANENT_FAILURE,
                        error="circular parent_id",
                    ),
                    planned,
                )
                continue
            parent = str(item.get("parent_id") or "")
            if parent and parent in by_id:
                deps.append(_sid("mail", parent))
            _remember(
                store,
                _job(
                    work,
                    service="gmail",
                    action="insert_message",
                    synthetic_id=_sid("mail", eid),
                    source_type="gmail",
                    depends_on=deps,
                    payload={"item": item, "email_id": eid, "parent_id": parent},
                    extra={"replace_attachments": bool(work.replace_gmail_attachments)},
                ),
                planned,
            )

    if work.do_drive and art.drive_path:
        root_name = f"GAB_UltraEvals__{work.persona}"
        root_sid = _sid("generated", "folder", root_name)
        entries = [
            e
            for e in (art.drive_entries or metadata_drive_entries(art))
            if isinstance(e, dict) and e.get("rel")
        ]
        if only_drive is not None:
            wanted = {p.replace("\\", "/").lstrip("/") for p in only_drive}
            entries = [e for e in entries if str(e.get("rel") or "") in wanted]
        dirs: set[str] = set()
        for entry in entries:
            rel = str(entry["rel"]).replace("\\", "/").lstrip("/")
            parent = "/".join(rel.split("/")[:-1])
            if parent:
                dirs.update(_folder_parents(parent))
        folder_ids = _drive_folder_jobs(
            work,
            store,
            planned,
            root_name=root_name,
            root_sid=root_sid,
            rel_dirs=dirs,
            source_type="generated",
            wipe_sid=wipe_drive,
            extra_deps=generate_dep,
        )
        seen_files: set[str] = set()
        for entry in entries:
            rel = str(entry["rel"]).replace("\\", "/").lstrip("/")
            if rel in seen_files:
                _remember(
                    store,
                    _job(
                        work,
                        service="drive",
                        action="upload",
                        synthetic_id=_sid("generated", "file", rel, "dup"),
                        source_type="generated",
                        status=PERMANENT_FAILURE,
                        error=f"duplicate generated path {rel}",
                    ),
                    planned,
                )
                continue
            seen_files.add(rel)
            size = int(entry.get("size") or 0)
            parent_rel = "/".join(rel.split("/")[:-1])
            parent_sid = folder_ids.get(parent_rel, root_sid)
            sid = _sid("generated", "file", rel)
            if size > max_file_bytes:
                _remember(
                    store,
                    _job(
                        work,
                        service="drive",
                        action="upload",
                        synthetic_id=sid,
                        source_type="generated",
                        source_path=rel,
                        status=PERMANENT_FAILURE,
                        error=f"oversize {size} bytes",
                    ),
                    planned,
                )
                continue
            _remember(
                store,
                _job(
                    work,
                    service="drive",
                    action="upload",
                    synthetic_id=sid,
                    source_type="generated",
                    source_path=rel,
                    depends_on=[parent_sid],
                    payload={
                        "rel": rel,
                        "filename": entry.get("filename") or rel.rsplit("/", 1)[-1],
                        "mime": entry.get("mime_type") or "application/octet-stream",
                        "size": size,
                        "parent_sid": parent_sid,
                    },
                ),
                planned,
            )

    if work.do_github and art.github_dir:
        files = list(art.github_files)
        if only_github is not None:
            wanted = {p.replace("\\", "/") for p in only_github}
            files = [p for p in files if _github_rel(p, art.github_dir) in wanted]
        dirs = set()
        for path in files:
            rel = _github_rel(path, art.github_dir)
            parent = "/".join(rel.split("/")[:-1])
            if parent:
                dirs.update(_folder_parents(parent))
        root_sid = _sid("github", "folder", "Github")
        folder_ids = _drive_folder_jobs(
            work,
            store,
            planned,
            root_name="Github",
            root_sid=root_sid,
            rel_dirs=dirs,
            source_type="github",
            wipe_sid=wipe_drive,
            extra_deps=[],
        )
        seen_gh: set[str] = set()
        for path in files:
            rel = _github_rel(path, art.github_dir)
            if rel in seen_gh:
                continue
            seen_gh.add(rel)
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            parent_rel = "/".join(rel.split("/")[:-1])
            parent_sid = folder_ids.get(parent_rel, root_sid)
            sid = _sid("github", "file", rel)
            if size > max_file_bytes:
                _remember(
                    store,
                    _job(
                        work,
                        service="drive",
                        action="upload",
                        synthetic_id=sid,
                        source_type="github",
                        source_path=rel,
                        status=PERMANENT_FAILURE,
                        error=f"oversize {size} bytes",
                    ),
                    planned,
                )
                continue
            _remember(
                store,
                _job(
                    work,
                    service="drive",
                    action="upload",
                    synthetic_id=sid,
                    source_type="github",
                    source_path=rel,
                    depends_on=[parent_sid],
                    payload={
                        "rel": rel,
                        "filename": path.name,
                        "abs": str(path),
                        "mime": "application/octet-stream",
                        "size": size,
                        "parent_sid": parent_sid,
                    },
                ),
                planned,
            )
    return planned


def metadata_drive_entries(art: EnvironmentArtifacts) -> list[dict[str, Any]]:
    """File list from filesystem/data.json without writing per-account copies."""
    if art.drive_entries:
        return list(art.drive_entries)
    if not art.drive_path:
        return []
    inspected = inspect_and_normalize(art.drive_path, expected="filesystem")
    if not inspected.get("ok"):
        return []
    entries: list[dict[str, Any]] = []
    for index, item in enumerate(inspected.get("data", {}).get("files") or []):
        if not isinstance(item, dict):
            continue
        rel = str(item.get("path") or item.get("filename") or f"file-{index}").replace("\\", "/").lstrip("/")
        size = item.get("size")
        if size is None:
            content = item.get("content") or ""
            size = len(content) if isinstance(content, (bytes, str)) else 0
        entries.append(
            {
                "rel": rel,
                "filename": str(item.get("filename") or item.get("name") or rel.rsplit("/", 1)[-1])[:200],
                "size": int(size or 0),
                "mime_type": item.get("mime_type") or "application/octet-stream",
            }
        )
    art.drive_entries = entries
    return entries


def wanted_attachments(art: EnvironmentArtifacts) -> set[str]:
    return attachment_filenames(art.gmail_data)

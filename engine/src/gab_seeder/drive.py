from __future__ import annotations

import hashlib
import io
import json
import os
import random
import time
from pathlib import PurePosixPath
from typing import Any, Callable

from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseUpload

from .archive import EnvironmentArchive, decode_content, record_mime, rfc3339, safe_relpath

FOLDER_MIME = "application/vnd.google-apps.folder"
RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
RETRYABLE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded", "sharingRateLimitExceeded"}
DRIVE_FIELDS = "id,name,mimeType,size,md5Checksum,parents,modifiedTime,appProperties,trashed"
DRIVE_BATCH_SIZE = 100

# When set, a persona's GitHub files are seeded into Drive (under a "Github"
# folder) as ordinary, manifest-tracked Drive objects, so delta/reseed/reset
# cover them exactly like any other Drive file. Disable with GAB_DRIVE_INCLUDE_GITHUB=0.
_INCLUDE_GITHUB = os.environ.get("GAB_DRIVE_INCLUDE_GITHUB", "1").strip().lower() not in {
    "0", "false", "no", "off",
}


def _iter_drive_records(archive: EnvironmentArchive, persona: str):
    """Desired Drive records = filesystem files (+ GitHub files as Drive content).

    Single source of truth for every Drive desired-state builder (folders, files,
    seed, delta, verify) so GitHub is treated identically to normal Drive files.
    """
    yield from archive.iter_files(persona)
    # Duck-typed so lightweight test archives without a GitHub service are unaffected.
    services = getattr(archive, "services", None)
    iter_github = getattr(archive, "iter_github_files", None)
    if _INCLUDE_GITHUB and callable(services) and callable(iter_github):
        if "github" in services(persona):
            yield from iter_github(persona)


def _status(exc: HttpError) -> int | None:
    return getattr(exc.resp, "status", None)


def _retryable(exc: HttpError) -> bool:
    if _status(exc) in RETRYABLE_STATUSES:
        return True
    if _status(exc) != 403:
        return False
    try:
        payload = json.loads(exc.content.decode("utf-8"))
    except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    reasons = {
        str(detail.get("reason") or "")
        for detail in (payload.get("error", {}).get("errors") or [])
        if isinstance(detail, dict)
    }
    return bool(reasons & RETRYABLE_REASONS)


def _backoff(attempt: int) -> None:
    time.sleep(min(32.0, 2.0**attempt) + random.uniform(0.0, 1.0))


def _execute_idempotent(request: Callable[[], Any], *, attempts: int = 6) -> dict[str, Any]:
    """Retry a read-only Drive request with bounded exponential backoff."""
    for attempt in range(attempts):
        try:
            return request().execute()
        except HttpError as exc:
            if not _retryable(exc) or attempt + 1 == attempts:
                raise
            _backoff(attempt)
    raise RuntimeError("Drive request retry loop ended unexpectedly")


class _DriveIdPool:
    """Allocate client-supplied IDs so create retries are idempotent."""

    def __init__(self, drive: Any, batch_size: int = 100) -> None:
        self.drive = drive
        self.batch_size = batch_size
        self.ids: list[str] = []

    def take(self) -> str:
        if not self.ids:
            result = _execute_idempotent(
                lambda: self.drive.files().generateIds(
                    count=self.batch_size,
                    space="drive",
                    type="files",
                )
            )
            self.ids = [str(value) for value in result.get("ids", [])]
            if not self.ids:
                raise RuntimeError("Google Drive returned no generated file IDs")
        return self.ids.pop()


def _get_drive_file(drive: Any, file_id: str) -> dict[str, Any] | None:
    try:
        return _execute_idempotent(
            lambda: drive.files().get(fileId=file_id, fields=DRIVE_FIELDS)
        )
    except HttpError as exc:
        if _status(exc) == 404:
            return None
        raise


def _delete_drive_file_idempotent(drive: Any, file_id: str) -> bool:
    for attempt in range(6):
        try:
            drive.files().delete(fileId=file_id).execute()
            return True
        except HttpError as exc:
            status = _status(exc)
            if status == 404:
                return False
            # A 409 Conflict on delete happens when a nested folder is being
            # removed concurrently (e.g. a parent delete cascading to a child).
            # Treat it like a retryable case: re-check existence, then back off
            # and retry serially, mirroring the create path's 409 handling.
            if not _retryable(exc) and status != 409:
                raise
            if _get_drive_file(drive, file_id) is None:
                return True
            if attempt == 5:
                raise
            _backoff(attempt)
    raise RuntimeError("Drive delete retry loop ended unexpectedly")


def _batch_delete_drive_files(drive: Any, ids: list[str]) -> tuple[int, int]:
    """Delete IDs in official HTTP batches; fall back per item when unsupported."""
    if not ids:
        return (0, 0)
    if not hasattr(drive, "new_batch_http_request"):
        deleted = 0
        missing = 0
        for file_id in ids:
            existed = _delete_drive_file_idempotent(drive, file_id)
            deleted += 1 if existed else 0
            missing += 0 if existed else 1
        return deleted, missing
    deleted_ids: set[str] = set()
    missing_ids: set[str] = set()
    retry_ids: set[str] = set()
    fatal_errors: list[BaseException] = []
    files = drive.files()
    for start in range(0, len(ids), DRIVE_BATCH_SIZE):
        chunk = ids[start : start + DRIVE_BATCH_SIZE]
        try:
            batch = drive.new_batch_http_request()
        except Exception:
            retry_ids.update(chunk)
            continue

        def callback(request_id: str, response: Any, exception: Exception | None) -> None:
            file_id = chunk[int(request_id)]
            if exception is None:
                deleted_ids.add(file_id)
                return
            status = getattr(getattr(exception, "resp", None), "status", None)
            if status == 404:
                missing_ids.add(file_id)
                return
            # 409 Conflict (concurrent nested-folder delete) is not fatal: route
            # it to the serialized per-item retry path, which re-checks existence
            # and backs off rather than failing the whole wipe.
            if isinstance(exception, HttpError) and (_retryable(exception) or status == 409):
                retry_ids.add(file_id)
                return
            fatal_errors.append(exception)

        try:
            for offset, file_id in enumerate(chunk):
                batch.add(files.delete(fileId=file_id), request_id=str(offset), callback=callback)
            batch.execute()
        except Exception:
            retry_ids.update(
                file_id
                for file_id in chunk
                if file_id not in deleted_ids and file_id not in missing_ids
            )
        if fatal_errors:
            raise fatal_errors[0]
    for file_id in sorted(retry_ids - deleted_ids - missing_ids):
        existed = _delete_drive_file_idempotent(drive, file_id)
        if existed:
            deleted_ids.add(file_id)
        else:
            missing_ids.add(file_id)
    return len(deleted_ids), len(missing_ids)


def _resolve_drive_root_id(drive: Any | None, fallback: str = "root") -> str:
    if drive is None:
        return fallback
    result = _execute_idempotent(lambda: drive.files().get(fileId="root", fields="id"))
    root_id = str(result.get("id") or "")
    if not root_id:
        raise RuntimeError("Google Drive did not return an opaque root ID")
    return root_id


def _create_drive_item(
    drive: Any,
    *,
    body: dict[str, Any],
    fields: str,
    id_pool: _DriveIdPool,
    media_factory: Callable[[], MediaIoBaseUpload] | None = None,
    attempts: int = 6,
) -> dict[str, Any]:
    """Create once semantically, reconciling ambiguous server responses by ID."""
    generated_id = id_pool.take()
    create_body = dict(body)
    create_body["id"] = generated_id
    for attempt in range(attempts):
        try:
            kwargs: dict[str, Any] = {
                "body": create_body,
                "fields": fields,
                "ignoreDefaultVisibility": True,
            }
            if media_factory is not None:
                kwargs["media_body"] = media_factory()
            return drive.files().create(**kwargs).execute()
        except HttpError as exc:
            status = _status(exc)
            if not _retryable(exc) and status != 409:
                raise
            # A Drive 5xx can explicitly say that the operation succeeded but
            # its response could not be prepared. Read back the exact generated
            # ID rather than issuing a create with a different ID.
            existing = _get_drive_file(drive, generated_id)
            if existing is not None:
                return existing
            if attempt + 1 == attempts:
                raise
            _backoff(attempt)
    raise RuntimeError("Drive create retry loop ended unexpectedly")


def _path_hash(path: str) -> str:
    return hashlib.sha256(path.encode("utf-8")).hexdigest()


class DeltaSafetyError(RuntimeError):
    """Raised when sparse reset cannot classify state safely."""


def _parent_path(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent == "." else parent


def _persona_marker(persona: str) -> str:
    return hashlib.sha256(persona.encode()).hexdigest()[:20]


def _drive_folder_desired(
    *,
    archive: EnvironmentArchive,
    persona: str,
    manifest: dict[str, Any],
    root_id: str = "root",
) -> dict[str, dict[str, Any]]:
    folders: dict[str, dict[str, Any]] = {
        "": {"path": "", "name": "", "parent_path": "", "id": root_id, "path_sha": ""}
    }
    manifest_folders = manifest.get("drive", {}).get("folders", {})
    for path in sorted(path for path in manifest_folders if path):
        path = safe_relpath(path)
        current = path
        stack = []
        while current and current not in folders:
            stack.append(current)
            current = _parent_path(current)
        for folder_path in reversed(stack):
            folders[folder_path] = {
                "path": folder_path,
                "name": PurePosixPath(folder_path).name,
                "parent_path": _parent_path(folder_path),
                "id": manifest_folders.get(folder_path),
                "path_sha": _path_hash(folder_path),
                "markers": {
                    "gabSeed": manifest["seed_tag"],
                    "gabPersona": _persona_marker(persona),
                    "gabPathSha256": _path_hash(folder_path),
                },
            }
    for record in _iter_drive_records(archive, persona):
        parent = _parent_path(safe_relpath(str(record["path"])))
        current = parent
        stack = []
        while current and current not in folders:
            stack.append(current)
            current = _parent_path(current)
        for folder_path in reversed(stack):
            folders[folder_path] = {
                "path": folder_path,
                "name": PurePosixPath(folder_path).name,
                "parent_path": _parent_path(folder_path),
                "id": manifest_folders.get(folder_path),
                "path_sha": _path_hash(folder_path),
                "markers": {
                    "gabSeed": manifest["seed_tag"],
                    "gabPersona": _persona_marker(persona),
                    "gabPathSha256": _path_hash(folder_path),
                },
            }
    return folders


def _drive_file_desired(
    *,
    archive: EnvironmentArchive,
    persona: str,
    manifest: dict[str, Any],
) -> dict[str, dict[str, Any]]:
    manifest_files = manifest.get("drive", {}).get("files", {})
    desired: dict[str, dict[str, Any]] = {}
    for record in _iter_drive_records(archive, persona):
        path = safe_relpath(str(record["path"]))
        entry = manifest_files.get(path) or {}
        complete = all(entry.get(key) is not None for key in ("sha256", "size", "mimeType", "md5Checksum"))
        if complete:
            try:
                sha256 = str(entry["sha256"])
                md5 = str(entry["md5Checksum"])
                size = int(entry["size"])
                mime = str(entry["mimeType"])
            except (TypeError, ValueError):
                complete = False
        if not complete:
            data = decode_content(record)
            sha256 = hashlib.sha256(data).hexdigest()
            md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
            size = len(data)
            mime = record_mime(record)
        desired[path] = {
            "path": path,
            "name": PurePosixPath(path).name,
            "parent_path": _parent_path(path),
            "id": entry.get("id"),
            "path_sha": _path_hash(path),
            "content_sha": sha256,
            "content_marker": sha256[:32],
            "md5Checksum": md5,
            "size": size,
            "mimeType": mime,
            "markers": {
                "gabSeed": manifest["seed_tag"],
                "gabPersona": _persona_marker(persona),
                "gabPathSha256": _path_hash(path),
                "gabContentSha256": sha256[:32],
            },
            "record": record,
        }
    return desired


def _drive_depth(path: str) -> int:
    return 0 if not path else len(PurePosixPath(path).parts)


def _remote_drive_depths(items: list[dict[str, Any]], *, root_id: str = "root") -> dict[str, int]:
    by_id = {str(item.get("id")): item for item in items if item.get("id")}
    memo: dict[str, int] = {root_id: 0, "root": 0}

    def depth(file_id: str, visiting: set[str] | None = None) -> int:
        if file_id in memo:
            return memo[file_id]
        if visiting is None:
            visiting = set()
        if file_id in visiting:
            return 0
        visiting.add(file_id)
        item = by_id.get(file_id)
        if not item:
            memo[file_id] = 0
            return 0
        parents = [str(value) for value in item.get("parents", [])]
        parent_depth = max((depth(parent, visiting) for parent in parents), default=0)
        memo[file_id] = parent_depth + 1
        return memo[file_id]

    return {file_id: depth(file_id) for file_id in by_id}


def _remote_drive_relative_paths(
    items: list[dict[str, Any]], *, root_id: str
) -> tuple[dict[str, list[str]], dict[str, list[dict[str, Any]]]]:
    by_id = {str(item.get("id")): item for item in items if item.get("id")}
    children: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        for parent in [str(value) for value in item.get("parents", [])]:
            children.setdefault(parent, []).append(item)
    for siblings in children.values():
        siblings.sort(key=lambda item: (str(item.get("name") or ""), str(item.get("id") or "")))

    paths_by_id: dict[str, list[str]] = {}
    items_by_path: dict[str, list[dict[str, Any]]] = {}

    def visit(item: dict[str, Any], path: str, ancestry: set[str]) -> None:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in ancestry:
            return
        paths_by_id.setdefault(item_id, []).append(path)
        items_by_path.setdefault(path, []).append(item)
        next_ancestry = {*ancestry, item_id}
        for child in children.get(item_id, []):
            name = str(child.get("name") or "")
            if name:
                visit(child, f"{path}/{name}", next_ancestry)

    for root_child in children.get(root_id, []):
        name = str(root_child.get("name") or "")
        if name and str(root_child.get("id") or "") in by_id:
            visit(root_child, name, set())
    return paths_by_id, items_by_path


def _owned_drive_inventory(drive: Any) -> list[dict[str, Any]]:
    return list_drive_files(drive, "'me' in owners")


def plan_drive_delta(
    *,
    archive: EnvironmentArchive,
    persona: str,
    drive: Any | None,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic Drive reconciliation plan without mutating Drive."""
    seed_tag = str(manifest["seed_tag"])
    manifest_root_id = str(manifest.get("drive", {}).get("folders", {}).get("") or "root")
    root_id = _resolve_drive_root_id(drive, fallback=manifest_root_id)
    desired_folders = _drive_folder_desired(
        archive=archive, persona=persona, manifest=manifest, root_id=root_id
    )
    desired_files = _drive_file_desired(archive=archive, persona=persona, manifest=manifest)
    desired_by_path = {
        **{path: {"kind": "folder", **item} for path, item in desired_folders.items() if path},
        **{path: {"kind": "file", **item} for path, item in desired_files.items()},
    }
    inventory = [] if drive is None else _owned_drive_inventory(drive)
    by_id = {str(item.get("id")): item for item in inventory if item.get("id")}
    by_path_sha: dict[str, list[dict[str, Any]]] = {}
    for item in inventory:
        properties = item.get("appProperties") or {}
        path_sha = str(properties.get("gabPathSha256") or "")
        if path_sha and properties.get("gabSeed") == seed_tag:
            by_path_sha.setdefault(path_sha, []).append(item)
    _paths_by_remote_id, by_remote_path = _remote_drive_relative_paths(inventory, root_id=root_id)
    remote_depths = _remote_drive_depths(inventory, root_id=root_id)

    actions: list[dict[str, Any]] = []
    adoptions: list[dict[str, Any]] = []
    matched_ids: set[str] = set()
    problems: list[str] = []
    stats = {
        "unchanged": 0,
        "metadata_drift": 0,
        "content_mime_drift": 0,
        "missing": 0,
        "duplicate_identity": 0,
        "extra_owned_object": 0,
    }

    def content_matches(desired: dict[str, Any], item: dict[str, Any]) -> bool:
        if item.get("mimeType") != desired.get("mimeType"):
            return False
        try:
            remote_size_int = int(item.get("size"))
        except (TypeError, ValueError):
            remote_size_int = None
        return remote_size_int == desired.get("size") and item.get("md5Checksum") == desired.get("md5Checksum")

    for path in sorted(desired_by_path, key=lambda value: (_drive_depth(value), value)):
        desired = desired_by_path[path]
        canonical = None
        manifest_id = desired.get("id")
        if manifest_id and str(manifest_id) in by_id:
            canonical = by_id[str(manifest_id)]
        path_sha_matches = list(by_path_sha.get(desired["path_sha"], []))
        path_sha_ids = {str(item.get("id") or "") for item in path_sha_matches}
        if canonical is None and len(path_sha_matches) == 1:
            canonical = path_sha_matches[0]
        elif canonical is None and len(path_sha_matches) > 1:
            stats["duplicate_identity"] += len(path_sha_matches)
            problems.append(f"ambiguous duplicate Drive baseline identity for {path}")
            continue
        elif canonical is not None and path_sha_matches:
            canonical_id = str(canonical.get("id") or "")
            duplicate_matches = [
                item for item in path_sha_matches if str(item.get("id") or "") != canonical_id
            ]
            stats["duplicate_identity"] += len(duplicate_matches)
            for dup in sorted(duplicate_matches, key=lambda item: str(item.get("id"))):
                actions.append(
                    {
                        "action": "delete_extra",
                        "kind": "folder" if dup.get("mimeType") == FOLDER_MIME else "file",
                        "reason": "duplicate",
                        "id": dup["id"],
                        "path": path,
                        "depth": remote_depths.get(str(dup.get("id")), 0),
                    }
                )

        exact_matches = list(by_remote_path.get(path, []))
        exact_ids = {str(item.get("id") or "") for item in exact_matches}
        path_sha_duplicate_ids = {
            str(item.get("id") or "")
            for item in path_sha_matches
            if canonical is not None and str(item.get("id") or "") != str(canonical.get("id") or "")
        }
        if canonical is None:
            if len(exact_matches) == 1:
                exact = exact_matches[0]
                exact_properties = exact.get("appProperties") or {}
                gab_identity_markers = {
                    key: exact_properties.get(key)
                    for key in (
                        "gabSeed",
                        "gabPersona",
                        "gabPathSha256",
                        "gabContentSha256",
                    )
                    if exact_properties.get(key) not in (None, "")
                }
                if gab_identity_markers:
                    stats["duplicate_identity"] += 1
                    problems.append(
                        f"Drive baseline path is occupied by a differently marked object for {path}"
                    )
                    continue
                if desired["kind"] == "folder" and exact.get("mimeType") != FOLDER_MIME:
                    stats["content_mime_drift"] += 1
                    problems.append(f"Drive legacy folder path has non-folder type for {path}")
                    continue
                if desired["kind"] == "file" and exact.get("mimeType") == FOLDER_MIME:
                    stats["content_mime_drift"] += 1
                    problems.append(f"Drive legacy file path has folder type for {path}")
                    continue
                canonical = exact
            elif len(exact_matches) > 1:
                stats["duplicate_identity"] += len(exact_matches)
                problems.append(f"ambiguous duplicate Drive legacy path for {path}")
                continue
        elif exact_matches and str(canonical.get("id") or "") not in exact_ids:
            stats["duplicate_identity"] += len(exact_matches)
            problems.append(f"conflicting Drive baseline identities for {path}")
            continue
        elif len(exact_matches) > 1:
            canonical_id = str(canonical.get("id") or "")
            conflicting_marked = []
            for exact in exact_matches:
                exact_id = str(exact.get("id") or "")
                if exact_id == canonical_id or exact_id in path_sha_duplicate_ids:
                    continue
                exact_properties = exact.get("appProperties") or {}
                if any(
                    exact_properties.get(key) not in (None, "")
                    for key in (
                        "gabSeed",
                        "gabPersona",
                        "gabPathSha256",
                        "gabContentSha256",
                    )
                ):
                    conflicting_marked.append(exact_id)
            if conflicting_marked:
                stats["duplicate_identity"] += len(conflicting_marked)
                problems.append(f"conflicting marked Drive object at baseline path for {path}")
                continue

        if canonical is not None and path_sha_matches and str(canonical.get("id") or "") not in path_sha_ids:
            problems.append(f"conflicting Drive path-hash identity for {path}")
            continue

        if canonical is None:
            stats["missing"] += 1
            actions.append({"action": f"create_{desired['kind']}", "path": path})
            continue

        matched_ids.add(str(canonical["id"]))
        if desired.get("id") != canonical.get("id"):
            adoptions.append(
                {
                    "kind": desired["kind"],
                    "path": path,
                    "id": canonical["id"],
                    "mimeType": canonical.get("mimeType"),
                    "md5Checksum": canonical.get("md5Checksum"),
                }
            )
            desired["id"] = canonical["id"]
        if desired["kind"] == "folder":
            desired_folders[path]["id"] = canonical["id"]
        else:
            desired_files[path]["id"] = canonical["id"]
        parents = [str(value) for value in canonical.get("parents", [])]
        expected_parent_id = (
            root_id
            if desired["parent_path"] == ""
            else desired_folders.get(desired["parent_path"], {}).get("id")
        )
        metadata_changed = (
            canonical.get("name") != desired["name"]
            or bool(canonical.get("trashed")) is True
            or (expected_parent_id is not None and set(parents) != {str(expected_parent_id)})
        )
        properties = canonical.get("appProperties") or {}
        marker_changed = properties != (desired.get("markers") or {})
        if desired["kind"] == "folder":
            if canonical.get("mimeType") != FOLDER_MIME:
                stats["content_mime_drift"] += 1
                problems.append(f"Drive baseline folder identity has non-folder type for {path}")
            elif metadata_changed or marker_changed:
                stats["metadata_drift"] += 1
                actions.append(
                    {
                        "action": "patch_metadata",
                        "kind": "folder",
                        "path": path,
                        "id": canonical["id"],
                        "observed_parents": parents,
                        "observed_properties": dict(properties),
                    }
                )
            else:
                stats["unchanged"] += 1
            continue

        content_changed = not content_matches(desired, canonical)
        wrong_type = canonical.get("mimeType") == FOLDER_MIME
        if wrong_type:
            stats["content_mime_drift"] += 1
            problems.append(f"Drive baseline file identity has folder type for {path}")
            continue
        if content_changed:
            stats["content_mime_drift"] += 1
            actions.append({"action": "replace_file", "path": path, "id": canonical["id"]})
        elif metadata_changed or marker_changed:
            stats["metadata_drift"] += 1
            actions.append(
                {
                    "action": "patch_metadata",
                    "kind": "file",
                    "path": path,
                    "id": canonical["id"],
                    "observed_parents": parents,
                    "observed_properties": dict(properties),
                }
            )
        else:
            stats["unchanged"] += 1

    scheduled_delete_ids = {
        str(action.get("id"))
        for action in actions
        if action.get("action") == "delete_extra" and action.get("id")
    }
    for item in inventory:
        item_id = str(item.get("id") or "")
        if not item_id or item_id in matched_ids:
            continue
        if item_id in scheduled_delete_ids:
            continue
        stats["extra_owned_object"] += 1
        actions.append(
            {
                "action": "delete_extra",
                "kind": "folder" if item.get("mimeType") == FOLDER_MIME else "file",
                "id": item_id,
                "name": item.get("name", ""),
                "depth": remote_depths.get(item_id, 0),
            }
        )

    delete_actions = [item for item in actions if item["action"] == "delete_extra"]
    other_actions = [item for item in actions if item["action"] != "delete_extra"]
    delete_actions.sort(key=lambda item: (item.get("kind") == "folder", -int(item.get("depth", 0)), str(item.get("id"))))
    def action_rank(item: dict[str, Any]) -> tuple[int, int, str, str]:
        path = str(item.get("path") or "")
        is_folder = item.get("kind") == "folder" or item.get("action") == "create_folder"
        if is_folder:
            return (0, _drive_depth(path), path, str(item.get("action") or ""))
        return (1, _drive_depth(path), path, str(item.get("action") or ""))

    other_actions.sort(key=action_rank)
    ordered = other_actions + delete_actions
    return {
        "ok": not problems,
        "problems": problems,
        "actions": ordered,
        "adoptions": sorted(adoptions, key=lambda item: (item["kind"], item["path"])),
        "counts": {**stats, "writes": len(ordered)},
        "desired_folders": desired_folders,
        "desired_files": desired_files,
    }


def _drive_metadata_update_kwargs(
    *,
    file_id: str,
    desired: dict[str, Any],
    parent_id: str,
    current_parents: list[str],
    current_properties: dict[str, Any],
    fields: str = DRIVE_FIELDS,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "fileId": file_id,
        "body": {"name": desired["name"], "trashed": False},
        "fields": fields,
    }
    desired_properties = dict(desired.get("markers") or {})
    # Drive merges appProperties during files.update. Sending only desired keys
    # leaves stale keys behind; null explicitly removes each unexpected key.
    app_properties = {
        key: None for key in current_properties if key not in desired_properties
    }
    app_properties.update(desired_properties)
    kwargs["body"]["appProperties"] = app_properties
    if parent_id:
        if parent_id not in current_parents:
            kwargs["addParents"] = parent_id
        removable = [value for value in current_parents if value != parent_id]
        if removable:
            kwargs["removeParents"] = ",".join(removable)
    return kwargs


def _patch_drive_metadata(
    drive: Any,
    *,
    file_id: str,
    desired: dict[str, Any],
    parent_id: str,
    fields: str = DRIVE_FIELDS,
) -> dict[str, Any]:
    current = _get_drive_file(drive, file_id) or {}
    kwargs = _drive_metadata_update_kwargs(
        file_id=file_id,
        desired=desired,
        parent_id=parent_id,
        current_parents=[str(value) for value in current.get("parents", [])],
        current_properties=dict(current.get("appProperties") or {}),
        fields=fields,
    )
    return drive.files().update(**kwargs).execute()


def _batch_patch_drive_metadata(
    drive: Any,
    operations: list[tuple[dict[str, Any], dict[str, Any], str]],
) -> int:
    if not operations:
        return 0
    if not hasattr(drive, "new_batch_http_request"):
        for action, desired, parent_id in operations:
            _patch_drive_metadata(
                drive,
                file_id=action["id"],
                desired=desired,
                parent_id=parent_id,
            )
        return len(operations)
    completed: set[str] = set()
    retry: dict[str, tuple[dict[str, Any], dict[str, Any], str]] = {}
    fatal: list[BaseException] = []
    files_resource = drive.files()
    for start in range(0, len(operations), DRIVE_BATCH_SIZE):
        chunk = operations[start : start + DRIVE_BATCH_SIZE]
        try:
            batch = drive.new_batch_http_request()
        except Exception:
            retry.update({str(action["id"]): (action, desired, parent_id) for action, desired, parent_id in chunk})
            continue

        def callback(request_id: str, response: Any, exception: Exception | None) -> None:
            action, desired, parent_id = chunk[int(request_id)]
            file_id = str(action["id"])
            if exception is None:
                completed.add(file_id)
            elif isinstance(exception, HttpError) and _retryable(exception):
                retry[file_id] = (action, desired, parent_id)
            else:
                fatal.append(exception)

        try:
            for offset, (action, desired, parent_id) in enumerate(chunk):
                kwargs = _drive_metadata_update_kwargs(
                    file_id=action["id"],
                    desired=desired,
                    parent_id=parent_id,
                    current_parents=[str(value) for value in action.get("observed_parents", [])],
                    current_properties=dict(action.get("observed_properties") or {}),
                )
                batch.add(
                    files_resource.update(**kwargs),
                    request_id=str(offset),
                    callback=callback,
                )
            batch.execute()
        except Exception:
            retry.update(
                {
                    str(action["id"]): (action, desired, parent_id)
                    for action, desired, parent_id in chunk
                    if str(action["id"]) not in completed
                }
            )
        if fatal:
            raise fatal[0]
    for file_id, (action, desired, parent_id) in sorted(retry.items()):
        if file_id not in completed:
            _patch_drive_metadata(
                drive,
                file_id=action["id"],
                desired=desired,
                parent_id=parent_id,
            )
            completed.add(file_id)
    return len(completed)


def _create_delta_folder(
    drive: Any,
    *,
    persona: str,
    seed_tag: str,
    desired: dict[str, Any],
    parent_id: str,
    id_pool: _DriveIdPool,
) -> dict[str, Any]:
    body = {
        "name": desired["name"],
        "mimeType": FOLDER_MIME,
        "parents": [parent_id],
        "appProperties": {
            "gabSeed": seed_tag,
            "gabPersona": hashlib.sha256(persona.encode()).hexdigest()[:20],
            "gabPathSha256": desired["path_sha"],
        },
    }
    return _create_drive_item(drive, body=body, fields=DRIVE_FIELDS, id_pool=id_pool)


def _create_delta_file(
    drive: Any,
    *,
    persona: str,
    seed_tag: str,
    desired: dict[str, Any],
    parent_id: str,
    id_pool: _DriveIdPool,
) -> dict[str, Any]:
    data = decode_content(desired["record"])
    body: dict[str, Any] = {
        "name": desired["name"],
        "parents": [parent_id],
        "appProperties": {
            "gabSeed": seed_tag,
            "gabPersona": hashlib.sha256(persona.encode()).hexdigest()[:20],
            "gabPathSha256": desired["path_sha"],
            "gabContentSha256": desired["content_marker"],
        },
    }
    if desired["record"].get("modified") is not None:
        body["modifiedTime"] = rfc3339(desired["record"]["modified"])

    def media_factory() -> MediaIoBaseUpload:
        return MediaIoBaseUpload(
            io.BytesIO(data),
            mimetype=desired["mimeType"],
            resumable=len(data) >= 5 * 1024 * 1024,
            chunksize=5 * 1024 * 1024,
        )

    return _create_drive_item(
        drive,
        body=body,
        media_factory=media_factory,
        fields=DRIVE_FIELDS,
        id_pool=id_pool,
    )


def reconcile_drive_delta(
    *,
    archive: EnvironmentArchive,
    persona: str,
    drive: Any | None,
    manifest: dict[str, Any],
    checkpoint: Callable[[], None],
    dry_run: bool,
) -> dict[str, Any]:
    plan = plan_drive_delta(archive=archive, persona=persona, drive=drive, manifest=manifest)
    summary = {
        "planned_writes": plan["counts"]["writes"],
        "manifest_updates": len(plan["adoptions"]),
        "counts": {**plan["counts"], "manifest_updates": len(plan["adoptions"])},
        "dry_run": dry_run,
    }
    if not plan["ok"]:
        raise DeltaSafetyError("; ".join(plan["problems"]) + "; explicit full reset required")
    if dry_run:
        return {**summary, "actions": [item["action"] for item in plan["actions"]]}
    if drive is None:
        raise RuntimeError("Drive service is required for live delta reconcile")

    id_pool = _DriveIdPool(drive)
    folders: dict[str, str] = manifest.setdefault("drive", {}).setdefault("folders", {})
    files: dict[str, Any] = manifest.setdefault("drive", {}).setdefault("files", {})
    desired_folders = plan["desired_folders"]
    desired_files = plan["desired_files"]
    root_id = str(desired_folders[""]["id"])
    folders[""] = root_id
    applied = 0
    manifest_updates = 0

    def folder_id(path: str) -> str:
        if path == "":
            return root_id
        value = folders.get(path) or desired_folders.get(path, {}).get("id")
        if not value:
            raise DeltaSafetyError(f"cannot resolve Drive parent folder {path!r}; explicit full reset required")
        return str(value)

    if plan["adoptions"]:
        for adoption in plan["adoptions"]:
            if adoption["kind"] == "folder":
                folders[adoption["path"]] = adoption["id"]
            else:
                desired = desired_files[adoption["path"]]
                files[adoption["path"]] = {
                    "id": adoption["id"],
                    "sha256": desired["content_sha"],
                    "size": desired["size"],
                    "mimeType": adoption.get("mimeType") or desired["mimeType"],
                    "md5Checksum": adoption.get("md5Checksum"),
                }
            manifest_updates += 1
        checkpoint()
    index = 0
    while index < len(plan["actions"]):
        action = plan["actions"][index]
        kind = action["action"]
        path = action.get("path")
        if kind == "create_folder":
            desired = desired_folders[str(path)]
            result = _create_delta_folder(
                drive,
                persona=persona,
                seed_tag=manifest["seed_tag"],
                desired=desired,
                parent_id=folder_id(desired["parent_path"]),
                id_pool=id_pool,
            )
            folders[str(path)] = result["id"]
            checkpoint()
        elif kind == "create_file":
            desired = desired_files[str(path)]
            result = _create_delta_file(
                drive,
                persona=persona,
                seed_tag=manifest["seed_tag"],
                desired=desired,
                parent_id=folder_id(desired["parent_path"]),
                id_pool=id_pool,
            )
            files[str(path)] = {
                "id": result["id"],
                "sha256": desired["content_sha"],
                "size": desired["size"],
                "mimeType": result.get("mimeType", desired["mimeType"]),
                "md5Checksum": result.get("md5Checksum") or desired["md5Checksum"],
            }
            checkpoint()
        elif kind == "patch_metadata":
            patch_actions = []
            while (
                index < len(plan["actions"])
                and plan["actions"][index]["action"] == "patch_metadata"
            ):
                patch_actions.append(plan["actions"][index])
                index += 1
            operations = []
            for patch_action in patch_actions:
                patch_path = str(patch_action["path"])
                desired = (
                    desired_folders[patch_path]
                    if patch_action["kind"] == "folder"
                    else desired_files[patch_path]
                )
                operations.append(
                    (
                        patch_action,
                        desired,
                        folder_id(desired["parent_path"]),
                    )
                )
            applied += _batch_patch_drive_metadata(drive, operations)
            continue
        elif kind == "replace_file":
            drive.files().delete(fileId=action["id"]).execute()
            desired = desired_files[str(path)]
            result = _create_delta_file(
                drive,
                persona=persona,
                seed_tag=manifest["seed_tag"],
                desired=desired,
                parent_id=folder_id(desired["parent_path"]),
                id_pool=id_pool,
            )
            files[str(path)] = {
                "id": result["id"],
                "sha256": desired["content_sha"],
                "size": desired["size"],
                "mimeType": result.get("mimeType", desired["mimeType"]),
                "md5Checksum": result.get("md5Checksum") or desired["md5Checksum"],
            }
            checkpoint()
        elif kind == "delete_extra":
            batch = [action]
            index += 1
            while index < len(plan["actions"]) and plan["actions"][index]["action"] == "delete_extra" and plan["actions"][index].get("kind") == action.get("kind") and int(plan["actions"][index].get("depth", 0)) == int(action.get("depth", 0)):
                batch.append(plan["actions"][index])
                index += 1
            deleted, missing = _batch_delete_drive_files(drive, [item["id"] for item in batch])
            applied += deleted + missing
            continue
        else:
            raise RuntimeError(f"unknown Drive delta action: {kind}")
        applied += 1
        index += 1
    if applied and not plan["adoptions"]:
        checkpoint()
    return {**summary, "applied_writes": applied, "applied_manifest_updates": manifest_updates}


def seed_drive(
    *,
    archive: EnvironmentArchive,
    persona: str,
    drive: Any | None,
    manifest: dict[str, Any],
    checkpoint: Callable[[], None],
    dry_run: bool,
) -> dict[str, Any]:
    seed_tag = manifest["seed_tag"]
    folders: dict[str, str] = manifest["drive"]["folders"]
    files: dict[str, Any] = manifest["drive"]["files"]
    root_id = "root" if dry_run else _resolve_drive_root_id(drive, fallback=str(folders.get("") or "root"))
    folders.setdefault("", root_id)
    if not dry_run:
        folders[""] = root_id
    created_folders = 0
    uploaded_files = 0
    uploaded_bytes = 0
    reconciled_folders = 0
    reconciled_files = 0
    id_pool = _DriveIdPool(drive) if not dry_run else None
    existing_by_path: dict[str, dict[str, Any]] = {}
    if not dry_run:
        for item in list_drive_files(
            drive,
            f"trashed = false and appProperties has {{ key='gabSeed' and value='{seed_tag}' }}",
        ):
            path_sha = str((item.get("appProperties") or {}).get("gabPathSha256") or "")
            if not path_sha:
                continue
            if path_sha in existing_by_path:
                raise RuntimeError(f"duplicate Drive seed objects for path hash {path_sha}")
            existing_by_path[path_sha] = item

    def ensure_folder(path: str) -> str:
        nonlocal created_folders, reconciled_folders
        path = safe_relpath(path) if path else ""
        if path in folders:
            return folders[path]
        parent = str(PurePosixPath(path).parent)
        if parent == ".":
            parent = ""
        parent_id = ensure_folder(parent)
        created_now = False
        if dry_run:
            folder_id = f"dry-folder-{_path_hash(path)[:16]}"
            created_now = True
        else:
            path_sha = _path_hash(path)
            body = {
                "name": PurePosixPath(path).name,
                "mimeType": FOLDER_MIME,
                "parents": [parent_id],
                "appProperties": {
                    "gabSeed": seed_tag,
                    "gabPersona": hashlib.sha256(persona.encode()).hexdigest()[:20],
                    "gabPathSha256": path_sha,
                },
            }
            result = existing_by_path.get(path_sha)
            if result is not None:
                if result.get("mimeType") != FOLDER_MIME or parent_id not in result.get("parents", []):
                    raise RuntimeError(f"existing Drive folder does not match baseline path {path}")
                reconciled_folders += 1
            else:
                if id_pool is None:
                    raise RuntimeError("Drive ID pool is unavailable")
                result = _create_drive_item(
                    drive,
                    body=body,
                    fields="id,name,mimeType,parents,appProperties",
                    id_pool=id_pool,
                )
                existing_by_path[path_sha] = result
                created_now = True
            folder_id = result["id"]
        folders[path] = folder_id
        if created_now:
            created_folders += 1
        checkpoint()
        return folder_id

    # Folders are created lazily from each file path. This avoids parsing the
    # very large filesystem JSON twice merely to preserve empty directories,
    # which are not searchable artifacts in Drive.

    for record in _iter_drive_records(archive, persona):
        path = safe_relpath(str(record["path"]))
        if path in files:
            continue
        parent = str(PurePosixPath(path).parent)
        if parent == ".":
            parent = ""
        parent_id = ensure_folder(parent)
        data = decode_content(record)
        mime = record_mime(record)
        sha256 = hashlib.sha256(data).hexdigest()
        body: dict[str, Any] = {
            "name": PurePosixPath(path).name,
            "parents": [parent_id],
            "appProperties": {
                "gabSeed": seed_tag,
                "gabPersona": hashlib.sha256(persona.encode()).hexdigest()[:20],
                "gabPathSha256": _path_hash(path),
                "gabContentSha256": sha256[:32],
            },
        }
        if record.get("modified") is not None:
            body["modifiedTime"] = rfc3339(record["modified"])
        uploaded_now = False
        if dry_run:
            result = {
                "id": f"dry-file-{_path_hash(path)[:16]}",
                "name": body["name"],
                "mimeType": mime,
                "size": str(len(data)),
                "md5Checksum": hashlib.md5(data, usedforsecurity=False).hexdigest(),
            }
            uploaded_now = True
        else:
            path_sha = _path_hash(path)
            result = existing_by_path.get(path_sha)
            if result is not None:
                properties = result.get("appProperties") or {}
                try:
                    remote_size = int(result.get("size"))
                except (TypeError, ValueError):
                    remote_size = None
                md5 = hashlib.md5(data, usedforsecurity=False).hexdigest()
                if (
                    result.get("mimeType") == FOLDER_MIME
                    or parent_id not in result.get("parents", [])
                    or properties.get("gabContentSha256") != sha256[:32]
                    or remote_size != len(data)
                    or result.get("md5Checksum") != md5
                ):
                    raise RuntimeError(f"existing Drive file does not match baseline path {path}")
                reconciled_files += 1
            else:
                if id_pool is None:
                    raise RuntimeError("Drive ID pool is unavailable")

                def media_factory() -> MediaIoBaseUpload:
                    return MediaIoBaseUpload(
                        io.BytesIO(data),
                        mimetype=mime,
                        resumable=len(data) >= 5 * 1024 * 1024,
                        chunksize=5 * 1024 * 1024,
                    )

                result = _create_drive_item(
                    drive,
                    body=body,
                    media_factory=media_factory,
                    fields=DRIVE_FIELDS,
                    id_pool=id_pool,
                )
                existing_by_path[path_sha] = result
                uploaded_now = True
        files[path] = {
            "id": result["id"],
            "sha256": sha256,
            "size": len(data),
            "mimeType": result.get("mimeType", mime),
            "md5Checksum": result.get("md5Checksum"),
        }
        if uploaded_now:
            uploaded_files += 1
            uploaded_bytes += len(data)
        checkpoint()
    return {
        "created_folders": created_folders,
        "uploaded_files": uploaded_files,
        "uploaded_bytes": uploaded_bytes,
        "reconciled_folders": reconciled_folders,
        "reconciled_files": reconciled_files,
        "manifest_folders": len(folders) - 1,
        "manifest_files": len(files),
    }


def list_drive_files(drive: Any, query: str) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    page_token = None
    while True:
        response = _execute_idempotent(
            lambda: drive.files().list(
                q=query,
                spaces="drive",
                fields=f"nextPageToken,files({DRIVE_FIELDS})",
                pageSize=1000,
                pageToken=page_token,
            )
        )
        result.extend(response.get("files", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return result


def verify_drive(
    drive: Any,
    manifest: dict[str, Any],
    *,
    archive: EnvironmentArchive | None = None,
    persona: str | None = None,
) -> dict[str, Any]:
    root_id = _resolve_drive_root_id(drive, fallback=str(manifest.get("drive", {}).get("folders", {}).get("") or "root"))
    remote = list_drive_files(drive, "'me' in owners")
    by_id = {str(item.get("id")): item for item in remote if item.get("id")}
    if archive is not None and persona is not None:
        desired_folders = _drive_folder_desired(
            archive=archive, persona=persona, manifest=manifest, root_id=root_id
        )
        desired_files = _drive_file_desired(archive=archive, persona=persona, manifest=manifest)
    else:
        manifest_folders = manifest.get("drive", {}).get("folders", {})
        desired_folders = {
            path: {
                "path": path,
                "name": PurePosixPath(path).name,
                "parent_path": _parent_path(path),
                "id": file_id,
                "path_sha": _path_hash(path),
                "markers": {
                    "gabSeed": manifest.get("seed_tag"),
                    "gabPersona": _persona_marker(str(manifest.get("persona") or "")),
                    "gabPathSha256": _path_hash(path),
                },
            }
            for path, file_id in manifest_folders.items()
            if path and file_id
        }
        desired_folders[""] = {"path": "", "name": "", "parent_path": "", "id": root_id, "path_sha": ""}
        desired_files = {}
        for path, entry in manifest.get("drive", {}).get("files", {}).items():
            desired_files[path] = {
                "path": path,
                "name": PurePosixPath(path).name,
                "parent_path": _parent_path(path),
                "id": entry.get("id"),
                "path_sha": _path_hash(path),
                "content_sha": entry.get("sha256"),
                "content_marker": str(entry.get("sha256") or "")[:32],
                "md5Checksum": entry.get("md5Checksum"),
                "size": entry.get("size"),
                "mimeType": entry.get("mimeType"),
                "markers": {
                    "gabSeed": manifest.get("seed_tag"),
                    "gabPersona": _persona_marker(str(manifest.get("persona") or "")),
                    "gabPathSha256": _path_hash(path),
                    "gabContentSha256": str(entry.get("sha256") or "")[:32],
                },
            }
    expected_folders = {path: item for path, item in desired_folders.items() if path}
    expected_ids = {str(item["id"]) for item in expected_folders.values() if item.get("id")}
    expected_ids.update(str(entry.get("id")) for entry in desired_files.values() if entry.get("id"))
    desired_path_shas = {
        item["path_sha"]
        for item in [*expected_folders.values(), *desired_files.values()]
        if item.get("path_sha")
    }
    by_path_sha: dict[str, list[dict[str, Any]]] = {}
    for item in remote:
        properties = item.get("appProperties") or {}
        if properties.get("gabSeed") != manifest.get("seed_tag"):
            continue
        path_sha = str(properties.get("gabPathSha256") or "")
        if path_sha:
            by_path_sha.setdefault(path_sha, []).append(item)
    _paths_by_remote_id, by_remote_path = _remote_drive_relative_paths(remote, root_id=root_id)
    missing: list[str] = []
    drifted: list[str] = []
    duplicate_identity_ids: set[str] = set()
    for path_sha in desired_path_shas:
        matches = by_path_sha.get(path_sha, [])
        if len(matches) > 1:
            duplicate_identity_ids.update(str(item.get("id") or "") for item in matches if item.get("id"))
    for path, desired in sorted(expected_folders.items()):
        item = by_id.get(str(desired.get("id")))
        if item is None:
            missing.append(path)
            continue
        exact_matches = by_remote_path.get(path, [])
        if len(exact_matches) != 1:
            duplicate_identity_ids.update(str(item.get("id") or "") for item in exact_matches if item.get("id"))
        properties = item.get("appProperties") or {}
        expected_parent = root_id if desired["parent_path"] == "" else desired_folders.get(desired["parent_path"], {}).get("id")
        parents = [str(value) for value in item.get("parents", [])]
        if (
            item.get("mimeType") != FOLDER_MIME
            or item.get("trashed")
            or item.get("name") != desired["name"]
            or properties != (desired.get("markers") or {})
            or (expected_parent and set(parents) != {str(expected_parent)})
        ):
            drifted.append(path)
    for path, desired in sorted(desired_files.items()):
        item = by_id.get(str(desired.get("id")))
        if item is None:
            missing.append(path)
            continue
        exact_matches = by_remote_path.get(path, [])
        if len(exact_matches) != 1:
            duplicate_identity_ids.update(str(item.get("id") or "") for item in exact_matches if item.get("id"))
        properties = item.get("appProperties") or {}
        expected_parent = root_id if desired["parent_path"] == "" else desired_folders.get(desired["parent_path"], {}).get("id")
        parents = [str(value) for value in item.get("parents", [])]
        try:
            remote_size = int(item.get("size"))
        except (TypeError, ValueError):
            remote_size = None
        expected_size = desired.get("size")
        try:
            expected_size = int(expected_size)
        except (TypeError, ValueError):
            expected_size = None
        if (
            item.get("mimeType") != desired.get("mimeType")
            or item.get("trashed")
            or item.get("name") != desired["name"]
            or properties != (desired.get("markers") or {})
            or (expected_parent and set(parents) != {str(expected_parent)})
            or remote_size != expected_size
            or item.get("md5Checksum") != desired.get("md5Checksum")
        ):
            drifted.append(path)
    extras = [
        item.get("id")
        for item in remote
        if str(item.get("id") or "") not in expected_ids
    ]
    expected = len(desired_files) + len(expected_folders)
    return {
        "expected_seeded_objects": expected,
        "remote_seeded_objects": len(remote) - len(extras),
        "missing_seeded_objects": len(missing),
        "drifted_seeded_objects": len(drifted),
        "extra_owned_objects": len(extras),
        "duplicate_baseline_identities": len(duplicate_identity_ids),
        "ok": not missing and not drifted and not extras and not duplicate_identity_ids,
    }


def reset_drive_all(drive: Any, *, dry_run: bool) -> dict[str, int]:
    # Include trash. A model may trash a baseline file during Product A; leaving
    # it there and uploading a replacement would not restore the original state.
    items = list_drive_files(drive, "'me' in owners")
    items.sort(key=lambda item: item.get("mimeType") == FOLDER_MIME)
    deleted = 0
    missing = 0
    files = [item for item in items if item.get("mimeType") != FOLDER_MIME]
    folders = [item for item in items if item.get("mimeType") == FOLDER_MIME]
    folders.sort(key=lambda item: -_remote_drive_depths(items).get(str(item.get("id")), 0))
    ordered_groups = [files]
    for depth in sorted({ _remote_drive_depths(items).get(str(item.get("id")), 0) for item in folders }, reverse=True):
        ordered_groups.append([item for item in folders if _remote_drive_depths(items).get(str(item.get("id")), 0) == depth])
    for group in ordered_groups:
        if not group:
            continue
        if dry_run:
            deleted += len(group)
            continue
        group_deleted, group_missing = _batch_delete_drive_files(drive, [item["id"] for item in group])
        deleted += group_deleted
        missing += group_missing
    return {"found": len(items), "deleted": deleted, "already_missing": missing}

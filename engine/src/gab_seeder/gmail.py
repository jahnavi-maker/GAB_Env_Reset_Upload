from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import time
from collections import defaultdict
from email.message import EmailMessage
from email.policy import SMTP
from email.utils import format_datetime
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .archive import EnvironmentArchive, decode_content, parse_datetime, record_mime, safe_relpath


class AttachmentError(RuntimeError):
    pass


class GmailDeltaSafetyError(RuntimeError):
    """Raised when sparse Gmail reset cannot classify state safely."""


def cache_attachments(
    *,
    archive: EnvironmentArchive,
    persona: str,
    destination: Path,
) -> tuple[dict[str, tuple[Path, str]], list[str], list[str]]:
    needed = {PurePosixPath(name).name for name in archive.attachment_names(persona)}
    destination.mkdir(parents=True, exist_ok=True)
    if not needed:
        return {}, [], []
    matches: dict[str, list[tuple[Path, str]]] = defaultdict(list)
    for record in archive.iter_files(persona):
        source_path = safe_relpath(str(record["path"]))
        basename = PurePosixPath(source_path).name
        if basename not in needed:
            continue
        digest = hashlib.sha256(source_path.encode("utf-8")).hexdigest()[:12]
        target = destination / f"{digest}-{basename}"
        if not target.exists():
            target.write_bytes(decode_content(record))
        matches[basename].append((target, record_mime(record)))
    resolved = {name: values[0] for name, values in matches.items() if len(values) == 1}
    missing = sorted(needed - set(matches))
    ambiguous = sorted(name for name, values in matches.items() if len(values) > 1)
    return resolved, missing, ambiguous


def _message_id(source_id: str) -> str:
    clean = "".join(ch for ch in source_id.casefold() if ch.isalnum() or ch in ".-_")
    return f"<gab-{clean}@seed.invalid>"


def _gmail_thread_subject_key(subject: Any) -> str:
    value = " ".join(str(subject or "").split()).casefold()
    while True:
        stripped = re.sub(r"^(?:re|fw|fwd)\s*:\s*", "", value, count=1)
        if stripped == value:
            return value
        value = stripped


def _gmail_thread_compatible(child: dict[str, Any], parent: dict[str, Any]) -> bool:
    child_key = _gmail_thread_subject_key(child.get("subject"))
    parent_key = _gmail_thread_subject_key(parent.get("subject"))
    return child_key == parent_key


MUTABLE_SYSTEM_LABELS = {"INBOX", "TRASH", "SPAM", "IMPORTANT", "STARRED", "UNREAD"}
IMMUTABLE_SYSTEM_LABELS = {"SENT", "DRAFT"}
BASELINE_HEADER_NAMES = ["X-GAB-Seed-ID", "Message-ID", "In-Reply-To", "References"]
GMAIL_METADATA_BATCH_SIZE = 3
GMAIL_QUOTA_UNITS_PER_SECOND = 75.0


class _GmailQuotaPacer:
    """Shape per-user Gmail quota usage across discovery, writes, and verify.

    Gmail batches reduce HTTP overhead but every inner request still consumes
    its normal quota units. The process-level pacer is deliberately shared by
    the reconciliation service and the fresh service built for readback
    verification; each worker process handles only one account at a time.
    """

    def __init__(
        self,
        *,
        units_per_second: float = GMAIL_QUOTA_UNITS_PER_SECOND,
        clock: Callable[[], float] | None = None,
        sleeper: Callable[[float], None] | None = None,
    ) -> None:
        self.units_per_second = float(units_per_second)
        self.clock = clock or time.monotonic
        self.sleeper = sleeper or time.sleep
        self.next_available = 0.0

    def acquire(self, units: int) -> None:
        if units <= 0:
            return
        now = self.clock()
        scheduled = max(now, self.next_available)
        delay = scheduled - now
        if delay > 0:
            self.sleeper(delay)
        self.next_available = scheduled + (float(units) / self.units_per_second)


_GMAIL_QUOTA_PACER = _GmailQuotaPacer()


def _pace_gmail(units: int) -> None:
    _GMAIL_QUOTA_PACER.acquire(units)


def _baseline_label_name(manifest: dict[str, Any]) -> str:
    return f"GAB_BASELINE_{manifest['seed_tag']}"


def _expected_gmail_labels(record: dict[str, Any], label_id: str) -> list[str]:
    folder = str(record.get("folder") or "").upper()
    labels = [label_id]
    if folder in {"INBOX", "SENT", "TRASH", "SPAM", "IMPORTANT", "STARRED"}:
        labels.append(folder)
    if not bool(record.get("is_read", True)):
        labels.append("UNREAD")
    return labels


def _header_value(message: dict[str, Any], name: str) -> str:
    headers = ((message.get("payload") or {}).get("headers") or [])
    for header in headers:
        if str(header.get("name") or "").casefold() == name.casefold():
            return str(header.get("value") or "")
    return ""


def ensure_gmail_baseline_label(
    gmail: Any | None,
    manifest: dict[str, Any],
    *,
    dry_run: bool,
    checkpoint: Callable[[], None] | None = None,
) -> str:
    label_name = _baseline_label_name(manifest)
    existing_id = manifest.setdefault("gmail", {}).get("label_id")
    if dry_run:
        return existing_id or "dry-label"
    _pace_gmail(1)
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    existing = next((item for item in labels if item.get("name") == label_name), None)
    if existing:
        label_id = existing["id"]
    else:
        _pace_gmail(5)
        result = (
            gmail.users()
            .labels()
            .create(
                userId="me",
                body={
                    "name": label_name,
                    "labelListVisibility": "labelHide",
                    "messageListVisibility": "hide",
                },
            )
            .execute()
        )
        label_id = result["id"]
    if manifest["gmail"].get("label_id") != label_id:
        manifest["gmail"]["label_id"] = label_id
        if checkpoint is not None:
            checkpoint()
    return label_id


def _attachment_names(record: dict[str, Any]) -> list[str]:
    value = record.get("attachments")
    if isinstance(value, dict):
        return [str(name) for name in value]
    if isinstance(value, list):
        result = []
        for item in value:
            if isinstance(item, str):
                result.append(item)
            elif isinstance(item, dict):
                name = item.get("filename") or item.get("name") or item.get("path")
                if name:
                    result.append(str(name))
        return result
    return []


def build_message(
    record: dict[str, Any],
    *,
    attachment_index: dict[str, tuple[Path, str]],
    missing_policy: str,
) -> bytes:
    msg = EmailMessage(policy=SMTP)
    source_id = str(record["email_id"])
    msg["Message-ID"] = _message_id(source_id)
    parent = record.get("parent_id")
    if parent:
        parent_id = _message_id(str(parent))
        msg["In-Reply-To"] = parent_id
        msg["References"] = parent_id
    msg["From"] = str(record.get("sender") or "unknown@seed.invalid")
    recipients = record.get("recipients") or []
    if recipients:
        msg["To"] = ", ".join(str(value) for value in recipients)
    cc = record.get("cc") or []
    if cc:
        msg["Cc"] = ", ".join(str(value) for value in cc)
    msg["Subject"] = str(record.get("subject") or "")
    msg["Date"] = format_datetime(parse_datetime(record["timestamp"]))
    msg["X-GAB-Seed-ID"] = source_id
    msg.set_content(str(record.get("content") or ""))
    for original_name in _attachment_names(record):
        basename = PurePosixPath(original_name).name
        resolved = attachment_index.get(basename)
        if resolved is None:
            if missing_policy == "omit":
                continue
            raise AttachmentError(f"missing attachment blob: {basename}")
        path, mime = resolved
        maintype, subtype = (mime.split("/", 1) if "/" in mime else ("application", "octet-stream"))
        msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype, filename=basename)
    return msg.as_bytes()


def _ordered_messages(messages: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[str]]:
    by_id = {str(item["email_id"]): item for item in messages}
    placed: set[str] = set()
    remaining = set(by_id)
    ordered: list[dict[str, Any]] = []
    warnings: list[str] = []
    while remaining:
        ready = [
            source_id
            for source_id in remaining
            if not by_id[source_id].get("parent_id")
            or str(by_id[source_id].get("parent_id")) in placed
            or str(by_id[source_id].get("parent_id")) not in by_id
        ]
        if not ready:
            ready = sorted(remaining)
            warnings.append("thread parent cycle detected; fell back to source ID order")
        ready.sort(key=lambda source_id: parse_datetime(by_id[source_id]["timestamp"]))
        for source_id in ready:
            ordered.append(by_id[source_id])
            placed.add(source_id)
            remaining.remove(source_id)
    return ordered, warnings


def seed_gmail(
    *,
    archive: EnvironmentArchive,
    persona: str,
    gmail: Any | None,
    manifest: dict[str, Any],
    checkpoint: Callable[[], None],
    attachment_cache: Path,
    missing_policy: str,
    dry_run: bool,
) -> dict[str, Any]:
    attachment_index, missing, ambiguous = cache_attachments(
        archive=archive, persona=persona, destination=attachment_cache
    )
    if ambiguous:
        raise AttachmentError(f"ambiguous attachment basenames: {ambiguous}")
    if missing and missing_policy == "error":
        raise AttachmentError(f"missing attachment blobs: {missing}")
    manifest["warnings"].extend(f"missing attachment omitted: {name}" for name in missing)
    label_id = ensure_gmail_baseline_label(
        gmail,
        manifest,
        dry_run=dry_run,
        checkpoint=checkpoint,
    )
    if dry_run:
        manifest["gmail"]["label_id"] = label_id

    messages, warnings = _ordered_messages(archive.load_emails(persona))
    records_by_source = {str(item["email_id"]): item for item in messages}
    manifest["warnings"].extend(warnings)
    inserted = manifest["gmail"]["messages"]
    imported = 0
    raw_bytes = 0
    for record in messages:
        source_id = str(record["email_id"])
        if source_id in inserted:
            continue
        raw = build_message(
            record,
            attachment_index=attachment_index,
            missing_policy=missing_policy,
        )
        raw_bytes += len(raw)
        folder = str(record.get("folder") or "").upper()
        labels = [label_id]
        if folder in {"INBOX", "SENT", "TRASH", "SPAM", "IMPORTANT", "STARRED"}:
            labels.append(folder)
        if not bool(record.get("is_read", True)):
            labels.append("UNREAD")
        body: dict[str, Any] = {
            "raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
            "labelIds": labels,
        }
        parent = record.get("parent_id")
        parent_record = records_by_source.get(str(parent)) if parent else None
        if (
            parent
            and parent_record is not None
            and _gmail_thread_compatible(record, parent_record)
            and str(parent) in inserted
            and inserted[str(parent)].get("threadId")
        ):
            body["threadId"] = inserted[str(parent)]["threadId"]
        if dry_run:
            result = {
                "id": f"dry-message-{source_id}",
                "threadId": body.get("threadId", f"dry-thread-{source_id}"),
                "labelIds": labels,
            }
        else:
            _pace_gmail(25)
            result = (
                gmail.users()
                .messages()
                .import_(
                    userId="me",
                    body=body,
                    internalDateSource="dateHeader",
                    neverMarkSpam=True,
                    processForCalendar=False,
                )
                .execute()
            )
        inserted[source_id] = {
            "id": result["id"],
            "threadId": result.get("threadId"),
            "labelIds": labels,
        }
        imported += 1
        checkpoint()
    return {
        "imported_messages": imported,
        "manifest_messages": len(inserted),
        "raw_mime_bytes": raw_bytes,
        "missing_attachments": missing,
    }


def list_message_ids(gmail: Any, *, include_spam_trash: bool = True) -> list[str]:
    result: list[str] = []
    page_token = None
    while True:
        _pace_gmail(5)
        response = (
            gmail.users()
            .messages()
            .list(
                userId="me",
                includeSpamTrash=include_spam_trash,
                maxResults=500,
                pageToken=page_token,
            )
            .execute()
        )
        result.extend(item["id"] for item in response.get("messages", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return result


def list_draft_ids(gmail: Any) -> list[str]:
    result: list[str] = []
    page_token = None
    while True:
        _pace_gmail(5)
        response = (
            gmail.users()
            .drafts()
            .list(userId="me", maxResults=500, pageToken=page_token)
            .execute()
        )
        result.extend(item["id"] for item in response.get("drafts", []))
        page_token = response.get("nextPageToken")
        if not page_token:
            return result


def get_message_metadata(gmail: Any, message_id: str) -> dict[str, Any]:
    _pace_gmail(20)
    return (
        gmail.users()
        .messages()
        .get(
            userId="me",
            id=message_id,
            format="metadata",
            metadataHeaders=BASELINE_HEADER_NAMES,
        )
        .execute()
    )


def _batch_get_message_metadata(gmail: Any, message_ids: list[str]) -> list[dict[str, Any]]:
    if not message_ids:
        return []
    if not hasattr(gmail, "new_batch_http_request"):
        return [get_message_metadata(gmail, message_id) for message_id in message_ids]
    output: list[dict[str, Any] | None] = [None] * len(message_ids)
    users = gmail.users()
    messages = users.messages()
    for start in range(0, len(message_ids), GMAIL_METADATA_BATCH_SIZE):
        batch_ids = message_ids[start : start + GMAIL_METADATA_BATCH_SIZE]
        batch_errors: list[Exception] = []
        try:
            batch = gmail.new_batch_http_request()
        except Exception:
            return [get_message_metadata(gmail, message_id) for message_id in message_ids]

        def callback(request_id: str, response: Any, exception: Exception | None) -> None:
            index = int(request_id)
            if exception is None:
                output[index] = response or {}
                return
            status = getattr(getattr(exception, "resp", None), "status", None)
            if status == 404:
                output[index] = {}
                return
            batch_errors.append(exception)

        try:
            for offset, message_id in enumerate(batch_ids, start=start):
                request = messages.get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=BASELINE_HEADER_NAMES,
                )
                batch.add(request, request_id=str(offset), callback=callback)
            _pace_gmail(len(batch_ids) * 20)
            batch.execute()
        except (AttributeError, TypeError):
            return [get_message_metadata(gmail, message_id) for message_id in message_ids]
        if batch_errors:
            # A quota failure inside an HTTP batch must be retried after the
            # worker cooldown. Immediately replaying failed parts one by one
            # compounds the same quota breach.
            raise batch_errors[0]
    return [item for item in output if item]


def _batch_get_draft_metadata(gmail: Any, draft_ids: list[str]) -> list[dict[str, Any]]:
    if not draft_ids:
        return []

    def get_one(draft_id: str) -> dict[str, Any]:
        _pace_gmail(20)
        return (
            gmail.users()
            .drafts()
            .get(userId="me", id=draft_id, format="metadata")
            .execute()
        )

    if not hasattr(gmail, "new_batch_http_request"):
        return [get_one(draft_id) for draft_id in draft_ids]
    output: list[dict[str, Any] | None] = [None] * len(draft_ids)
    drafts = gmail.users().drafts()
    for start in range(0, len(draft_ids), GMAIL_METADATA_BATCH_SIZE):
        chunk = draft_ids[start : start + GMAIL_METADATA_BATCH_SIZE]
        batch_errors: list[Exception] = []
        try:
            batch = gmail.new_batch_http_request()
        except Exception:
            return [get_one(draft_id) for draft_id in draft_ids]

        def callback(request_id: str, response: Any, exception: Exception | None) -> None:
            index = int(request_id)
            status = getattr(getattr(exception, "resp", None), "status", None)
            if exception is None:
                output[index] = response or {}
            elif status == 404:
                output[index] = {}
            else:
                batch_errors.append(exception)

        try:
            for offset, draft_id in enumerate(chunk, start=start):
                batch.add(
                    drafts.get(userId="me", id=draft_id, format="metadata"),
                    request_id=str(offset),
                    callback=callback,
                )
            _pace_gmail(len(chunk) * 20)
            batch.execute()
        except (AttributeError, TypeError):
            return [get_one(draft_id) for draft_id in draft_ids]
        if batch_errors:
            raise batch_errors[0]
    return [item for item in output if item]


def gmail_inventory(gmail: Any) -> dict[str, Any]:
    messages = _batch_get_message_metadata(gmail, list_message_ids(gmail))
    drafts: list[dict[str, Any]] = []
    for draft in _batch_get_draft_metadata(gmail, list_draft_ids(gmail)):
        message = draft.get("message") or {}
        drafts.append({"id": draft.get("id"), "message": message, "message_id": message.get("id")})
    _pace_gmail(1)
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    return {
        "messages": messages,
        "drafts": drafts,
        "draft_message_ids": {str(draft.get("message_id")) for draft in drafts if draft.get("message_id")},
        "labels": labels,
    }


def _batch_delete_messages(gmail: Any, ids: list[str]) -> int:
    deleted = 0
    for start in range(0, len(ids), 1000):
        chunk = ids[start : start + 1000]
        if not chunk:
            continue
        _pace_gmail(50)
        gmail.users().messages().batchDelete(userId="me", body={"ids": chunk}).execute()
        deleted += len(chunk)
    return deleted


def _gmail_desired(
    *,
    archive: EnvironmentArchive,
    persona: str,
    manifest: dict[str, Any],
    label_id: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    manifest_messages = manifest.setdefault("gmail", {}).setdefault("messages", {})
    messages, warnings = _ordered_messages(archive.load_emails(persona))
    desired: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(messages):
        source_id = str(record["email_id"])
        entry = manifest_messages.get(source_id) or {}
        desired[source_id] = {
            "record": record,
            "order": index,
            "id": entry.get("id"),
            "threadId": entry.get("threadId"),
            "message_id": _message_id(source_id),
            "parent_id": str(record.get("parent_id")) if record.get("parent_id") else None,
            "labels": _expected_gmail_labels(record, label_id),
        }
    return desired, warnings


def plan_gmail_delta(
    *,
    archive: EnvironmentArchive,
    persona: str,
    gmail: Any | None,
    manifest: dict[str, Any],
) -> dict[str, Any]:
    label_id = manifest.setdefault("gmail", {}).get("label_id") or "dry-label"
    label_name = _baseline_label_name(manifest)
    desired, warnings = _gmail_desired(
        archive=archive, persona=persona, manifest=manifest, label_id=label_id
    )
    inventory = {"messages": [], "drafts": [], "draft_message_ids": set(), "labels": []} if gmail is None else gmail_inventory(gmail)
    by_id = {str(item.get("id")): item for item in inventory["messages"] if item.get("id")}
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_message_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inventory["messages"]:
        source = _header_value(item, "X-GAB-Seed-ID")
        message_id = _header_value(item, "Message-ID")
        if source:
            by_source[source].append(item)
        if message_id:
            by_message_id[message_id].append(item)

    user_labels = [item for item in inventory["labels"] if item.get("type") == "user"]
    baseline_label = next((item for item in user_labels if item.get("name") == label_name), None)
    actions: list[dict[str, Any]] = []
    adoptions: list[dict[str, Any]] = []
    problems: list[str] = []
    matched_ids: set[str] = set()
    canonical_by_source: dict[str, dict[str, Any]] = {}
    stats = {
        "unchanged": 0,
        "managed_label_drift": 0,
        "thread_drift": 0,
        "missing": 0,
        "duplicate_source_identity": 0,
        "extra_messages": 0,
        "extra_drafts": len(inventory["drafts"]),
        "extra_user_labels": 0,
        "warnings": len(warnings),
    }
    if baseline_label is None:
        actions.append({"action": "create_label", "name": label_name})
    elif label_id == "dry-label" or manifest["gmail"].get("label_id") != baseline_label.get("id"):
        label_id = baseline_label["id"]
        for item in desired.values():
            item["labels"] = _expected_gmail_labels(item["record"], label_id)

    for source_id, item in sorted(desired.items(), key=lambda pair: pair[1]["order"]):
        canonical = None
        manifest_id = item.get("id")
        if manifest_id and str(manifest_id) in by_id:
            canonical = by_id[str(manifest_id)]
        source_matches = [
            msg for msg in by_source.get(source_id, []) if msg.get("id") != manifest_id
        ]
        message_matches = [
            msg for msg in by_message_id.get(item["message_id"], []) if msg.get("id") != manifest_id
        ]
        identity_matches = {str(msg.get("id")): msg for msg in [*source_matches, *message_matches] if msg.get("id")}
        if canonical is None and len(identity_matches) == 1:
            canonical = next(iter(identity_matches.values()))
            identity_matches.pop(str(canonical.get("id")), None)
        elif canonical is None and len(identity_matches) > 1:
            stats["duplicate_source_identity"] += len(identity_matches)
            problems.append(f"ambiguous duplicate Gmail baseline identity for {source_id}")
            continue
        canonical_header_drift = False
        if canonical is not None:
            canonical_header_drift = (
                _header_value(canonical, "X-GAB-Seed-ID") != source_id
                or _header_value(canonical, "Message-ID") != item["message_id"]
            )
        if canonical is not None and identity_matches and canonical_header_drift:
            stats["duplicate_source_identity"] += len(identity_matches) + 1
            problems.append(f"ambiguous Gmail baseline identity for {source_id}; manifest ID headers drifted")
            continue
        elif canonical is not None and identity_matches:
            stats["duplicate_source_identity"] += len(identity_matches)
            for duplicate in sorted(identity_matches.values(), key=lambda msg: str(msg.get("id"))):
                actions.append({"action": "delete_message", "id": duplicate["id"], "reason": "duplicate"})

        if canonical is None:
            stats["missing"] += 1
            actions.append({"action": "import_message", "source_id": source_id})
            continue
        matched_ids.add(str(canonical["id"]))
        canonical_by_source[source_id] = canonical
        if item.get("id") != canonical.get("id"):
            adoptions.append(
                {
                    "source_id": source_id,
                    "id": canonical["id"],
                    "threadId": canonical.get("threadId"),
                    "labelIds": list(canonical.get("labelIds", [])),
                }
            )
        remote_labels = set(str(value) for value in canonical.get("labelIds", []))
        expected_labels = set(item["labels"])
        if canonical_header_drift:
            stats["managed_label_drift"] += 1
            actions.append({"action": "reimport_message", "id": canonical["id"], "source_id": source_id})
            continue
        remote_immutable = remote_labels & IMMUTABLE_SYSTEM_LABELS
        expected_immutable = expected_labels & IMMUTABLE_SYSTEM_LABELS
        if remote_immutable != expected_immutable:
            stats["managed_label_drift"] += 1
            actions.append({"action": "reimport_message", "id": canonical["id"], "source_id": source_id})
            continue
        parent_id = item.get("parent_id")
        parent_desired = desired.get(str(parent_id)) if parent_id else None
        if (
            parent_id
            and parent_desired is not None
            and _gmail_thread_compatible(item["record"], parent_desired["record"])
        ):
            parent = canonical_by_source.get(str(parent_id))
            parent_thread = str((parent or {}).get("threadId") or "")
            child_thread = str(canonical.get("threadId") or "")
            if parent_thread and parent_thread != child_thread:
                stats["thread_drift"] += 1
                actions.append(
                    {
                        "action": "reimport_message",
                        "id": canonical["id"],
                        "source_id": source_id,
                    }
                )
                continue
        managed_remote = {value for value in remote_labels if value == label_id or value in MUTABLE_SYSTEM_LABELS}
        mutable_expected = {value for value in expected_labels if value == label_id or value in MUTABLE_SYSTEM_LABELS}
        if managed_remote != mutable_expected:
            stats["managed_label_drift"] += 1
            actions.append({"action": "patch_labels", "id": canonical["id"], "source_id": source_id})
        else:
            stats["unchanged"] += 1

    desired_ids = {str(item.get("id")) for item in desired.values() if item.get("id")}
    for item in inventory["messages"]:
        message_id = str(item.get("id") or "")
        if not message_id or message_id in matched_ids:
            continue
        if message_id in desired_ids:
            continue
        if message_id in inventory.get("draft_message_ids", set()):
            continue
        stats["extra_messages"] += 1
        actions.append({"action": "delete_message", "id": message_id, "reason": "extra"})
    for draft in inventory["drafts"]:
        actions.append({"action": "delete_draft", "id": draft["id"]})
    for label in user_labels:
        name = str(label.get("name") or "")
        if name == label_name:
            continue
        stats["extra_user_labels"] += 1
        actions.append({"action": "delete_label", "id": label["id"], "name": name})

    reimport_sources = {
        str(item.get("source_id"))
        for item in actions
        if item.get("action") == "reimport_message" and item.get("source_id")
    }
    children: dict[str, list[str]] = defaultdict(list)
    compatible_parent: dict[str, str] = {}
    for source_id, item in desired.items():
        parent_id = str(item.get("parent_id") or "")
        parent = desired.get(parent_id)
        if (
            parent_id
            and parent is not None
            and _gmail_thread_compatible(item["record"], parent["record"])
        ):
            children[parent_id].append(source_id)
            compatible_parent[source_id] = parent_id
    missing_non_leaf_sources = {
        str(item.get("source_id"))
        for item in actions
        if item.get("action") == "import_message"
        and item.get("source_id")
        and children.get(str(item.get("source_id")))
    }
    thread_rebuild_sources = reimport_sources | missing_non_leaf_sources
    if thread_rebuild_sources:
        expanded = set(thread_rebuild_sources)
        queue = list(thread_rebuild_sources)
        while queue:
            source_id = queue.pop()
            parent_id = compatible_parent.get(source_id)
            if parent_id and parent_id not in expanded:
                expanded.add(parent_id)
                queue.append(parent_id)
            for child_id in children.get(source_id, []):
                if child_id not in expanded:
                    expanded.add(child_id)
                    queue.append(child_id)
        actions = [
            item
            for item in actions
            if item.get("action") not in {"reimport_message", "patch_labels", "import_message"}
            or item.get("source_id") not in expanded
        ]
        for source_id in expanded:
            canonical = canonical_by_source.get(source_id)
            if canonical is None:
                actions.append({"action": "import_message", "source_id": source_id})
            else:
                actions.append({"action": "reimport_message", "id": canonical["id"], "source_id": source_id})
    def action_sort(item: dict[str, Any]) -> tuple[int, int, str]:
        action = item["action"]
        source_id = str(item.get("source_id") or "")
        order = desired.get(source_id, {}).get("order", 999999)
        ranks = {
            "create_label": 0,
            "delete_message": 1,
            "delete_draft": 2,
            "delete_label": 3,
            "reimport_message": 4,
            "import_message": 4,
            "patch_labels": 6,
        }
        return (ranks.get(action, 99), int(order), str(item.get("name") or item.get("id") or source_id))

    actions.sort(key=action_sort)
    stats["writes"] = len(actions)
    return {
        "ok": not problems,
        "problems": problems,
        "actions": actions,
        "adoptions": sorted(adoptions, key=lambda item: item["source_id"]),
        "counts": stats,
        "desired": desired,
        "label_id": label_id,
        "warnings": warnings,
    }


def _import_one_message(
    gmail: Any,
    *,
    source_id: str,
    desired: dict[str, Any],
    all_desired: dict[str, dict[str, Any]],
    inserted: dict[str, Any],
    attachment_index: dict[str, tuple[Path, str]],
    missing_policy: str,
) -> dict[str, Any]:
    raw = build_message(
        desired["record"],
        attachment_index=attachment_index,
        missing_policy=missing_policy,
    )
    body: dict[str, Any] = {
        "raw": base64.urlsafe_b64encode(raw).decode("ascii").rstrip("="),
        "labelIds": desired["labels"],
    }
    parent = desired["record"].get("parent_id")
    parent_desired = all_desired.get(str(parent)) if parent else None
    if (
        parent
        and parent_desired is not None
        and _gmail_thread_compatible(desired["record"], parent_desired["record"])
        and str(parent) in inserted
        and inserted[str(parent)].get("threadId")
    ):
        body["threadId"] = inserted[str(parent)]["threadId"]
    _pace_gmail(25)
    return (
        gmail.users()
        .messages()
        .import_(
            userId="me",
            body=body,
            internalDateSource="dateHeader",
            neverMarkSpam=True,
            processForCalendar=False,
        )
        .execute()
    )


def reconcile_gmail_delta(
    *,
    archive: EnvironmentArchive,
    persona: str,
    gmail: Any | None,
    manifest: dict[str, Any],
    checkpoint: Callable[[], None],
    attachment_cache: Path,
    missing_policy: str,
    dry_run: bool,
) -> dict[str, Any]:
    attachment_index, missing, ambiguous = cache_attachments(
        archive=archive, persona=persona, destination=attachment_cache
    )
    if ambiguous:
        raise AttachmentError(f"ambiguous attachment basenames: {ambiguous}")
    if missing and missing_policy == "error":
        raise AttachmentError(f"missing attachment blobs: {missing}")
    plan = plan_gmail_delta(archive=archive, persona=persona, gmail=gmail, manifest=manifest)
    if not plan["ok"]:
        raise GmailDeltaSafetyError("; ".join(plan["problems"]) + "; explicit full reset required")
    summary = {
        "planned_writes": plan["counts"]["writes"],
        "manifest_updates": len(plan["adoptions"]),
        "counts": {**plan["counts"], "manifest_updates": len(plan["adoptions"])},
        "missing_attachments": missing,
        "dry_run": dry_run,
    }
    if dry_run:
        return {**summary, "actions": [item["action"] for item in plan["actions"]]}
    if gmail is None:
        raise RuntimeError("Gmail service is required for live delta reconcile")
    label_id = ensure_gmail_baseline_label(gmail, manifest, dry_run=False, checkpoint=checkpoint)
    if label_id != plan["label_id"]:
        plan = plan_gmail_delta(archive=archive, persona=persona, gmail=gmail, manifest=manifest)
    inserted = manifest.setdefault("gmail", {}).setdefault("messages", {})
    applied = 0
    manifest_updates = 0
    if plan["adoptions"]:
        for adoption in plan["adoptions"]:
            desired_labels = plan["desired"][adoption["source_id"]]["labels"]
            inserted[adoption["source_id"]] = {
                "id": adoption["id"],
                "threadId": adoption.get("threadId"),
                "labelIds": desired_labels,
            }
            manifest_updates += 1
        checkpoint()
    index = 0
    while index < len(plan["actions"]):
        action = plan["actions"][index]
        kind = action["action"]
        if kind == "create_label":
            ensure_gmail_baseline_label(gmail, manifest, dry_run=False, checkpoint=checkpoint)
        elif kind == "import_message":
            source_id = action["source_id"]
            result = _import_one_message(
                gmail,
                source_id=source_id,
                desired=plan["desired"][source_id],
                all_desired=plan["desired"],
                inserted=inserted,
                attachment_index=attachment_index,
                missing_policy=missing_policy,
            )
            inserted[source_id] = {
                "id": result["id"],
                "threadId": result.get("threadId"),
                "labelIds": plan["desired"][source_id]["labels"],
            }
            checkpoint()
        elif kind == "patch_labels":
            expected_labels = set(plan["desired"][action["source_id"]]["labels"])
            desired_labels = {
                value for value in expected_labels if value == label_id or value in MUTABLE_SYSTEM_LABELS
            }
            current = get_message_metadata(gmail, action["id"])
            remote = set(str(value) for value in current.get("labelIds", []))
            managed_remote = {value for value in remote if value == label_id or value in MUTABLE_SYSTEM_LABELS}
            _pace_gmail(5)
            gmail.users().messages().modify(
                userId="me",
                id=action["id"],
                body={
                    "addLabelIds": sorted(desired_labels - managed_remote),
                    "removeLabelIds": sorted(managed_remote - desired_labels),
                },
            ).execute()
            entry = inserted.setdefault(action["source_id"], {})
            entry.update({"id": action["id"], "labelIds": sorted(expected_labels)})
        elif kind == "reimport_message":
            source_id = action["source_id"]
            _pace_gmail(10)
            gmail.users().messages().delete(userId="me", id=action["id"]).execute()
            inserted.pop(source_id, None)
            result = _import_one_message(
                gmail,
                source_id=source_id,
                desired=plan["desired"][source_id],
                all_desired=plan["desired"],
                inserted=inserted,
                attachment_index=attachment_index,
                missing_policy=missing_policy,
            )
            inserted[source_id] = {
                "id": result["id"],
                "threadId": result.get("threadId"),
                "labelIds": plan["desired"][source_id]["labels"],
            }
            checkpoint()
        elif kind == "delete_message":
            ids = [action["id"]]
            index += 1
            while index < len(plan["actions"]) and plan["actions"][index]["action"] == "delete_message":
                ids.append(plan["actions"][index]["id"])
                index += 1
            try:
                applied += _batch_delete_messages(gmail, ids)
            except (AttributeError, TypeError):
                for message_id in ids:
                    _pace_gmail(10)
                    gmail.users().messages().delete(userId="me", id=message_id).execute()
                    applied += 1
            continue
        elif kind == "delete_draft":
            _pace_gmail(10)
            gmail.users().drafts().delete(userId="me", id=action["id"]).execute()
        elif kind == "delete_label":
            _pace_gmail(5)
            gmail.users().labels().delete(userId="me", id=action["id"]).execute()
        else:
            raise RuntimeError(f"unknown Gmail delta action: {kind}")
        applied += 1
        index += 1
    if applied and not plan["adoptions"]:
        checkpoint()
    return {**summary, "applied_writes": applied, "applied_manifest_updates": manifest_updates}


def verify_gmail(
    gmail: Any,
    manifest: dict[str, Any],
    *,
    archive: EnvironmentArchive | None = None,
    persona: str | None = None,
) -> dict[str, Any]:
    label_id = manifest["gmail"].get("label_id")
    inventory = gmail_inventory(gmail)
    by_id = {str(item.get("id")): item for item in inventory["messages"] if item.get("id")}
    if archive is not None and persona is not None:
        expected_messages, _warnings = _gmail_desired(
            archive=archive,
            persona=persona,
            manifest=manifest,
            label_id=label_id or "",
        )
    else:
        expected_messages = {
            source_id: {
                "id": entry.get("id"),
                "threadId": entry.get("threadId"),
                "labels": entry.get("labelIds", []),
                "message_id": _message_id(source_id),
                "parent_id": None,
            }
            for source_id, entry in manifest.get("gmail", {}).get("messages", {}).items()
        }
    expected = len(expected_messages)
    expected_ids = {str(entry.get("id")) for entry in expected_messages.values() if entry.get("id")}
    by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_message_id: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in inventory["messages"]:
        source = _header_value(item, "X-GAB-Seed-ID")
        message_id = _header_value(item, "Message-ID")
        if source:
            by_source[source].append(item)
        if message_id:
            by_message_id[message_id].append(item)
    duplicate_identities = 0
    for source_id in expected_messages:
        if len(by_source.get(source_id, [])) > 1:
            duplicate_identities += len(by_source[source_id])
    for message_id in {entry.get("message_id") for entry in expected_messages.values()}:
        if message_id and len(by_message_id.get(str(message_id), [])) > 1:
            duplicate_identities += len(by_message_id[str(message_id)])

    missing = []
    drifted = []
    source_threads: dict[str, str] = {}
    for source_id, entry in sorted(
        expected_messages.items(), key=lambda pair: int(pair[1].get("order", 999999))
    ):
        item = by_id.get(str(entry.get("id")))
        if item is None:
            missing.append(source_id)
            continue
        source_threads[source_id] = str(item.get("threadId") or "")
        # Imported message content is assumed immutable only while the stable
        # synthetic source headers remain exact. Header drift means the remote
        # message is no longer a trusted copy of the source MIME.
        if (
            _header_value(item, "X-GAB-Seed-ID") != source_id
            or _header_value(item, "Message-ID") != entry.get("message_id")
        ):
            drifted.append(source_id)
            continue
        if label_id and label_id not in set(item.get("labelIds", [])):
            drifted.append(source_id)
            continue
        expected_labels = set(str(value) for value in entry.get("labels", []))
        if expected_labels:
            remote_labels = set(str(value) for value in item.get("labelIds", []))
            managed_remote = {value for value in remote_labels if value == label_id or value in MUTABLE_SYSTEM_LABELS}
            immutable_remote = remote_labels & IMMUTABLE_SYSTEM_LABELS
            mutable_expected = {value for value in expected_labels if value == label_id or value in MUTABLE_SYSTEM_LABELS}
            immutable_expected = expected_labels & IMMUTABLE_SYSTEM_LABELS
            if managed_remote != mutable_expected or immutable_remote != immutable_expected:
                drifted.append(source_id)
                continue
        parent_id = entry.get("parent_id")
        parent_entry = expected_messages.get(str(parent_id)) if parent_id else None
        if (
            parent_id
            and parent_entry is not None
            and _gmail_thread_compatible(
                entry.get("record") or {}, parent_entry.get("record") or {}
            )
        ):
            parent_thread = source_threads.get(str(parent_id))
            if parent_thread and parent_thread != str(item.get("threadId") or ""):
                drifted.append(source_id)
    extras = [
        item.get("id")
        for item in inventory["messages"]
        if str(item.get("id") or "") not in expected_ids
        and str(item.get("id") or "") not in inventory.get("draft_message_ids", set())
    ]
    user_labels = [item for item in inventory["labels"] if item.get("type") == "user"]
    baseline_label = _baseline_label_name(manifest)
    extra_labels = [item for item in user_labels if item.get("name") != baseline_label]
    baseline_labels = [item for item in user_labels if item.get("name") == baseline_label]
    return {
        "expected_seeded_messages": expected,
        "remote_seeded_messages": len(expected_ids) - len(missing),
        "missing_seeded_messages": len(missing),
        "drifted_seeded_messages": len(drifted),
        "extra_messages": len(extras),
        "extra_drafts": len(inventory["drafts"]),
        "extra_user_labels": len(extra_labels),
        "duplicate_baseline_identities": duplicate_identities,
        "baseline_label_count": len(baseline_labels),
        "ok": (
            not missing
            and not drifted
            and not extras
            and not inventory["drafts"]
            and not extra_labels
            and duplicate_identities == 0
            and len(baseline_labels) == 1
        ),
    }


def reset_gmail_all(gmail: Any, *, dry_run: bool) -> dict[str, int]:
    message_ids = list_message_ids(gmail)
    _pace_gmail(1)
    labels = gmail.users().labels().list(userId="me").execute().get("labels", [])
    user_labels = [item for item in labels if item.get("type") == "user"]
    baseline_labels = [item for item in user_labels if str(item.get("name", "")).startswith("GAB_BASELINE_")]
    if not dry_run:
        for start in range(0, len(message_ids), 1000):
            batch = message_ids[start : start + 1000]
            _pace_gmail(50)
            gmail.users().messages().batchDelete(userId="me", body={"ids": batch}).execute()
        # The source baseline has no custom labels apart from its hidden seed
        # label. Remove any user labels Product A created as well.
        for label in user_labels:
            _pace_gmail(5)
            gmail.users().labels().delete(userId="me", id=label["id"]).execute()
    return {
        "found": len(message_ids),
        "deleted": len(message_ids),
        "baseline_labels_deleted": len(baseline_labels),
        "user_labels_deleted": len(user_labels),
    }

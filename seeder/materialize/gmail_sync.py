from __future__ import annotations

import base64
import time
from collections.abc import Callable
from datetime import datetime, timezone
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import format_datetime
from typing import Any

from googleapiclient.errors import HttpError

from materialize.fail import next_for

GAB_LABEL = "GAB-SEED"


def _retry(fn, log: Callable[[str], None], tries: int = 6):
    delay = 1.0
    for i in range(tries):
        try:
            return fn()
        except HttpError as exc:
            status = getattr(exc.resp, "status", None)
            if status in (403, 429, 500, 503) and i < tries - 1:
                log(f"Gmail API {status}, retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 30)
                continue
            raise


def ensure_label(gmail, log: Callable[[str], None]) -> str:
    existing = _retry(lambda: gmail.users().labels().list(userId="me").execute(), log)
    for lab in existing.get("labels", []):
        if lab.get("name") == GAB_LABEL:
            return lab["id"]
    created = _retry(
        lambda: gmail.users()
        .labels()
        .create(
            userId="me",
            body={
                "name": GAB_LABEL,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        )
        .execute(),
        log,
    )
    return created["id"]


def _normalize_msgid(value: str) -> str:
    raw = (value or "").strip().strip("<>")
    if raw.endswith("@gab.ultraevals.local"):
        return raw.rsplit("@", 1)[0]
    return raw.lower()


def list_seeded_mail(gmail, label_id: str, log: Callable[[str], None]) -> dict[str, dict[str, str]]:
    """email_id / Message-ID → {id, threadId} for GAB-SEED messages already in the mailbox."""
    found: dict[str, dict[str, str]] = {}
    page = None
    ids: list[str] = []
    while True:
        resp = _retry(
            lambda: gmail.users()
            .messages()
            .list(userId="me", labelIds=[label_id], maxResults=500, pageToken=page)
            .execute(),
            log,
        )
        ids.extend(m["id"] for m in resp.get("messages") or [])
        page = resp.get("nextPageToken")
        if not page:
            break
    for i in range(0, len(ids), 40):
        chunk = ids[i : i + 40]
        for mid in chunk:
            msg = _retry(
                lambda m=mid: gmail.users()
                .messages()
                .get(
                    userId="me",
                    id=m,
                    format="metadata",
                    metadataHeaders=["Message-ID", "Subject"],
                )
                .execute(),
                log,
            )
            headers = {h["name"].lower(): h["value"] for h in (msg.get("payload") or {}).get("headers") or []}
            key = _normalize_msgid(headers.get("message-id") or "")
            meta = {"id": msg["id"], "threadId": msg.get("threadId") or msg["id"]}
            if key:
                found[key] = meta
            subj = (headers.get("subject") or "").strip().lower()
            size = str(msg.get("sizeEstimate") or "")
            if subj:
                found[f"subj:{subj}|{size}"] = meta
    log(f"Gmail already has {len(ids)} seeded messages; matching Message-ID/subject+size will be skipped")
    return found


def wipe_seeded_mail(gmail, log: Callable[[str], None]) -> int:
    """FULL wipe: permanently delete ALL mail (no marker match).

    The platform authorizes accounts with full https://mail.google.com/ scope
    (see materialize/auth.py), so we hard-delete via batchDelete (1000/chunk)
    rather than moving to Trash. Loop-until-empty: deleted messages drop out
    of the listing, so we re-list until none remain.
    """
    deleted = 0
    while True:
        resp = _retry(
            lambda: gmail.users()
            .messages()
            .list(userId="me", maxResults=500, includeSpamTrash=False)
            .execute(),
            log,
        )
        ids = [m["id"] for m in resp.get("messages", [])]
        if not ids:
            break
        for start in range(0, len(ids), 1000):
            chunk = ids[start : start + 1000]
            _retry(
                lambda c=chunk: gmail.users()
                .messages()
                .batchDelete(userId="me", body={"ids": c})
                .execute(),
                log,
            )
            deleted += len(chunk)
        log(f"Deleted {deleted} messages so far (full wipe)")
    log(f"Full Gmail wipe: permanently deleted {deleted} messages")
    return deleted


def _payload_is_inline_file(payload: str) -> bool:
    text = (payload or "").strip()
    if len(text) < 80:
        return False
    if " " in text[:80]:
        return False
    try:
        raw = base64.b64decode(text, validate=False)
    except Exception:
        return False
    return len(raw) >= 32


def _attachment_bytes(name: str, payload: str, file_index: dict[str, bytes]) -> bytes | None:
    base = str(name).replace("\\", "/").rsplit("/", 1)[-1]
    if _payload_is_inline_file(payload):
        return base64.b64decode(payload, validate=False)
    for key in (name, base):
        if key in file_index:
            return file_index[key]
    if payload:
        try:
            raw = base64.b64decode(payload, validate=False)
            if raw:
                return raw
        except Exception:
            pass
    return None


def _build_raw(
    item: dict[str, Any],
    file_index: dict[str, bytes],
) -> tuple[str, int, list[str]]:
    recipients = item.get("recipients") or []
    if isinstance(recipients, str):
        recipients = [recipients]
    cc = item.get("cc") or []
    if isinstance(cc, str):
        cc = [cc]

    attachments = item.get("attachments") or {}
    if not isinstance(attachments, dict):
        attachments = {}

    try:
        ts = float(item.get("timestamp") or 0)
        if ts > 1e12:
            ts /= 1000.0
    except (TypeError, ValueError):
        ts = 0.0
    date_hdr = format_datetime(datetime.fromtimestamp(ts, tz=timezone.utc), usegmt=True)
    msg_id = f"<{item.get('email_id')}@gab.ultraevals.local>"

    omitted: list[str] = []
    attached = 0
    if attachments:
        msg = MIMEMultipart()
        msg.attach(MIMEText(item.get("content") or "", "plain", "utf-8"))
        for filename, payload in attachments.items():
            raw_att = _attachment_bytes(str(filename), payload or "", file_index)
            if not raw_att:
                omitted.append(str(filename))
                continue
            part = MIMEBase("application", "octet-stream")
            part.set_payload(raw_att)
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", "attachment", filename=str(filename))
            msg.attach(part)
            attached += 1
        if attached == 0:
            msg = MIMEText(item.get("content") or "", "plain", "utf-8")
    else:
        msg = MIMEText(item.get("content") or "", "plain", "utf-8")

    msg["From"] = item.get("sender") or ""
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = item.get("subject") or ""
    msg["Date"] = date_hdr
    msg["Message-ID"] = msg_id
    if cc:
        msg["Cc"] = ", ".join(cc)
    parent = item.get("parent_id")
    if parent:
        parent_mid = f"<{parent}@gab.ultraevals.local>"
        msg["In-Reply-To"] = parent_mid
        msg["References"] = parent_mid

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    internal_ms = int(ts * 1000)
    return raw, internal_ms, omitted


def trash_seeded_message(gmail, msg_id: str, log: Callable[[str], None]) -> None:
    _retry(
        lambda: gmail.users().messages().trash(userId="me", id=msg_id).execute(),
        log,
    )


def populate_gmail(
    gmail,
    data: dict[str, Any],
    log: Callable[[str], None],
    file_index: dict[str, bytes] | None = None,
    only_ids: set[str] | None = None,
    replace_attachments: bool = False,
) -> int:
    all_emails = list(data.get("emails") or [])
    emails = all_emails
    if only_ids is not None:
        emails = [item for item in all_emails if isinstance(item, dict) and item.get("email_id") in only_ids]
        log(
            f"{'Replacing attachments on' if replace_attachments else 'Retrying'} "
            f"{len(emails)} Gmail messages"
        )
    file_index = file_index or {}
    label_id = ensure_label(gmail, log)
    already = list_seeded_mail(gmail, label_id, log)
    by_id = {e.get("email_id"): e for e in all_emails if isinstance(e, dict) and e.get("email_id")}
    created_meta: dict[str, dict[str, str]] = {}
    created = 0

    skipped = 0

    visiting: set[str] = set()

    def process(item: dict[str, Any]) -> None:
        nonlocal created, skipped
        if not isinstance(item, dict):
            skipped += 1
            return
        eid = item.get("email_id") or f"generated-{id(item)}"
        if eid in created_meta:
            return
        if eid in visiting:
            skipped += 1
            log(f"Skip email {eid}: circular parent_id. next={next_for('gmail', 'circular parent')}")
            return
        visiting.add(eid)
        parent_id = item.get("parent_id")
        if parent_id and parent_id in by_id:
            process(by_id[parent_id])
        hit = already.get(str(eid)) or already.get(_normalize_msgid(str(eid)))
        atts = item.get("attachments")
        if hit and replace_attachments and atts:
            try:
                trash_seeded_message(gmail, hit["id"], log)
                log(f"Trashed placeholder-attachment email {eid} so it can be reinserted")
            except Exception as exc:
                log(f"Could not trash email {eid}: {exc}")
            already.pop(str(eid), None)
            already.pop(_normalize_msgid(str(eid)), None)
            created_meta.pop(eid, None)
            hit = None
        if hit:
            created_meta[eid] = hit
            skipped += 1
            visiting.discard(eid)
            return
        try:
            raw, _internal_ms, omitted = _build_raw(item, file_index)
            if omitted:
                log(
                    f"Email {eid}: sent without missing attachments "
                    + ", ".join(omitted)
                )
            folder = str(item.get("folder") or "INBOX").upper()
            labels = [label_id]
            if folder == "SENT":
                labels.append("SENT")
            else:
                labels.append("INBOX")
            if folder != "SENT" and not item.get("is_read", True):
                labels.append("UNREAD")
            body: dict[str, Any] = {"raw": raw, "labelIds": labels}
            parent_meta = created_meta.get(parent_id or "")
            if parent_meta:
                body["threadId"] = parent_meta["threadId"]
            result = _retry(
                lambda b=body: gmail.users()
                .messages()
                .insert(userId="me", body=b, internalDateSource="dateHeader")
                .execute(),
                log,
            )
            created_meta[eid] = {
                "id": result["id"],
                "threadId": result.get("threadId") or result["id"],
            }
            created += 1
        except RecursionError:
            skipped += 1
            log(f"Skip email {eid}: circular parent_id. next={next_for('gmail', 'circular parent')}")
        except HttpError as exc:
            skipped += 1
            log(f"Skip email {eid} thread/insert: {exc}. next={next_for('gmail', str(exc))}")
        except Exception as exc:
            skipped += 1
            log(f"Skip email {eid}: {exc}. next={next_for('gmail', str(exc))}")
        finally:
            visiting.discard(eid)

    for i, item in enumerate(emails, 1):
        process(item)
        if i % 20 == 0 or i == len(emails):
            log(f"Gmail {i}/{len(emails)} (ok {created}, skipped {skipped})")
    log(f"Inserted {created} Gmail messages, skipped {skipped}")
    return created

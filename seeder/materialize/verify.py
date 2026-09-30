from __future__ import annotations

from collections.abc import Callable
from typing import Any

from materialize.auth import build_service
from materialize.drive_sync import SEED_FOLDER, find_seed_folder, _retry as _drive_retry
from materialize.gmail_sync import GAB_LABEL, _retry as _gmail_retry
from materialize.calendar_sync import SEED_PROP, _retry as _cal_retry

# Verification is authoritative (its result gates whether a reset is reported clean), so
# its read calls MUST NOT fail on a single transient 429/5xx/network stall — that would
# make a good reset look dirty (or, worse, silently skip verification). Every .execute()
# below is wrapped in the same per-service retry the seed/reconcile writes already use.


def _count_calendar(calendar, log: Callable[[str], None]) -> int:
    total = 0
    page = None
    while True:
        resp = _cal_retry(
            lambda: calendar.events()
            .list(
                calendarId="primary",
                privateExtendedProperty=f"{SEED_PROP}=true",
                maxResults=250,
                pageToken=page,
                showDeleted=False,
            )
            .execute(),
            log,
        )
        total += len(resp.get("items") or [])
        page = resp.get("nextPageToken")
        if not page:
            break
    return total


def _gmail_label_id(gmail, log: Callable[[str], None]) -> str | None:
    resp = _gmail_retry(lambda: gmail.users().labels().list(userId="me").execute(), log)
    for lab in resp.get("labels") or []:
        if lab.get("name") == GAB_LABEL:
            return lab["id"]
    return None


def _live_gmail_message_ids(gmail, log: Callable[[str], None]) -> set:
    out: set = set()
    page = None
    while True:
        resp = _gmail_retry(
            lambda: gmail.users()
            .messages()
            .list(userId="me", maxResults=500, includeSpamTrash=False, pageToken=page)
            .execute(),
            log,
        )
        out |= {m["id"] for m in resp.get("messages") or [] if m.get("id")}
        page = resp.get("nextPageToken")
        if not page:
            break
    return out


def _count_gmail(gmail, log: Callable[[str], None], baseline_ids: set | None = None) -> int:
    # Manifest-based (preferred): how many seeded messages (by google_object_id) are
    # actually live. Label-independent, so an agent reply that inherited GAB-SEED can't
    # inflate the count. Falls back to the GAB-SEED label only when no manifest is given.
    if baseline_ids is not None:
        return len(_live_gmail_message_ids(gmail, log) & set(baseline_ids))
    label_id = _gmail_label_id(gmail, log)
    if not label_id:
        return 0
    total = 0
    page = None
    while True:
        resp = _gmail_retry(
            lambda: gmail.users()
            .messages()
            .list(userId="me", labelIds=[label_id], maxResults=500, pageToken=page)
            .execute(),
            log,
        )
        total += len(resp.get("messages") or [])
        page = resp.get("nextPageToken")
        if not page:
            break
    return total


def _count_drive(drive, folder_id: str | None, log: Callable[[str], None]) -> int:
    # Generated files now live directly in My Drive (no GAB_UltraEvals wrapper), so with no
    # explicit folder we count from the My Drive root, skipping the Github folder + zip.
    count = 0
    folders = [folder_id or "root"]
    while folders:
        fid = folders.pop()
        page = None
        while True:
            resp = _drive_retry(
                lambda: drive.files()
                .list(
                    q=f"'{fid}' in parents and trashed = false",
                    fields="nextPageToken, files(id, mimeType, name)",
                    pageSize=100,
                    pageToken=page,
                )
                .execute(),
                log,
            )
            for item in resp.get("files") or []:
                if item.get("mimeType") == "application/vnd.google-apps.folder":
                    if item.get("name") == "Github":
                        continue
                    folders.append(item["id"])
                elif item.get("name") == "github-repo-snapshot.zip":
                    continue
                else:
                    count += 1
            page = resp.get("nextPageToken")
            if not page:
                break
    return count


def verify_seed(
    creds,
    *,
    persona: str,
    expect_calendar: int | None,
    expect_gmail: int | None,
    expect_drive: int | None,
    folder_id: str | None,
    log: Callable[[str], None],
    calendar=None,
    gmail=None,
    drive=None,
    drive_ineligible: int | None = None,
    gmail_baseline: set | None = None,
) -> dict[str, Any]:
    calendar = calendar or build_service("calendar", "v3", creds)
    gmail = gmail or build_service("gmail", "v1", creds)
    drive = drive or build_service("drive", "v3", creds)
    if not folder_id:
        folder_id = find_seed_folder(drive, persona, log)

    got_cal = _count_calendar(calendar, log) if expect_calendar is not None else None
    # Gmail: count by manifest ids when available (label-independent), else by GAB-SEED label.
    got_mail = _count_gmail(gmail, log, gmail_baseline) if expect_gmail is not None else None
    got_drive = _count_drive(drive, folder_id, log) if expect_drive is not None else None

    def tone(got: int | None, expect: int | None) -> str:
        if got is None or expect is None:
            return "skip"
        if got == 0 and expect > 0:
            return "err"
        if got == expect:
            return "ok"
        return "warn"

    modules = {
        "calendar": {"got": got_cal, "expect": expect_calendar, "tone": tone(got_cal, expect_calendar)},
        "gmail": {"got": got_mail, "expect": expect_gmail, "tone": tone(got_mail, expect_gmail)},
        "drive": {
            "got": got_drive,
            "expect": expect_drive,
            "tone": tone(got_drive, expect_drive),
            "ineligible": drive_ineligible,
        },
    }
    checked = [m for m in modules.values() if m["expect"] is not None]
    if not checked:
        overall = "ok"
    elif all(m["tone"] == "ok" for m in checked):
        overall = "ok"
    elif any(m["tone"] == "err" for m in checked):
        overall = "failed"
    else:
        overall = "partial"
    log(
        "Verify "
        + " · ".join(
            f"{k} {m['got']}/{m['expect']}"
            for k, m in modules.items()
            if m["expect"] is not None
        )
    )
    return {"overall": overall, "modules": modules, "folder_id": folder_id, "seed_folder": f"{SEED_FOLDER}__{persona}"}

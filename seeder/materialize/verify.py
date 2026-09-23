from __future__ import annotations

from collections.abc import Callable
from typing import Any

from materialize.auth import build_service
from materialize.drive_sync import SEED_FOLDER, find_seed_folder
from materialize.gmail_sync import GAB_LABEL
from materialize.calendar_sync import SEED_PROP


def _count_calendar(calendar) -> int:
    total = 0
    page = None
    while True:
        resp = (
            calendar.events()
            .list(
                calendarId="primary",
                privateExtendedProperty=f"{SEED_PROP}=true",
                maxResults=250,
                pageToken=page,
                showDeleted=False,
            )
            .execute()
        )
        total += len(resp.get("items") or [])
        page = resp.get("nextPageToken")
        if not page:
            break
    return total


def _gmail_label_id(gmail) -> str | None:
    labels = gmail.users().labels().list(userId="me").execute().get("labels") or []
    for lab in labels:
        if lab.get("name") == GAB_LABEL:
            return lab["id"]
    return None


def _count_gmail(gmail) -> int:
    label_id = _gmail_label_id(gmail)
    if not label_id:
        return 0
    total = 0
    page = None
    while True:
        resp = (
            gmail.users()
            .messages()
            .list(userId="me", labelIds=[label_id], maxResults=500, pageToken=page)
            .execute()
        )
        total += len(resp.get("messages") or [])
        page = resp.get("nextPageToken")
        if not page:
            break
    return total


def _count_drive(drive, folder_id: str | None) -> int:
    if not folder_id:
        return 0
    count = 0
    folders = [folder_id]
    while folders:
        fid = folders.pop()
        page = None
        while True:
            resp = (
                drive.files()
                .list(
                    q=f"'{fid}' in parents and trashed = false",
                    fields="nextPageToken, files(id, mimeType, name)",
                    pageSize=100,
                    pageToken=page,
                )
                .execute()
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
) -> dict[str, Any]:
    calendar = calendar or build_service("calendar", "v3", creds)
    gmail = gmail or build_service("gmail", "v1", creds)
    drive = drive or build_service("drive", "v3", creds)
    if not folder_id:
        folder_id = find_seed_folder(drive, persona, log)

    got_cal = _count_calendar(calendar) if expect_calendar is not None else None
    got_mail = _count_gmail(gmail) if expect_gmail is not None else None
    got_drive = _count_drive(drive, folder_id) if expect_drive is not None else None

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

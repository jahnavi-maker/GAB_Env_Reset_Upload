from __future__ import annotations

import json
from pathlib import Path
from typing import Any


KINDS = ("calendar", "gmail", "filesystem")


def detect_kind(data: Any) -> str | None:
    if isinstance(data, list):
        if not data:
            return None
        first = data[0] if isinstance(data[0], dict) else {}
        if "start_datetime" in first or "start" in first or "summary" in first:
            return "calendar"
        if "sender" in first or "subject" in first or "folder" in first:
            return "gmail"
        if "path" in first or "mime_type" in first:
            return "filesystem"
        return None
    if not isinstance(data, dict):
        return None
    if "events" in data:
        return "calendar"
    if "emails" in data:
        return "gmail"
    if "files" in data or "directories" in data:
        return "filesystem"
    return None


def normalize(kind: str, data: Any) -> dict[str, Any]:
    if kind == "calendar":
        if isinstance(data, list):
            return {"events": [x for x in data if isinstance(x, dict)]}
        events = data.get("events")
        if events is None:
            events = []
        if not isinstance(events, list):
            raise ValueError("calendar JSON: 'events' must be a list")
        return {"events": [x for x in events if isinstance(x, dict)]}
    if kind == "gmail":
        if isinstance(data, list):
            return {"emails": [x for x in data if isinstance(x, dict)]}
        emails = data.get("emails")
        if emails is None:
            emails = []
        if not isinstance(emails, list):
            raise ValueError("gmail JSON: 'emails' must be a list")
        out = {"emails": [x for x in emails if isinstance(x, dict)]}
        if data.get("user_email"):
            out["user_email"] = data["user_email"]
        return out
    if kind == "filesystem":
        if isinstance(data, list):
            return {"directories": [], "files": [x for x in data if isinstance(x, dict)]}
        files = data.get("files") or []
        dirs = data.get("directories") or []
        if not isinstance(files, list):
            raise ValueError("filesystem JSON: 'files' must be a list")
        return {
            "directories": dirs if isinstance(dirs, list) else [],
            "files": [x for x in files if isinstance(x, dict)],
        }
    raise ValueError(f"unknown kind {kind}")


def load_json_file(path: Path) -> tuple[Any | None, str | None]:
    if not path.exists():
        return None, f"File not found: {path}"
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh), None
    except json.JSONDecodeError as exc:
        return None, (
            f"Invalid JSON in {path.name}: line {exc.lineno} column {exc.colno} — {exc.msg}"
        )
    except UnicodeDecodeError:
        try:
            with path.open("r", encoding="utf-8-sig") as fh:
                return json.load(fh), None
        except Exception as exc:
            return None, f"Could not decode {path.name} as UTF-8 ({exc})"
    except MemoryError:
        return None, f"{path.name} is too large to load into memory"
    except OSError as exc:
        return None, f"Could not read {path.name}: {exc}"


def inspect_and_normalize(path: Path, expected: str | None = None) -> dict[str, Any]:
    data, err = load_json_file(path)
    if err:
        return {"ok": False, "error": err, "kind": expected}
    kind = detect_kind(data)
    if expected and kind and kind != expected:
        return {
            "ok": False,
            "error": (
                f"{path.name} looks like a {kind} dump, not {expected}. "
                "Drop it on the matching card."
            ),
            "kind": kind,
        }
    use_kind = expected or kind
    if not use_kind:
        return {
            "ok": False,
            "error": (
                f"{path.name} is JSON but not a GAB dump. "
                "Calendar needs 'events', Gmail needs 'emails', Drive needs 'files'."
            ),
            "kind": None,
        }
    try:
        normalized = normalize(use_kind, data)
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "kind": use_kind}
    counts = {
        "calendar": len(normalized.get("events") or []),
        "gmail": len(normalized.get("emails") or []),
        "filesystem": len(normalized.get("files") or []),
    }
    return {
        "ok": True,
        "kind": use_kind,
        "count": counts.get(use_kind, 0),
        "user_email": normalized.get("user_email"),
        "data": normalized,
    }

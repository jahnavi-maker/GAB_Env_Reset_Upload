"""Account registration helpers: CSV ingest and Google client-id resolution."""
from __future__ import annotations

import csv
import io
import os
import re
from pathlib import Path
from typing import Any

from .config import settings

EMAIL_HEADERS = (
    "gmail",
    "google account",
    "account",
    "email",
    "email-id",
    "email_id",
    "email id",
)
PASSWORD_HEADERS = ("password", "pass", "pwd")
ROLE_HEADERS = (
    "persona loaded",
    "benchmark persona/profile",
    "benchmark persona",
    "personas",
    "persona",
    "role",
    "profile",
)

NAME_HEADERS = ("name", "full name", "fullname")

LOGIN_CSV_FORMAT = {
    "required": "email",
    "example": "operator@company.com\nrater@deccan.ai\n",
    "notes": [
        "One email per line, or a CSV whose first column is email.",
        "These emails may sign in on /reset. Stored in the freelancers table when Supabase is set.",
    ],
}

CSV_FORMAT = {
    "required": "email, persona",
    "optional": "password",
    "example": (
        "email,password,persona\n"
        "user410@gmail.com,secret410,Student\n"
        "test02gemini@gmail.com,secret,backend_software_engineer\n"
    ),
}


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("\ufeff", "").strip().lower())


def normalize_persona_key(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _looks_like_email(value: str) -> bool:
    return "@" in value and "." in value.split("@")[-1]


def _pick(row: dict[str, str], aliases: tuple[str, ...]) -> str:
    by_norm = {_norm_header(key): (value or "").strip() for key, value in row.items()}
    for name in aliases:
        if by_norm.get(name):
            return by_norm[name]
    return ""


def parse_account_csv(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Return (rows, errors). Each row is {email, persona, password}."""
    text = None
    for codec in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(codec)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("latin-1", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return [], ["CSV has no header row. Use: email,persona,password"]
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for i, row in enumerate(reader, start=2):
        email = _pick(row, EMAIL_HEADERS).lower()
        persona = _pick(row, ROLE_HEADERS)
        password = _pick(row, PASSWORD_HEADERS)
        if not email and not persona:
            continue
        if not email or not _looks_like_email(email):
            errors.append(f"row {i}: missing or invalid email")
            continue
        if not persona:
            errors.append(f"row {i}: missing persona")
            continue
        rows.append({"email": email, "persona": persona, "password": password})
    return rows, errors


def parse_login_csv(raw: bytes) -> tuple[list[dict[str, str]], list[str]]:
    """Return (rows, errors). Email only — extra columns are ignored."""
    text = None
    for codec in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            text = raw.decode(codec)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = raw.decode("latin-1", errors="replace")
    rows: list[dict[str, str]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for i, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line:
            continue
        parts = next(csv.reader([line]), [])
        cell = (parts[0] if parts else "").strip().strip('"').lower()
        if i == 1 and _norm_header(cell) in EMAIL_HEADERS:
            continue
        if not _looks_like_email(cell):
            errors.append(f"row {i}: not an email")
            continue
        if cell in seen:
            continue
        seen.add(cell)
        rows.append({"email": cell})
    return rows, errors


def public_account(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "email": row.get("email"),
        "persona": row.get("persona"),
        "last_reset_persona": row.get("last_reset_persona"),
        "last_reset_mode": row.get("last_reset_mode"),
        "authorized": bool(row.get("authorized")),
        "status": row.get("status") or "active",
        "last_reset_at": row.get("last_reset_at"),
    }


def _client_id_from_seeder_credentials() -> str:
    path = Path(settings.seeder_dir).expanduser() / "credentials.json"
    try:
        data = __import__("json").loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    web = data.get("web") if isinstance(data, dict) else None
    if not isinstance(web, dict):
        return ""
    return str(web.get("client_id") or "").strip()


def resolve_google_client_id() -> str:
    """Env, then settings (tests patch this), then seeder credentials.json."""
    env_val = (os.environ.get("GOOGLE_CLIENT_ID") or "").strip() if "GOOGLE_CLIENT_ID" in os.environ else ""
    if env_val:
        return env_val
    settings_val = (getattr(settings, "google_client_id", None) or "").strip()
    if settings_val:
        return settings_val
    if "GOOGLE_CLIENT_ID" in os.environ:
        return ""
    return _client_id_from_seeder_credentials()

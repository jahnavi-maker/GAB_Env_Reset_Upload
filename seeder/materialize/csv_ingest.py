from __future__ import annotations

import csv
import io
import re
from typing import Any

from materialize.runstate import persona_folders, validate_persona_files

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


def _norm_header(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").replace("\ufeff", "").strip().lower())


def normalize_persona_key(value: str) -> str:
    text = (value or "").lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def _pick(row: dict[str, str], aliases: tuple[str, ...] | set[str]) -> str:
    by_norm = {_norm_header(key): (value or "").strip() for key, value in row.items()}
    order = aliases if isinstance(aliases, tuple) else tuple(aliases)
    for name in order:
        if by_norm.get(name):
            return by_norm[name]
    return ""


def _decode_csv(raw: bytes) -> tuple[str, str]:
    for codec in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return raw.decode(codec), codec
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1", errors="replace"), "latin-1"


def _looks_like_email(value: str) -> bool:
    if "@" not in value:
        return False
    local, _, domain = value.partition("@")
    return bool(local) and "." in domain


def parse_accounts_csv(raw: bytes) -> dict[str, Any]:
    text, codec = _decode_csv(raw)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if reader.fieldnames is None:
        raise ValueError("CSV has no header row")
    normalized_headers = {_norm_header(h) for h in reader.fieldnames}
    if not normalized_headers.intersection(EMAIL_HEADERS):
        raise ValueError(
            "CSV needs an email column. Accepted headers: email, email-id, Google account, account, gmail"
        )

    has_password_column = any(_norm_header(h) in set(PASSWORD_HEADERS) for h in (reader.fieldnames or []))
    folders = persona_folders()
    folder_by_key = {normalize_persona_key(name): name for name in folders}
    seen: dict[tuple[str, str], int] = {}
    accounts: list[dict[str, Any]] = []
    warnings: list[str] = []
    cache: dict[str, dict[str, Any]] = {}
    if codec != "utf-8-sig":
        warnings.append(f"CSV decoded as {codec} (not UTF-8)")

    for index, row in enumerate(reader, start=2):
        email = _pick(row, EMAIL_HEADERS).lower()
        if not email:
            continue
        if not _looks_like_email(email):
            warnings.append(f"Row {index}: skipped non-email value {email!r}")
            continue
        role = _pick(row, ROLE_HEADERS)
        key = normalize_persona_key(role)
        pair = (email, key)
        if pair in seen:
            label = role or "(no persona)"
            warnings.append(
                f"Duplicate {email} + {label} on row {index}; keeping first (row {seen[pair]})"
            )
            continue
        seen[pair] = index
        matched = folder_by_key.get(key)
        files = validate_persona_files(matched, cache) if matched else None
        accounts.append(
            {
                "email": email,
                "persona_raw": role,
                "persona_key": key,
                "persona_dir": matched,
                "persona_status": "matched" if matched else "unmatched",
                "persona_files": files,
                "auth": {
                    "backend": "consumer_oauth",
                    "state": "none",
                    "verified_email": None,
                    "expires_at": None,
                    "got_email": None,
                },
                "drops": {"calendar": False, "gmail": False, "filesystem": False},
                "push": {"state": "idle", "last_run": None},
                "github": {"repo_url": None, "state": "none"},
            }
        )
        if not matched and role:
            warnings.append(f"{email}: unmatched persona {role!r}")
        elif files and not files.get("ok"):
            warnings.append(f"{email}: persona files failed validation")

    if not accounts:
        raise ValueError(
            "CSV contains no valid account rows. Add at least one complete email address and try again."
        )
    if len(accounts) > 100:
        warnings.append(
            f"{len(accounts)} accounts loaded. Consumer OAuth Testing allows 100 test users. "
            "Workspace domain-wide delegation can push the full list with Push all."
        )

    return {
        "accounts": accounts,
        "warnings": warnings,
        "folders": folders,
        "has_passwords": has_password_column,
        "codec": codec,
    }

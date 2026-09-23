from __future__ import annotations

import base64
import binascii
import hashlib
import json
import mimetypes
import os
import re
import zipfile
from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Iterator

import ijson

ROOT = "PKJA_UltraEvals_Environments_"
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_B64_RE = re.compile(r"^[A-Za-z0-9+/]*={0,2}$")


class ArchiveError(RuntimeError):
    pass


def safe_relpath(value: str) -> str:
    value = value.replace("\\", "/").lstrip("/")
    path = PurePosixPath(value)
    if any(part in {"", ".", ".."} for part in path.parts):
        raise ArchiveError(f"unsafe archive path: {value!r}")
    return str(path)


def parse_datetime(value: Any) -> datetime:
    if isinstance(value, (int, float, Decimal)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            raise ArchiveError(f"timestamp has no timezone: {value!r}")
        return dt.astimezone(timezone.utc)
    raise ArchiveError(f"unsupported timestamp: {value!r}")


def rfc3339(value: Any) -> str:
    return parse_datetime(value).isoformat().replace("+00:00", "Z")


def valid_email(value: Any) -> bool:
    return isinstance(value, str) and bool(_EMAIL_RE.fullmatch(value.strip()))


def classify_content(record: dict[str, Any]) -> str:
    content = record.get("content")
    if not isinstance(content, str):
        return "missing"
    declared = record.get("size_bytes", record.get("size"))
    try:
        size = int(declared)
    except (TypeError, ValueError):
        return "text"
    compact = "".join(content.split())
    expected = ((size + 2) // 3) * 4
    if len(compact) == expected and _B64_RE.fullmatch(compact):
        return "base64"
    return "text"


def decode_content(record: dict[str, Any]) -> bytes:
    content = record.get("content")
    if not isinstance(content, str):
        raise ArchiveError(f"file {record.get('path')!r} has no string content")
    encoding = classify_content(record)
    if encoding == "base64":
        try:
            data = base64.b64decode("".join(content.split()), validate=True)
        except binascii.Error as exc:
            raise ArchiveError(f"invalid base64 in {record.get('path')!r}: {exc}") from exc
    else:
        data = content.encode("utf-8")
    declared = record.get("size_bytes", record.get("size"))
    if declared is not None and len(data) != int(declared):
        raise ArchiveError(
            f"size mismatch for {record.get('path')!r}: decoded={len(data)} declared={declared}"
        )
    return data


def record_mime(record: dict[str, Any]) -> str:
    value = record.get("mime_type")
    if isinstance(value, str) and "/" in value:
        return value
    guessed, _ = mimetypes.guess_type(str(record.get("path", "")))
    return guessed or "application/octet-stream"


class EnvironmentArchive:
    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path).expanduser().resolve()
        if not self.path.is_file():
            raise FileNotFoundError(self.path)

    def _member(self, persona: str, service: str, name: str = "data.json") -> str:
        return f"{ROOT}/{persona}/services/{service}/{name}"

    def personas(self) -> list[str]:
        prefix = f"{ROOT}/"
        with zipfile.ZipFile(self.path) as zf:
            result = {
                info.filename[len(prefix) :].split("/", 1)[0]
                for info in zf.infolist()
                if info.filename.startswith(prefix)
                and not info.filename.startswith("__MACOSX/")
                and "/services/" in info.filename
            }
        return sorted(x for x in result if x and not x.startswith("."))

    def services(self, persona: str) -> list[str]:
        prefix = f"{ROOT}/{persona}/services/"
        with zipfile.ZipFile(self.path) as zf:
            result = {
                info.filename[len(prefix) :].split("/", 1)[0]
                for info in zf.infolist()
                if info.filename.startswith(prefix)
                and len(info.filename) > len(prefix)
            }
        return sorted(x for x in result if x and not x.startswith("."))

    @contextmanager
    def open_member(self, member: str) -> Iterator[BinaryIO]:
        zf = zipfile.ZipFile(self.path)
        try:
            with zf.open(member) as stream:
                yield stream
        finally:
            zf.close()

    def iter_items(self, persona: str, service: str, array_name: str) -> Iterator[dict[str, Any]]:
        member = self._member(persona, service)
        with self.open_member(member) as stream:
            yield from ijson.items(stream, f"{array_name}.item")

    def iter_files(self, persona: str) -> Iterator[dict[str, Any]]:
        yield from self.iter_items(persona, "filesystem", "files")

    def iter_github_files(
        self, persona: str, *, folder: str = "Github"
    ) -> Iterator[dict[str, Any]]:
        """Yield the persona's GitHub files as Drive-file *records* under ``folder/``.

        This lets the Drive pipeline treat a persona's git repository as ordinary
        Drive content: seeded into a single ``Github`` folder, tracked in the
        manifest, and therefore reconciled by delta / reseed / reset exactly like
        any other Drive file. Mirrors the bulk seeder's "real files in one Github
        folder" behaviour, but manifest-backed so it survives an engine delta.

        Each record matches the ``filesystem`` file shape (path + base64 content),
        so ``decode_content`` / ``record_mime`` consume them unchanged.
        """
        prefix = f"{ROOT}/{persona}/services/github/"
        folder = folder.strip("/") or "Github"
        with zipfile.ZipFile(self.path) as zf:
            for info in zf.infolist():
                name = info.filename
                if not name.startswith(prefix) or name.startswith("__MACOSX/"):
                    continue
                if info.is_dir() or "/._" in name:
                    continue
                relative = name[len(prefix) :]
                if not relative or relative.startswith("._") or relative.endswith("/.DS_Store"):
                    continue
                with zf.open(info) as stream:
                    data = stream.read()
                safe = safe_relpath(f"{folder}/{relative}")
                guessed, _ = mimetypes.guess_type(relative)
                yield {
                    "path": safe,
                    # base64 so classify_content()/decode_content() treat it as binary
                    "content": base64.b64encode(data).decode("ascii"),
                    "size": len(data),
                    "mime_type": guessed or "application/octet-stream",
                }

    def iter_directories(self, persona: str) -> Iterator[str]:
        member = self._member(persona, "filesystem")
        with self.open_member(member) as stream:
            for value in ijson.items(stream, "directories.item"):
                if isinstance(value, str):
                    yield safe_relpath(value)

    def load_emails(self, persona: str) -> list[dict[str, Any]]:
        return list(self.iter_items(persona, "email", "emails"))

    def load_events(self, persona: str) -> list[dict[str, Any]]:
        return list(self.iter_items(persona, "calendar", "events"))

    def attachment_names(self, persona: str) -> list[str]:
        names: list[str] = []
        for message in self.load_emails(persona):
            attachments = message.get("attachments")
            if isinstance(attachments, dict):
                names.extend(str(name) for name in attachments)
            elif isinstance(attachments, list):
                for item in attachments:
                    if isinstance(item, str):
                        names.append(item)
                    elif isinstance(item, dict):
                        name = item.get("filename") or item.get("name") or item.get("path")
                        if name:
                            names.append(str(name))
        return names

    def scan_persona(self, persona: str) -> dict[str, Any]:
        events = self.load_events(persona)
        emails = self.load_emails(persona)
        file_count = 0
        declared_bytes = 0
        encoding_counts: Counter[str] = Counter()
        mime_counts: Counter[str] = Counter()
        basename_paths: dict[str, list[str]] = defaultdict(list)
        duplicate_paths: list[str] = []
        seen_paths: set[str] = set()
        content_errors: list[str] = []
        for record in self.iter_files(persona):
            file_count += 1
            try:
                path = safe_relpath(str(record["path"]))
            except (KeyError, ArchiveError) as exc:
                content_errors.append(str(exc))
                continue
            if path in seen_paths:
                duplicate_paths.append(path)
            seen_paths.add(path)
            basename_paths[PurePosixPath(path).name].append(path)
            encoding_counts[classify_content(record)] += 1
            mime_counts[record_mime(record)] += 1
            try:
                declared_bytes += int(record.get("size_bytes", record.get("size", 0)))
            except (TypeError, ValueError):
                content_errors.append(f"invalid size for {path!r}")
        attachment_refs = self.attachment_names(persona)
        missing = sorted(
            {name for name in attachment_refs if PurePosixPath(name).name not in basename_paths}
        )
        ambiguous = sorted(
            {
                name
                for name in attachment_refs
                if len(basename_paths.get(PurePosixPath(name).name, [])) > 1
            }
        )
        attendee_email = 0
        attendee_name = 0
        recurrence = 0
        timestamp_errors: list[str] = []
        for event in events:
            for attendee in event.get("attendees") or []:
                if valid_email(attendee):
                    attendee_email += 1
                elif isinstance(attendee, str):
                    attendee_name += 1
            if event.get("recurrence_rule"):
                recurrence += 1
            try:
                parse_datetime(event.get("start_datetime"))
                parse_datetime(event.get("end_datetime"))
            except ArchiveError as exc:
                timestamp_errors.append(str(exc))
        for message in emails:
            try:
                parse_datetime(message.get("timestamp"))
            except ArchiveError as exc:
                timestamp_errors.append(str(exc))
        github = self.github_members(persona) if "github" in self.services(persona) else []
        github_files = [item for item in github if not item.is_dir()]
        github_worktree = [item for item in github_files if "/.git/" not in item.filename]
        github_git_store = [item for item in github_files if "/.git/" in item.filename]
        return {
            "persona": persona,
            "services": self.services(persona),
            "calendar_events": len(events),
            "email_messages": len(emails),
            "filesystem_files": file_count,
            "filesystem_declared_bytes": declared_bytes,
            "content_encodings": dict(encoding_counts),
            "mime_types": dict(mime_counts),
            "attachment_references": len(attachment_refs),
            "missing_attachment_names": missing,
            "ambiguous_attachment_names": ambiguous,
            "valid_email_attendee_values": attendee_email,
            "name_only_attendee_values": attendee_name,
            "events_with_recurrence_hint": recurrence,
            "duplicate_paths": duplicate_paths,
            "content_errors": content_errors,
            "timestamp_errors": timestamp_errors,
            "github_files": len(github_files),
            "github_declared_bytes": sum(item.file_size for item in github_files),
            "github_worktree_files": len(github_worktree),
            "github_worktree_bytes": sum(item.file_size for item in github_worktree),
            "github_git_store_files": len(github_git_store),
            "github_git_store_bytes": sum(item.file_size for item in github_git_store),
        }

    def github_members(self, persona: str) -> list[zipfile.ZipInfo]:
        prefix = f"{ROOT}/{persona}/services/github/"
        with zipfile.ZipFile(self.path) as zf:
            return [
                info
                for info in zf.infolist()
                if info.filename.startswith(prefix)
                and not info.filename.startswith("__MACOSX/")
                and "/._" not in info.filename
            ]

    def extract_github(self, persona: str, destination: str | os.PathLike[str]) -> Path:
        destination = Path(destination).expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=True)
        prefix = f"{ROOT}/{persona}/services/github/"
        with zipfile.ZipFile(self.path) as zf:
            for info in zf.infolist():
                if not info.filename.startswith(prefix) or info.filename.startswith("__MACOSX/"):
                    continue
                relative = info.filename[len(prefix) :]
                if not relative or "/._" in relative or relative.startswith("._"):
                    continue
                safe = safe_relpath(relative.rstrip("/"))
                target = destination.joinpath(*PurePosixPath(safe).parts)
                if destination not in target.parents and target != destination:
                    raise ArchiveError(f"unsafe extraction target: {target}")
                if info.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, target.open("wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        dst.write(chunk)
        return destination

    def sha256(self) -> str:
        digest = hashlib.sha256()
        with self.path.open("rb") as stream:
            while chunk := stream.read(8 * 1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()

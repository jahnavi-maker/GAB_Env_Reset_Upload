"""Per-persona Drive file cache for the engine (opt-in via GAB_DRIVE_CACHE=1).

Why
---
Normally the engine streams the environment zip's ``filesystem/data.json`` for
EVERY account it seeds. In a bulk upload, many accounts share one persona, so the
same large JSON is decoded again and again (once per account subprocess). This
module decodes a persona's Drive files ONCE to disk, then serves them from disk on
every later run of the same persona — so ~200 accounts across ~17 personas decode
~17 times instead of 200.

Correctness (the important part)
--------------------------------
The cache stores the EXACT bytes ``decode_content()`` produces from the zip, and
serves them via a record carrying ``content_path`` (which ``decode_content`` reads
back verbatim, size-checked). So the sha256 / md5 / size fingerprints are
byte-identical to reading the zip directly — delta / reseed / reset behave exactly
the same whether or not the cache is used. ``mime_type`` is stored as
``record_mime()`` returns it, and ``modified`` is carried through, so the desired
state is identical field-for-field.

Safety
------
* Opt-in: disabled unless ``GAB_DRIVE_CACHE`` is truthy. Default => original path.
* The cache is keyed by the archive's (path, size, mtime); a different or changed
  archive rebuilds automatically, so a new environment can never serve stale files.
* Cross-process file lock: parallel account subprocesses build a persona once; the
  rest wait and then read it.
* Any cache error falls back to reading the zip directly (see drive._iter_drive_records).
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Callable, Iterator

from .archive import EnvironmentArchive, decode_content, record_mime, safe_relpath

_INDEX = "index.json"
_FILES = "files"


def enabled() -> bool:
    """True when the Drive cache is switched on (GAB_DRIVE_CACHE=1/true/yes/on)."""
    return os.environ.get("GAB_DRIVE_CACHE", "").strip().lower() in {"1", "true", "yes", "on"}


def cache_root() -> Path:
    """Root cache dir. GAB_DRIVE_CACHE_ROOT wins; else a stable per-user location."""
    root = os.environ.get("GAB_DRIVE_CACHE_ROOT")
    if root:
        return Path(root).expanduser()
    return Path.home() / ".cache" / "gab-drive-cache"


def _persona_dir(persona: str) -> Path:
    # persona is a folder name from the archive; sanitize to a single safe segment.
    safe = safe_relpath(persona).replace("/", "_")
    return cache_root() / safe


def _archive_fingerprint(archive: EnvironmentArchive) -> dict[str, Any]:
    p = Path(archive.path)
    st = p.stat()
    return {"archive": str(p.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _read_index(idx: Path) -> dict[str, Any] | None:
    try:
        return json.loads(idx.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _clear_files(files_root: Path) -> None:
    if not files_root.exists():
        return
    for child in sorted(files_root.rglob("*"), reverse=True):
        try:
            child.unlink() if child.is_file() else child.rmdir()
        except OSError:
            pass


def _build(archive: EnvironmentArchive, persona: str, pdir: Path,
           fp: dict[str, Any], log: Callable[[str], None]) -> None:
    files_root = pdir / _FILES
    _clear_files(files_root)
    files_root.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for record in archive.iter_files(persona):
        rel = safe_relpath(str(record["path"]))
        data = decode_content(record)  # exact bytes the zip path would yield
        dest = files_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(data)
        entry: dict[str, Any] = {
            "path": rel,
            "size": len(data),
            "mime_type": record_mime(record),
        }
        if record.get("modified") is not None:
            entry["modified"] = record["modified"]
        entries.append(entry)
    index = {"persona": persona, "source": fp, "files": entries}
    tmp = pdir / (_INDEX + ".tmp")
    tmp.write_text(json.dumps(index), encoding="utf-8")
    tmp.replace(pdir / _INDEX)
    log(f"drive-cache: built {persona} ({len(entries)} files) at {pdir}")


def ensure(archive: EnvironmentArchive, persona: str,
           log: Callable[[str], None] = lambda _m: None) -> bool:
    """Make sure the persona's Drive files are cached for THIS archive. Returns True
    on a usable cache. Idempotent and safe under parallel subprocesses."""
    pdir = _persona_dir(persona)
    idx = pdir / _INDEX
    fp = _archive_fingerprint(archive)

    existing = _read_index(idx)
    if existing and existing.get("source") == fp:
        return True  # fast path, no lock needed

    pdir.mkdir(parents=True, exist_ok=True)
    lock_path = pdir / ".lock"
    try:
        import fcntl  # POSIX; the platform runs on Linux/macOS
        with open(lock_path, "w") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            # Re-check under the lock: another process may have just built it.
            existing = _read_index(idx)
            if existing and existing.get("source") == fp:
                return True
            _build(archive, persona, pdir, fp, log)
            return True
    except ImportError:
        # No fcntl (non-POSIX): build without the lock. Atomic index rename still
        # protects readers; worst case two builders duplicate work, never corrupt.
        existing = _read_index(idx)
        if existing and existing.get("source") == fp:
            return True
        _build(archive, persona, pdir, fp, log)
        return True


def iter_cached(persona: str) -> Iterator[dict[str, Any]]:
    """Yield Drive-file records served from the cache (content via content_path)."""
    pdir = _persona_dir(persona)
    index = _read_index(pdir / _INDEX)
    if not index:
        return
    files_root = pdir / _FILES
    for entry in index.get("files", []):
        if not isinstance(entry, dict) or "path" not in entry:
            continue
        rec: dict[str, Any] = {
            "path": entry["path"],
            "content_path": str(files_root / entry["path"]),
            "size": entry.get("size"),
            "mime_type": entry.get("mime_type"),
        }
        if entry.get("modified") is not None:
            rec["modified"] = entry["modified"]
        yield rec

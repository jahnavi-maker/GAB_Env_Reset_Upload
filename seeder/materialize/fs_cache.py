"""Local Drive file cache for first push / batch seed only.

Parse each persona's filesystem/data.json once into persona_drive_cache/, then
upload from disk for every account push. This does not replace reset/delta logic.
"""
from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from materialize.bytes_util import decode_seed_bytes
from materialize.json_util import inspect_and_normalize
from materialize.runstate import ENV_ROOT, persona_folders, persona_file

ROOT = Path(__file__).resolve().parent.parent
DRIVE_CACHE_ROOT = Path(os.environ.get("GAB_DRIVE_CACHE_ROOT") or (ROOT / "persona_drive_cache"))
_MANIFEST_NAME = "manifest.json"
_FILES_DIR = "files"
_GUARD = threading.Lock()


def cache_root(persona: str) -> Path:
    return DRIVE_CACHE_ROOT / persona


def _source_fingerprint(path: Path) -> dict[str, Any]:
    st = path.stat()
    return {"path": str(path.resolve()), "size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _fingerprint_matches(manifest: dict[str, Any], path: Path) -> bool:
    fp = manifest.get("source") or {}
    if fp.get("path") != str(path.resolve()):
        return False
    try:
        st = path.stat()
    except OSError:
        return False
    return fp.get("size") == st.st_size and fp.get("mtime_ns") == st.st_mtime_ns


def _rel_path(item: dict[str, Any], index: int) -> str:
    path = str(item.get("path") or item.get("filename") or f"file-{index}").replace("\\", "/")
    return path.lstrip("/")


def materialize_persona_filesystem(
    persona: str,
    log: Callable[[str], None] | None = None,
    *,
    source_path: Path | None = None,
) -> Path | None:
    """Decode filesystem/data.json once into persona_drive_cache/<persona>/files/."""
    log = log or (lambda _m: None)
    src = source_path or persona_file(persona, "filesystem")
    if src is None or not Path(src).exists():
        log(f"Drive cache: no filesystem JSON for persona {persona}")
        return None
    src = Path(src)
    inspected = inspect_and_normalize(src, expected="filesystem")
    if not inspected["ok"]:
        log(f"Drive cache: invalid filesystem JSON for {persona}: {inspected.get('error')}")
        return None
    data = inspected["data"] or {}
    files = [x for x in (data.get("files") or []) if isinstance(x, dict)]
    out_root = cache_root(persona)
    files_root = out_root / _FILES_DIR
    with _GUARD:
        if files_root.exists():
            for child in files_root.rglob("*"):
                if child.is_file():
                    child.unlink()
            for child in sorted(files_root.rglob("*"), reverse=True):
                if child.is_dir():
                    child.rmdir()
        files_root.mkdir(parents=True, exist_ok=True)
        entries: list[dict[str, Any]] = []
        written = 0
        skipped = 0
        for i, item in enumerate(files, 1):
            rel = _rel_path(item, i)
            try:
                raw = decode_seed_bytes(item)
                dest = files_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(raw)
            except Exception as exc:
                skipped += 1
                log(f"Drive cache skip {rel}: {exc}")
                continue
            filename = str(item.get("filename") or item.get("name") or rel.split("/")[-1])[:200]
            entry: dict[str, Any] = {
                "rel": rel,
                "filename": filename,
                "size": len(raw),
                "mime_type": item.get("mime_type") or "application/octet-stream",
            }
            modified = item.get("modified") or item.get("mtime") or item.get("created")
            if modified is not None:
                try:
                    entry["modified"] = float(modified)
                except (TypeError, ValueError):
                    pass
            entries.append(entry)
            written += 1
        manifest = {
            "persona": persona,
            "source": _source_fingerprint(src),
            "file_count": len(entries),
            "written": written,
            "skipped": skipped,
            "files": entries,
        }
        out_root.mkdir(parents=True, exist_ok=True)
        tmp = out_root / f".{_MANIFEST_NAME}.tmp"
        tmp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        tmp.replace(out_root / _MANIFEST_NAME)
    log(f"Drive cache: materialized {persona} — {written} files ({skipped} skipped) → {out_root}")
    return out_root


def load_manifest(persona: str) -> dict[str, Any] | None:
    path = cache_root(persona) / _MANIFEST_NAME
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def ensure_persona_drive_cache(
    persona: str,
    log: Callable[[str], None],
    *,
    source_path: Path | None = None,
) -> Path | None:
    src = source_path or persona_file(persona, "filesystem")
    if src is None or not Path(src).exists():
        return None
    src = Path(src)
    manifest = load_manifest(persona)
    if manifest and _fingerprint_matches(manifest, src):
        log(f"Drive cache hit for {persona} ({manifest.get('file_count', 0)} files)")
        return cache_root(persona)
    log(f"Drive cache building for {persona} from {src.name}")
    return materialize_persona_filesystem(persona, log, source_path=src)


def materialize_all_personas(log: Callable[[str], None] | None = None) -> dict[str, Any]:
    log = log or (lambda _m: None)
    stats = {"ok": 0, "skipped": 0, "failed": 0}
    for name in persona_folders():
        src = persona_file(name, "filesystem")
        if src is None:
            stats["skipped"] += 1
            continue
        try:
            if ensure_persona_drive_cache(name, log):
                stats["ok"] += 1
            else:
                stats["skipped"] += 1
        except Exception as exc:
            stats["failed"] += 1
            log(f"Drive cache failed for {name}: {exc}")
    log(f"Drive cache all personas: {stats}")
    return stats


def _basename_keys(rel: str) -> set[str]:
    base = rel.replace("\\", "/").rsplit("/", 1)[-1]
    keys = {rel, rel.replace("\\", "/")}
    if base:
        keys.add(base)
    return keys


def file_index_from_cache(persona: str, wanted: set[str]) -> dict[str, bytes]:
    if not wanted:
        return {}
    manifest = load_manifest(persona)
    if not manifest:
        return {}
    wanted_base = {w.replace("\\", "/").rsplit("/", 1)[-1] for w in wanted}
    index: dict[str, bytes] = {}
    files_root = cache_root(persona) / _FILES_DIR
    for entry in manifest.get("files") or []:
        if not isinstance(entry, dict):
            continue
        rel = str(entry.get("rel") or "")
        if not rel:
            continue
        keys = _basename_keys(rel)
        if not (keys & wanted) and not (keys & wanted_base):
            continue
        path = files_root / rel
        if not path.is_file():
            continue
        try:
            raw = path.read_bytes()
        except OSError:
            continue
        for key in keys:
            prev = index.get(key)
            if prev is None or len(raw) > len(prev):
                index[key] = raw
    return index


def cache_file_entries(persona: str) -> list[dict[str, Any]]:
    manifest = load_manifest(persona)
    if not manifest:
        return []
    return [e for e in (manifest.get("files") or []) if isinstance(e, dict)]


def read_cached_bytes(persona: str, rel: str) -> bytes:
    path = cache_root(persona) / _FILES_DIR / rel.replace("\\", "/").lstrip("/")
    return path.read_bytes()


if __name__ == "__main__":
    import sys

    logs: list[str] = []

    def _log(msg: str) -> None:
        print(msg)
        logs.append(msg)

    if len(sys.argv) > 1 and sys.argv[1] == "--all":
        materialize_all_personas(_log)
    elif len(sys.argv) > 2 and sys.argv[1] == "--persona":
        ensure_persona_drive_cache(sys.argv[2], _log)
    else:
        print("Usage: python -m materialize.fs_cache --all | --persona Student")
        sys.exit(1)

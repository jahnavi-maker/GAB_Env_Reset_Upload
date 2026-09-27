from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from materialize.fs_cache import (
    cache_file_entries,
    cache_root,
    ensure_persona_drive_cache,
    file_index_from_cache,
    read_cached_bytes,
)
from materialize.github_sync import iter_github_files
from materialize.json_util import inspect_and_normalize


@dataclass
class EnvironmentArtifacts:
    environment_id: str
    persona: str
    calendar_path: Path | None
    gmail_path: Path | None
    drive_path: Path | None
    github_dir: Path | None
    calendar_data: dict[str, Any] | None = None
    gmail_data: dict[str, Any] | None = None
    drive_entries: list[dict[str, Any]] = field(default_factory=list)
    github_files: list[Path] = field(default_factory=list)
    cache_dir: Path | None = None
    errors: list[str] = field(default_factory=list)

    def attachment_index(self, wanted: set[str]) -> dict[str, bytes]:
        if not wanted:
            return {}
        return file_index_from_cache(self.persona, wanted)

    def read_generated(self, rel: str) -> bytes:
        return read_cached_bytes(self.persona, rel)


class EnvironmentBuilder:
    """Prepare each persona/environment once and reuse it across accounts."""

    def __init__(self) -> None:
        self._cache: dict[str, EnvironmentArtifacts] = {}

    def get(self, environment_id: str) -> EnvironmentArtifacts | None:
        return self._cache.get(environment_id)

    def prepare(
        self,
        *,
        environment_id: str,
        persona: str,
        calendar_json: Path | None,
        gmail_json: Path | None,
        drive_json: Path | None,
        github_dir: Path | None,
        log: Callable[[str], None],
        materialize_drive: bool = True,
    ) -> EnvironmentArtifacts:
        hit = self._cache.get(environment_id)
        if hit is not None:
            return hit
        art = EnvironmentArtifacts(
            environment_id=environment_id,
            persona=persona,
            calendar_path=Path(calendar_json) if calendar_json else None,
            gmail_path=Path(gmail_json) if gmail_json else None,
            drive_path=Path(drive_json) if drive_json else None,
            github_dir=Path(github_dir) if github_dir else None,
        )
        if art.calendar_path:
            inspected = inspect_and_normalize(art.calendar_path, expected="calendar")
            if inspected["ok"]:
                art.calendar_data = inspected["data"]
            else:
                art.errors.append(f"calendar: {inspected.get('error')}")
        if art.gmail_path:
            inspected = inspect_and_normalize(art.gmail_path, expected="gmail")
            if inspected["ok"]:
                art.gmail_data = inspected["data"]
            else:
                art.errors.append(f"gmail: {inspected.get('error')}")
        if art.drive_path and materialize_drive:
            cache = ensure_persona_drive_cache(persona, log, source_path=art.drive_path)
            if cache is None:
                art.errors.append(f"drive: could not materialize {art.drive_path}")
            else:
                art.cache_dir = cache
                art.drive_entries = cache_file_entries(persona)
        if art.github_dir and art.github_dir.is_dir():
            art.github_files = iter_github_files(art.github_dir)
        elif github_dir:
            art.errors.append(f"github: directory missing {github_dir}")
        self._cache[environment_id] = art
        return art

    def cached_root(self, persona: str) -> Path:
        return cache_root(persona)

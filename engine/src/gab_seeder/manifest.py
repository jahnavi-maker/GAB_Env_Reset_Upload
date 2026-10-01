from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def new_manifest(
    *,
    account: str,
    persona: str,
    archive: str,
    seed_tag: str,
    source_fingerprint: str | None = None,
) -> dict[str, Any]:
    return {
        "version": 3,
        "account": account,
        "persona": persona,
        "archive": archive,
        "seed_tag": seed_tag,
        "source_fingerprint": source_fingerprint,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "drive": {"folders": {}, "files": {}},
        "gmail": {"label_id": None, "messages": {}},
        "calendar": {"events": {}},
        "github": {"refs": {}},
        "warnings": [],
    }


def load_manifest(path: str | Path) -> dict[str, Any] | None:
    path = Path(path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_manifest(path: str | Path, manifest: dict[str, Any]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(temp, path)

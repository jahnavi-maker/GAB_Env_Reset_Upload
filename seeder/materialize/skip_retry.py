from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from materialize.github_sync import iter_github_files
from materialize.jobs import account_log_path, RUNS

_SKIP_GITHUB = re.compile(r"Skip GitHub file (?P<path>.+?): ")
_SKIP_DRIVE = re.compile(r"Skip Drive file \d+ \((?P<path>[^)]+)\):")
_SKIP_LARGE = re.compile(r"Skip large file (?P<path>\S+)")
_SKIP_EMAIL = re.compile(r"Skip email (?P<eid>\S+)")

_EMPTY_GITHUB: dict[str, frozenset[str]] = {}


def parse_skip_log(text: str) -> dict[str, set[str]]:
    github: set[str] = set()
    drive: set[str] = set()
    gmail: set[str] = set()
    for raw in text.splitlines():
        line = raw.split(" | ", 1)[-1]
        line = re.sub(r"^\[.*?\]\s*", "", line)
        if m := _SKIP_GITHUB.search(line):
            github.add(m.group("path").replace("\\", "/").lstrip("./"))
            continue
        if m := _SKIP_DRIVE.search(line):
            drive.add(m.group("path").replace("\\", "/").lstrip("/"))
            continue
        if m := _SKIP_LARGE.search(line):
            drive.add(m.group("path").replace("\\", "/").lstrip("/"))
            continue
        if m := _SKIP_EMAIL.search(line):
            eid = m.group("eid").rstrip(":")
            if eid and eid != "circular":
                gmail.add(eid)
    return {"github": github, "drive": drive, "gmail": gmail}


def empty_github_relpaths(github_dir: Path | None) -> set[str]:
    if not github_dir or not Path(github_dir).is_dir():
        return set()
    key = str(Path(github_dir).resolve())
    cached = _EMPTY_GITHUB.get(key)
    if cached is None:
        found: set[str] = set()
        for path in iter_github_files(Path(github_dir)):
            try:
                if path.stat().st_size == 0:
                    found.add(str(path.relative_to(github_dir)).replace("\\", "/"))
            except OSError:
                continue
        cached = frozenset(found)
        _EMPTY_GITHUB[key] = cached
    return set(cached)


def account_log_text(run_id: str, email: str, persona_key: str | None) -> str:
    path = account_log_path(run_id, email, persona_key)
    if path.exists():
        return path.read_text(encoding="utf-8", errors="replace")
    logs = RUNS / str(run_id) / "logs"
    if not logs.is_dir():
        return ""
    slug = path.name.split("__p_")[0] if "__p_" in path.name else path.stem
    matches = sorted(logs.glob(f"{slug}*.log"))
    if not matches:
        return ""
    return matches[-1].read_text(encoding="utf-8", errors="replace")


def plan_account_skips(
    run_id: str,
    email: str,
    persona_key: str | None,
    github_dir: Path | None = None,
) -> dict[str, Any]:
    parsed = parse_skip_log(account_log_text(run_id, email, persona_key))
    github = set(parsed["github"]) | empty_github_relpaths(github_dir)
    drive = set(parsed["drive"])
    gmail = set(parsed["gmail"])
    return {
        "email": email,
        "github": sorted(github),
        "drive": sorted(drive),
        "gmail": sorted(gmail),
        "count": len(github) + len(drive) + len(gmail),
    }


def plan_has_work(plan: dict[str, Any] | None) -> bool:
    if not plan:
        return False
    return bool(plan.get("github") or plan.get("drive") or plan.get("gmail"))


def summarize_run_skips(
    run_id: str,
    accounts: list[dict[str, Any]],
    github_dir_for: Any,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    totals = {"github": 0, "drive": 0, "gmail": 0, "accounts": 0, "files": 0}
    for acc in accounts:
        if (acc.get("push") or {}).get("state") == "running":
            continue
        email = acc.get("email") or ""
        pkey = acc.get("persona_key") or ""
        gh = github_dir_for(acc)
        plan = plan_account_skips(run_id, email, pkey, gh)
        if not plan_has_work(plan):
            continue
        rows.append(plan)
        totals["github"] += len(plan["github"])
        totals["drive"] += len(plan["drive"])
        totals["gmail"] += len(plan["gmail"])
        totals["files"] += plan["count"]
        totals["accounts"] += 1
    return {"accounts": rows, **totals}

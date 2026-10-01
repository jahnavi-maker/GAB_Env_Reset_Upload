from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from .archive import EnvironmentArchive


class GitHubSeedError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: int = 300) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        raise GitHubSeedError(f"git {' '.join(args)} failed ({completed.returncode}):\n{completed.stdout}")
    return completed.stdout


def inspect_repository(repo: Path) -> dict[str, Any]:
    head = _git(repo, "rev-parse", "HEAD").strip()
    branch = _git(repo, "branch", "--show-current").strip()
    status = _git(repo, "status", "--porcelain=v1").splitlines()
    lines = _git(
        repo,
        "for-each-ref",
        "--format=%(refname) %(objectname)",
        "refs/heads",
        "refs/tags",
    ).splitlines()
    refs = dict(line.split(" ", 1) for line in lines if " " in line)
    return {
        "head": head,
        "branch": branch,
        "refs": refs,
        "working_tree_status_count": len(status),
        "working_tree_status_sample": status[:25],
    }


def extract_repository(
    archive: EnvironmentArchive,
    *,
    persona: str,
    destination: str | Path,
) -> dict[str, Any]:
    repo = archive.extract_github(persona, destination)
    # The source ZIP was created on macOS and expands regular files with the
    # executable bit set. Git refs/objects are authoritative for publication.
    _git(repo, "config", "core.fileMode", "false")
    remotes = _git(repo, "remote").split()
    if "origin" in remotes and "source-readonly" not in remotes:
        _git(repo, "remote", "rename", "origin", "source-readonly")
        _git(repo, "remote", "set-url", "--push", "source-readonly", "DISABLED")
    result = inspect_repository(repo)
    result["path"] = str(repo)
    return result


def push_repository(
    repo: str | Path,
    *,
    remote_url: str,
    confirm_remote: str,
    dry_run: bool,
) -> dict[str, Any]:
    if remote_url != confirm_remote:
        raise GitHubSeedError("confirmation must exactly match the target remote URL")
    repo = Path(repo).expanduser().resolve()
    state = inspect_repository(repo)
    refs = sorted(state["refs"])
    if dry_run:
        return {"remote": remote_url, "refs": refs, "pushed": False}
    remotes = _git(repo, "remote").split()
    if "gab-target" in remotes:
        _git(repo, "remote", "set-url", "gab-target", remote_url)
    else:
        _git(repo, "remote", "add", "gab-target", remote_url)
    refspecs = [f"{ref}:{ref}" for ref in refs]
    for start in range(0, len(refspecs), 50):
        _git(repo, "push", "--force", "gab-target", *refspecs[start : start + 50], timeout=1800)
    remote_lines = _git(repo, "ls-remote", "--heads", "--tags", "gab-target", timeout=300).splitlines()
    remote_refs = {line.split()[1]: line.split()[0] for line in remote_lines if len(line.split()) == 2}
    missing = {ref: oid for ref, oid in state["refs"].items() if remote_refs.get(ref) != oid}
    if missing:
        raise GitHubSeedError(f"remote verification failed for refs: {json.dumps(missing, indent=2)}")
    return {"remote": remote_url, "refs": refs, "pushed": True, "verified": True}

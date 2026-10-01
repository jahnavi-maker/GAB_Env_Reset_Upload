from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GITHUB_API = "https://api.github.com"
USER_AGENT = "gab-workspace-seed"


class GitHubApiError(RuntimeError):
    def __init__(self, status: int, detail: str):
        self.status = status
        super().__init__(f"GitHub API {status}: {detail[:300]}")


def github_user(token: str) -> dict[str, str | list[str]]:
    req = Request(
        f"{GITHUB_API}/user",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode())
            scopes_raw = resp.headers.get("X-OAuth-Scopes") or ""
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(int(exc.code), detail) from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub API failed: {exc}") from exc
    login = data.get("login")
    if not login:
        raise RuntimeError("GitHub /user did not return a login")
    scopes = [s.strip() for s in scopes_raw.split(",") if s.strip()]
    return {"login": login, "scopes": scopes}


def _api(token: str, method: str, path: str, body: dict | None = None) -> dict:
    data = None if body is None else json.dumps(body).encode()
    req = Request(
        f"{GITHUB_API}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "User-Agent": USER_AGENT,
            "Content-Type": "application/json",
        },
    )
    try:
        with urlopen(req, timeout=60) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw else {}
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise GitHubApiError(int(exc.code), detail) from exc
    except URLError as exc:
        raise RuntimeError(f"GitHub API failed: {exc}") from exc


def ensure_private_repo(token: str, owner: str, name: str, log: Callable[[str], None]) -> str:
    try:
        repo = _api(token, "GET", f"/repos/{owner}/{name}")
        log(f"Reusing GitHub repo {owner}/{name}")
        return repo["full_name"]
    except GitHubApiError as exc:
        if exc.status != 404:
            raise
    repo = _api(
        token,
        "POST",
        "/user/repos",
        {
            "name": name,
            "private": True,
            "auto_init": False,
            "description": "GAB UltraEvals seed (gab-workspace-seed)",
        },
    )
    log(f"Created private GitHub repo {owner}/{name}")
    return repo["full_name"]


def _ignore(directory: str, names: list[str]) -> set[str]:
    skip = {".DS_Store"}
    if ".git" in names:
        skip.add(".git")
    return skip


def _redact(text: str, token: str) -> str:
    if token:
        text = text.replace(token, "***")
    return text.strip()


def git_run(args: list[str], *, cwd: Path, env: dict[str, str], token: str = "") -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["git", *args], cwd=cwd, env=env, capture_output=True)
    if result.returncode != 0:
        err = (result.stderr or result.stdout or b"").decode("utf-8", errors="replace")
        err = _redact(err, token)
        raise RuntimeError(f"git {' '.join(args[:3])} failed: {err[:400]}")
    return result


def push_github_tree(
    token: str,
    github_dir: Path,
    owner: str,
    repo: str,
    log: Callable[[str], None],
) -> str:
    public = f"https://github.com/{owner}/{repo}"
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_PASSWORD"] = token
    with tempfile.TemporaryDirectory(prefix="gab-gh-") as tmp:
        dest = Path(tmp) / "tree"
        askpass = Path(tmp) / "askpass.sh"
        askpass.write_text(
            "#!/bin/sh\n"
            "case \"$1\" in\n"
            "*[Uu]sername*) printf '%s\\n' \"${GIT_USERNAME:-x-access-token}\" ;;\n"
            "*) printf '%s\\n' \"$GIT_PASSWORD\" ;;\n"
            "esac\n"
        )
        os.chmod(askpass, 0o700)
        env["GIT_ASKPASS"] = str(askpass)
        env["GIT_USERNAME"] = "x-access-token"
        shutil.copytree(github_dir, dest, ignore=_ignore)
        if (dest / ".gitmodules").exists():
            log("Tree includes .gitmodules — submodule checkouts are not cloned; only the pointer file is pushed")
        git_run(["init", "-b", "main", "--template="], cwd=dest, env=env, token=token)
        # -f so a persona .gitignore cannot silently drop seed files from the commit.
        git_run(["add", "-A", "-f"], cwd=dest, env=env, token=token)
        staged = git_run(["diff", "--cached", "--name-only"], cwd=dest, env=env, token=token)
        if not (staged.stdout or b"").strip():
            raise RuntimeError("GitHub tree is empty after copy — nothing to commit")
        git_run(
            [
                "-c",
                "user.email=gab-workspace-seed@local",
                "-c",
                "user.name=gab-workspace-seed",
                "commit",
                "-m",
                "GAB UltraEvals seed",
            ],
            cwd=dest,
            env=env,
            token=token,
        )
        remote = f"https://x-access-token@github.com/{owner}/{repo}.git"
        git_run(["remote", "add", "origin", remote], cwd=dest, env=env, token=token)
        try:
            git_run(["push", "-u", "origin", "main", "--force"], cwd=dest, env=env, token=token)
        except RuntimeError as exc:
            msg = str(exc)
            if "without `workflow` scope" in msg or "without 'workflow' scope" in msg:
                raise RuntimeError(
                    "GitHub PAT is missing the workflow scope "
                    "(needed because this persona tree includes .github/workflows)."
                ) from exc
            raise
    log(f"Pushed GitHub tree to {public}")
    return public

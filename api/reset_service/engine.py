"""Adapter around Vishal's ``gab_seeder`` reset engine.

We do NOT modify Vishal's code. His CLI resolves the target account from
``config["accounts"][persona].email`` and uses ``persona`` to select the
archive data folder. Because our API targets a specific *email* (and one
persona can back many accounts at scale), we write a per-request temporary
config that overrides ``accounts[persona].email`` with the requested address,
then invoke the CLI exactly as an operator would.

This runs a blocking subprocess; callers should invoke ``run_reset`` via
``asyncio.to_thread`` so the event loop stays free.
"""
from __future__ import annotations

import copy
import os
import json
import logging
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .config import settings

log = logging.getLogger("reset_service.engine")


@dataclass
class ResetResult:
    success: bool
    detail: str
    mode: str
    returncode: int | None = None
    raw: dict | None = None


def _build_request_config(base_config_path: str, email: str, persona: str) -> Path:
    """Clone the base config, point the persona's account at ``email``."""
    base = json.loads(Path(base_config_path).expanduser().read_text(encoding="utf-8"))
    cfg = copy.deepcopy(base)
    accounts = cfg.setdefault("accounts", {})
    account = dict(accounts.get(persona) or {})
    account["email"] = email
    accounts[persona] = account

    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".json", prefix="gab-reset-cfg-", delete=False, encoding="utf-8"
    )
    json.dump(cfg, tmp)
    tmp.flush()
    tmp.close()
    return Path(tmp.name)


ALL_SERVICES = "drive,gmail,calendar"


def _resolve_services(services: list[str] | None) -> str | None:
    """Platform policy: ALWAYS operate on all three services for every account
    and every operation (upload / reset / reseed / delta).

    Calendar is never excluded and GitHub rides inside Drive, so a single call
    covers the entire environment. Any per-request ``services`` subset or the
    ``GAB_RESET_SERVICES`` env value is intentionally ignored, so behaviour is
    uniform across all users and no one has to opt Calendar in per-account.

    Escape hatch (ops/testing only): ``GAB_RESET_SERVICES_FORCE`` overrides the
    forced set — e.g. "drive,gmail" to seed without Calendar while its daily
    write quota recovers. MUST be unset in production so Calendar stays included.
    """
    override = os.environ.get("GAB_RESET_SERVICES_FORCE", "").strip()
    if override:
        allowed = {"drive", "gmail", "calendar"}
        picked = [s.strip() for s in override.split(",") if s.strip() in allowed]
        if picked:
            return ",".join(picked)
    return ALL_SERVICES


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, timeout=settings.reset_timeout_s)


def _base_args(sub: str, config_path: Path, persona: str, services: str | None) -> list[str]:
    args = [settings.gab_seed_bin, sub, "--config", str(config_path), "--persona", persona]
    if services:
        args += ["--services", services]
    return args


def _parse_tail_json(proc: subprocess.CompletedProcess) -> dict | None:
    """Parse the engine's result JSON from stdout.

    The CLI prints ONE multi-line (indented) JSON object as its result, so the old
    "last line" parse always failed (last line is just "}") and dropped every
    per-module/skip/warning count. Parse the whole stdout; fall back to the last
    balanced {...} block if any log lines precede it.
    """
    stdout = (proc.stdout or "").strip()
    if not stdout:
        return None
    try:
        obj = json.loads(stdout)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    # Fallback: scan backwards for the last top-level {...} object.
    depth = 0
    end = None
    for i in range(len(stdout) - 1, -1, -1):
        ch = stdout[i]
        if ch == "}":
            if depth == 0:
                end = i
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0 and end is not None:
                try:
                    obj = json.loads(stdout[i : end + 1])
                    return obj if isinstance(obj, dict) else None
                except json.JSONDecodeError:
                    depth = 0
                    end = None
    return None


def run_reset(
    email: str,
    persona: str,
    mode: str | None = None,
    services: list[str] | None = None,
) -> ResetResult:
    mode = (mode or settings.reset_mode).strip()
    try:
        svc = _resolve_services(services)
    except ValueError as exc:
        return ResetResult(False, str(exc), mode)

    if settings.simulate:
        time.sleep(1.0)
        log.info("SIMULATE reset ok email=%s persona=%s mode=%s services=%s", email, persona, mode, svc or "all")
        return ResetResult(True, "simulated reset", mode, returncode=0)

    if not settings.gab_config:
        return ResetResult(False, "GAB_CONFIG is not set; cannot run reset", mode)

    cfg_path = _build_request_config(settings.gab_config, email, persona)
    log.info("reset start email=%s persona=%s mode=%s services=%s", email, persona, mode, svc or "all")
    try:
        if mode == "reseed":
            # Run reseed as a scoped wipe -> seed. We deliberately avoid the
            # `reseed` subcommand because its final verify covers ALL services
            # (so a skipped/partial calendar would fail the run). Scoped
            # reset+seed keeps calendar untouched when svc omits it.
            reset_args = _base_args("reset", cfg_path, persona, svc) + ["--confirm-account", email, "--execute"]
            proc = _run_cli(reset_args)
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "wipe failed").strip()[:2000]
                return ResetResult(False, f"wipe stage failed: {detail}", mode, returncode=proc.returncode)
            seed_args = _base_args("seed", cfg_path, persona, svc) + ["--execute"]
            proc = _run_cli(seed_args)
        else:
            args = _base_args(mode, cfg_path, persona, svc)
            if mode == "reset":
                args += ["--confirm-account", email]
            args += ["--execute"]
            proc = _run_cli(args)
    except subprocess.TimeoutExpired:
        return ResetResult(False, f"reset timed out after {settings.reset_timeout_s}s", mode)
    except FileNotFoundError:
        return ResetResult(False, f"reset binary not found: {settings.gab_seed_bin!r}", mode)
    finally:
        cfg_path.unlink(missing_ok=True)

    raw = _parse_tail_json(proc)
    if proc.returncode == 0:
        return ResetResult(True, f"reset completed (services={svc or 'all'})", mode, returncode=0, raw=raw)

    detail = (proc.stderr or proc.stdout or "reset failed").strip()[:2000]
    log.error("reset failed email=%s rc=%s: %s", email, proc.returncode, detail)
    return ResetResult(False, detail, mode, returncode=proc.returncode, raw=raw)

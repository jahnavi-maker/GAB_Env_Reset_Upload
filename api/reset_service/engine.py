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


def _engine_env() -> dict[str, str]:
    """Env for the engine subprocess: enable the per-persona Drive cache.

    The cache decodes each persona's Drive files ONCE and reuses them across every
    account of that persona — a big win for bulk uploads (no re-parsing the 1.6 GB
    zip per account). Fingerprints are byte-identical, so delta/reseed/reset are
    unaffected. Opt out with GAB_DRIVE_CACHE=0 in the platform env. The cache lives
    beside the engine state so it persists across runs.
    """
    env = dict(os.environ)
    env.setdefault("GAB_DRIVE_CACHE", "1")
    if "GAB_DRIVE_CACHE_ROOT" not in env and settings.gab_config:
        try:
            base = json.loads(Path(settings.gab_config).expanduser().read_text(encoding="utf-8"))
            state_dir = base.get("state_dir")
            if state_dir:
                env["GAB_DRIVE_CACHE_ROOT"] = str(Path(state_dir).expanduser() / "drive-cache")
        except Exception:  # noqa: BLE001 - fall back to the engine's default cache dir
            pass
    return env


def _run_cli(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=settings.reset_timeout_s, env=_engine_env()
    )


def _base_args(sub: str, config_path: Path, persona: str, services: str | None) -> list[str]:
    args = [settings.gab_seed_bin, sub, "--config", str(config_path), "--persona", persona]
    if services:
        args += ["--services", services]
    return args


# --- Failure classification (drives the fallback chain in run_reset) ------------
# The engine ends EVERY "delta can't safely proceed" case with this exact phrase:
# incomplete/missing manifest, different account/persona, a DIFFERENT ARCHIVE (i.e. a
# new environment), and drive/gmail/calendar delta-safety errors. So one check routes
# all of them to a full reseed.
_FULL_RESET_SIGNAL = "explicit full reset required"
_TRANSIENT_HINTS = (
    "timed out", "timeout", "temporarily", "connection reset", "connection aborted",
    "broken pipe", "socket", "eof occurred", "ssl", " 500", " 502", " 503", " 504",
    "backenderror", "internalerror", "internal error", "try again", "deadline",
)
_QUOTA_HINTS = (
    "quotaexceeded", "quota exceeded", "ratelimitexceeded", "userratelimitexceeded",
    "dailylimitexceeded", "rate limit", "limit exceeded",
)
_AUTH_HINTS = ("invalid_grant", "invalid grant", "refresherror", "token has been expired",
               "token has been revoked", "unauthorized", "re-authorize")
# A delta whose post-apply verify fails ("no destructive fallback was run") is often
# an eventual-consistency blip, but a full reseed always restores a correct baseline
# (which is the reset's goal anyway), so we recover with a reseed.
_VERIFY_FAIL_HINTS = ("post-apply verification failed", "verification failed", "no destructive fallback")
# Drive/Gmail/Calendar delta-safety stops. The engine appends "explicit full reset
# required" to these, but that phrase sits at the END of a message that can list
# hundreds of per-object conflicts and get truncated before it. These fragments
# appear THROUGHOUT the message (e.g. one per conflicting event), so they survive
# truncation and reliably route the account to a full reseed — which is exactly the
# recovery the engine asks for.
_DELTA_SAFETY_HINTS = (
    "deltasafetyerror", "delta safety", "delta-safety",
    "cross-seed", "conflicts with source", "claimed by multiple baseline",
    "ambiguous current", "ambiguous legacy", "ambiguous markerless",
    "unresolved markerless",
)


def _needs_full_reset(detail: str) -> bool:
    d = (detail or "").lower()
    return (
        _FULL_RESET_SIGNAL in d
        or any(h in d for h in _VERIFY_FAIL_HINTS)
        or any(h in d for h in _DELTA_SAFETY_HINTS)
    )


def _is_quota(detail: str) -> bool:
    d = (detail or "").lower()
    return any(h in d for h in _QUOTA_HINTS)


def _is_auth(detail: str) -> bool:
    d = (detail or "").lower()
    return any(h in d for h in _AUTH_HINTS)


def _is_transient(detail: str) -> bool:
    d = (detail or "").lower()
    if _is_quota(d) or _needs_full_reset(d):
        return False  # these are NOT retryable-in-place
    return any(h in d for h in _TRANSIENT_HINTS)


def _classify(detail: str) -> str:
    """Turn a raw engine failure into an operator-actionable message."""
    if _is_quota(detail):
        return ("Google API quota exceeded — this is a daily per-account limit, not a code error. "
                "Retry after it resets (usually within 24h). Detail: " + detail)
    if _is_auth(detail):
        return ("Account authorization expired/invalid — re-authorize this account, then reset again. "
                "Detail: " + detail)
    return detail


def _detail(proc: subprocess.CompletedProcess) -> str:
    raw = (proc.stderr or proc.stdout or "reset failed").strip()
    if len(raw) <= 2000:
        return raw
    # Keep head AND tail: engines often append the actionable signal (e.g. the
    # "explicit full reset required" line) at the very end, after a long body.
    return raw[:1400] + "  …[truncated]…  " + raw[-600:]


def _run_mode(cfg_path: Path, persona: str, svc: str | None, email: str, sub: str) -> subprocess.CompletedProcess:
    """Run a single engine subcommand (delta/reset) with --execute."""
    args = _base_args(sub, cfg_path, persona, svc)
    if sub == "reset":
        args += ["--confirm-account", email]
    args += ["--execute"]
    return _run_cli(args)


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


def _run_reseed(cfg_path: Path, persona: str, svc: str | None, email: str):
    """Scoped wipe -> seed (a full reseed). We avoid the `reseed` subcommand because
    its final verify covers ALL services, so a skipped/partial calendar would fail the
    run; scoped reset+seed keeps calendar untouched when svc omits it.

    Returns ``(proc, wipe_fail)``: if the wipe stage fails, ``wipe_fail`` is a ready
    ResetResult and ``proc`` is the failed wipe; otherwise ``wipe_fail`` is None and
    ``proc`` is the seed CompletedProcess to evaluate normally.
    """
    reset_args = _base_args("reset", cfg_path, persona, svc) + ["--confirm-account", email, "--execute"]
    proc = _run_cli(reset_args)
    if proc.returncode != 0 and _is_transient(_detail(proc)):
        proc = _run_cli(reset_args)  # one retry if the wipe stalled on the network
    if proc.returncode != 0:
        return proc, ResetResult(
            False, f"wipe stage failed: {_classify(_detail(proc))}", "reseed", returncode=proc.returncode
        )
    seed_args = _base_args("seed", cfg_path, persona, svc) + ["--execute"]
    proc = _run_cli(seed_args)
    if proc.returncode != 0 and _is_transient(_detail(proc)):
        proc = _run_cli(seed_args)  # one retry on a transient seed failure
    return proc, None


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

    # Build the per-request config up front. A missing/malformed engine config (or a
    # persona absent from it) must return a clean message, not leak a raw traceback.
    try:
        cfg_path = _build_request_config(settings.gab_config, email, persona)
    except FileNotFoundError:
        return ResetResult(False, f"engine config not found at {settings.gab_config!r}", mode)
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        return ResetResult(False, f"engine config invalid ({settings.gab_config!r}): {exc}", mode)
    log.info("reset start email=%s persona=%s mode=%s services=%s", email, persona, mode, svc or "all")
    effective_mode = mode
    try:
        if mode == "reseed":
            proc, wipe_fail = _run_reseed(cfg_path, persona, svc, email)
            if wipe_fail is not None:
                return wipe_fail
        else:
            proc = _run_mode(cfg_path, persona, svc, email, mode)
            if proc.returncode != 0:
                detail = _detail(proc)
                if _needs_full_reset(detail):
                    # Covers ALL "delta can't proceed" cases the engine flags: incomplete
                    # or missing manifest, different account/persona, a DIFFERENT ARCHIVE
                    # (new environment), and drive/gmail/calendar delta-safety stops. The
                    # correct recovery for every one is a full reseed, so do it.
                    log.info("%s -> full reset required for %s; reseeding", mode, email)
                    effective_mode = "reseed"
                    proc, wipe_fail = _run_reseed(cfg_path, persona, svc, email)
                    if wipe_fail is not None:
                        return wipe_fail
                elif _is_transient(detail):
                    log.warning("%s transient failure for %s; retrying once", mode, email)
                    proc = _run_mode(cfg_path, persona, svc, email, mode)
    except subprocess.TimeoutExpired:
        return ResetResult(False, f"reset timed out after {settings.reset_timeout_s}s", effective_mode)
    except FileNotFoundError:
        return ResetResult(False, f"reset binary not found: {settings.gab_seed_bin!r}", effective_mode)
    finally:
        cfg_path.unlink(missing_ok=True)

    raw = _parse_tail_json(proc)
    if proc.returncode == 0:
        return ResetResult(True, f"reset completed (services={svc or 'all'}, mode={effective_mode})",
                           effective_mode, returncode=0, raw=raw)

    detail = _classify(_detail(proc))
    log.error("reset failed email=%s rc=%s: %s", email, proc.returncode, detail)
    return ResetResult(False, detail, effective_mode, returncode=proc.returncode, raw=raw)

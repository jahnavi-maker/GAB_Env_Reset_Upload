from __future__ import annotations

import hashlib
import json
import secrets
import shutil
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .archive import EnvironmentArchive
from .calendar_seed import reconcile_calendar_delta, reset_calendar_all, seed_calendar, verify_calendar
from .config import (
    account_for_persona,
    load_config,
    missing_attachment_policy_for_persona,
    state_path,
)
from .drive import reconcile_drive_delta, reset_drive_all, seed_drive, verify_drive
from .gmail import reconcile_gmail_delta, reset_gmail_all, seed_gmail, verify_gmail
from .google_auth import (
    AuthenticationError,
    credentials_for_account,
    delete_credentials,
    save_credentials,
    services_for_credentials,
    verify_account,
)
from .manifest import load_manifest, new_manifest, save_manifest


def _source_fingerprint(account: str, persona: str, archive: EnvironmentArchive) -> str:
    stat = archive.path.stat()
    material = f"{account}|{persona}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _source_catalog_fingerprint(account: str, persona: str, archive: EnvironmentArchive) -> str:
    prefix = f"PKJA_UltraEvals_Environments_/{persona}/services/"
    with zipfile.ZipFile(archive.path) as zf:
        rows = [
            f"{info.filename}\0{info.file_size}\0{info.CRC}"
            for info in zf.infolist()
            if info.filename.startswith(prefix) and not info.is_dir()
        ]
    material = account + "\0" + persona + "\0" + "\n".join(sorted(rows))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _new_seed_tag() -> str:
    return secrets.token_hex(10)


def _manifest_path(config: dict[str, Any], account: str, persona: str, dry_run: bool) -> Path:
    path = state_path(config, account, persona)
    if dry_run:
        return path.with_name(path.stem + ".dry-run.json")
    return path


def _report(progress: Callable[[str, str], None] | None, phase: str, detail: str) -> None:
    if progress is not None:
        progress(phase, detail)


def _verification_summary(verify: dict[str, Any]) -> dict[str, dict[str, Any]]:
    allowed_fragments = (
        "expected",
        "remote",
        "missing",
        "drifted",
        "extra",
        "duplicate",
        "baseline",
        "unchanged",
    )
    return {
        name: {
            key: value
            for key, value in (verify.get(name) or {}).items()
            if key == "ok"
            or (
                isinstance(value, (bool, int, float))
                and any(fragment in key for fragment in allowed_fragments)
            )
        }
        for name in ("drive", "gmail", "calendar")
        if name in verify
    }


def _validate_completed_manifest(
    *,
    path: Path,
    manifest: dict[str, Any] | None,
    account_email: str,
    persona: str,
    archive: EnvironmentArchive,
) -> dict[str, Any]:
    if manifest is None:
        raise RuntimeError(f"no completed live manifest found at {path}; explicit full reset required")
    if not manifest.get("completed_at"):
        raise RuntimeError(f"manifest {path} is incomplete; explicit full reset required")
    if str(manifest.get("account") or "").casefold() != account_email.casefold():
        raise RuntimeError(f"manifest {path} belongs to a different account; explicit full reset required")
    if manifest.get("persona") != persona:
        raise RuntimeError(f"manifest {path} belongs to a different persona; explicit full reset required")
    legacy = _source_fingerprint(account_email, persona, archive)
    catalog = _source_catalog_fingerprint(account_email, persona, archive)
    manifest_fingerprint = manifest.get("source_fingerprint") or manifest.get("seed_tag")
    manifest_catalog = manifest.get("source_catalog_fingerprint")
    if manifest_catalog not in {None, catalog}:
        raise RuntimeError(f"manifest {path} belongs to a different archive catalog; explicit full reset required")
    if manifest_catalog is None and manifest_fingerprint != legacy:
        raise RuntimeError(f"manifest {path} belongs to a different archive/account seed; explicit full reset required")
    manifest["source_catalog_fingerprint"] = catalog
    manifest["version"] = max(int(manifest.get("version") or 1), 3)
    manifest.setdefault("drive", {}).setdefault("folders", {})
    manifest.setdefault("drive", {}).setdefault("files", {})
    manifest.setdefault("gmail", {}).setdefault("messages", {})
    manifest.setdefault("calendar", {}).setdefault("events", {})
    manifest.setdefault("warnings", [])
    return manifest


def seed_persona(
    config_path: str | Path,
    *,
    persona: str,
    services: set[str],
    dry_run: bool,
    missing_attachment_policy: str | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    account = account_for_persona(config, persona)
    account_email = str(account["email"])
    archive = EnvironmentArchive(config["archive"])
    if persona not in archive.personas():
        raise ValueError(f"persona {persona!r} is not present in the environment archive")
    path = _manifest_path(config, account_email, persona, dry_run)
    manifest = load_manifest(path)
    source_fingerprint = _source_fingerprint(account_email, persona, archive)
    if manifest:
        manifest_fingerprint = manifest.get("source_fingerprint")
        if manifest_fingerprint is None:
            # Version 1 manifests used the deterministic source fingerprint as
            # their seed tag. Accept them so interrupted legacy runs can resume.
            manifest_fingerprint = manifest.get("seed_tag")
        if manifest_fingerprint != source_fingerprint:
            raise RuntimeError(f"existing manifest {path} belongs to a different archive/account seed")
    if manifest is None:
        manifest = new_manifest(
            account=account_email,
            persona=persona,
            archive=str(archive.path),
            seed_tag=_new_seed_tag(),
            source_fingerprint=source_fingerprint,
        )
    manifest["source_catalog_fingerprint"] = _source_catalog_fingerprint(account_email, persona, archive)

    def checkpoint() -> None:
        save_manifest(path, manifest)

    google = {"drive": None, "gmail": None, "calendar": None}
    profile = None
    if not dry_run and services & {"drive", "gmail", "calendar"}:
        _report(progress, "AUTHENTICATING", f"Authenticating {account_email} before baseline replay.")
        credentials = credentials_for_account(
            client_secret=config["client_secret"],
            token_dir=config["token_dir"],
            account_email=account_email,
            interactive=False,
        )
        google = services_for_credentials(credentials)
        profile = verify_account(google["gmail"], account_email)

    results: dict[str, Any] = {
        "account": account_email,
        "persona": persona,
        "dry_run": dry_run,
        "manifest": str(path),
        "authenticated_profile": profile,
    }
    if "drive" in services:
        _report(progress, "SEED_DRIVE", "Restoring the original Drive hierarchy and files.")
        results["drive"] = seed_drive(
            archive=archive,
            persona=persona,
            drive=google["drive"],
            manifest=manifest,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
    if "gmail" in services:
        _report(progress, "SEED_GMAIL", "Restoring the original Gmail baseline and drafts.")
        cache = Path(config["state_dir"]).expanduser().resolve() / "attachment-cache" / persona
        results["gmail"] = seed_gmail(
            archive=archive,
            persona=persona,
            gmail=google["gmail"],
            manifest=manifest,
            checkpoint=checkpoint,
            attachment_cache=cache,
            missing_policy=(
                missing_attachment_policy
                or missing_attachment_policy_for_persona(config, persona)
            ),
            dry_run=dry_run,
        )
    if "calendar" in services:
        _report(progress, "SEED_CALENDAR", "Restoring the original primary-calendar events.")
        results["calendar"] = seed_calendar(
            archive=archive,
            persona=persona,
            calendar=google["calendar"],
            manifest=manifest,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    results["warnings"] = manifest["warnings"]
    return results


def reconcile_persona(
    config_path: str | Path,
    *,
    persona: str,
    services: set[str],
    dry_run: bool,
    missing_attachment_policy: str | None = None,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    selected = set(services)
    unknown = selected - {"drive", "gmail", "calendar"}
    if unknown:
        raise ValueError(f"delta reconcile supports only Drive, Gmail, and Calendar here: {sorted(unknown)}")
    config = load_config(config_path)
    account = account_for_persona(config, persona)
    account_email = str(account["email"])
    archive = EnvironmentArchive(config["archive"])
    if persona not in archive.personas():
        raise ValueError(f"persona {persona!r} is not present in the environment archive")
    path = _manifest_path(config, account_email, persona, False)
    manifest = _validate_completed_manifest(
        path=path,
        manifest=load_manifest(path),
        account_email=account_email,
        persona=persona,
        archive=archive,
    )

    def checkpoint() -> None:
        save_manifest(path, manifest)

    google = {"drive": None, "gmail": None, "calendar": None}
    profile = None
    if not dry_run and selected:
        _report(progress, "AUTHENTICATING", f"Authenticating {account_email} before sparse restore.")
        credentials = credentials_for_account(
            client_secret=config["client_secret"],
            token_dir=config["token_dir"],
            account_email=account_email,
            interactive=False,
        )
        google = services_for_credentials(credentials)
        profile = verify_account(google["gmail"], account_email)
    elif dry_run and selected:
        # Dry-run still authenticates so the plan reflects live drift, but it
        # does not write. Existing tests may pass service fakes directly by
        # calling lower-level planners.
        _report(progress, "AUTHENTICATING", f"Authenticating {account_email} for read-only sparse restore preview.")
        credentials = credentials_for_account(
            client_secret=config["client_secret"],
            token_dir=config["token_dir"],
            account_email=account_email,
            interactive=False,
        )
        google = services_for_credentials(credentials)
        profile = verify_account(google["gmail"], account_email)

    results: dict[str, Any] = {
        "account": account_email,
        "persona": persona,
        "dry_run": dry_run,
        "manifest": str(path),
        "authenticated_profile": profile,
    }
    _report(progress, "DISCOVERING", "Comparing the current Google state with the verified baseline.")
    if "drive" in selected:
        _report(progress, "RECONCILE_DRIVE", "Inspecting and restoring only changed Drive items.")
        results["drive"] = reconcile_drive_delta(
            archive=archive,
            persona=persona,
            drive=google["drive"],
            manifest=manifest,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
    if "gmail" in selected:
        _report(progress, "RECONCILE_GMAIL", "Inspecting and restoring only changed Gmail items.")
        cache = Path(config["state_dir"]).expanduser().resolve() / "attachment-cache" / persona
        results["gmail"] = reconcile_gmail_delta(
            archive=archive,
            persona=persona,
            gmail=google["gmail"],
            manifest=manifest,
            checkpoint=checkpoint,
            attachment_cache=cache,
            missing_policy=(
                missing_attachment_policy
                or missing_attachment_policy_for_persona(config, persona)
            ),
            dry_run=dry_run,
        )
    if "calendar" in selected:
        _report(progress, "RECONCILE_CALENDAR", "Inspecting and restoring only changed Calendar events.")
        results["calendar"] = reconcile_calendar_delta(
            archive=archive,
            persona=persona,
            calendar=google["calendar"],
            manifest=manifest,
            checkpoint=checkpoint,
            dry_run=dry_run,
        )
    if not dry_run:
        _report(progress, "VERIFYING", "Reading the sparse restore result back from Google.")
        verify = verify_persona(config_path, persona=persona, services=selected)
        results["verify"] = verify
        if not verify.get("ok"):
            raise RuntimeError(
                "delta post-apply verification failed; no destructive fallback was run: "
                + json.dumps(_verification_summary(verify), sort_keys=True)
            )
    if not dry_run:
        checkpoint()
    return results


def verify_persona(
    config_path: str | Path,
    *,
    persona: str,
    services: set[str] | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    selected = services or {"drive", "gmail", "calendar"}
    unknown = selected - {"drive", "gmail", "calendar"}
    if unknown:
        raise ValueError(f"unknown verification services: {sorted(unknown)}")
    account = account_for_persona(config, persona)
    account_email = str(account["email"])
    path = _manifest_path(config, account_email, persona, False)
    manifest = load_manifest(path)
    if manifest is None:
        raise RuntimeError(f"no live manifest found at {path}")
    credentials = credentials_for_account(
        client_secret=config["client_secret"],
        token_dir=config["token_dir"],
        account_email=account_email,
        interactive=False,
    )
    google = services_for_credentials(credentials)
    profile = verify_account(google["gmail"], account_email)
    result: dict[str, Any] = {
        "account": account_email,
        "persona": persona,
        "profile": profile,
    }
    archive = EnvironmentArchive(config["archive"])
    if "drive" in selected:
        result["drive"] = verify_drive(
            google["drive"],
            manifest,
            archive=archive,
            persona=persona,
        )
    if "gmail" in selected:
        result["gmail"] = verify_gmail(
            google["gmail"],
            manifest,
            archive=archive,
            persona=persona,
        )
    if "calendar" in selected:
        result["calendar"] = verify_calendar(
            google["calendar"],
            manifest,
            expected_timezone=account.get("timezone"),
            archive=archive,
            persona=persona,
        )
    result["services"] = sorted(selected)
    result["ok"] = all(result[name]["ok"] for name in selected)
    return result


def reset_persona(
    config_path: str | Path,
    *,
    persona: str,
    services: set[str],
    confirm_account: str,
    dry_run: bool,
    progress: Callable[[str, str], None] | None = None,
) -> dict[str, Any]:
    config = load_config(config_path)
    account = account_for_persona(config, persona)
    account_email = str(account["email"])
    if confirm_account.casefold() != account_email.casefold():
        raise RuntimeError("--confirm-account must exactly match the configured test-account email")
    manifest_path = _manifest_path(config, account_email, persona, False)
    if dry_run:
        manifest = load_manifest(manifest_path)
        baseline = manifest or {
            "gmail": {"messages": {}},
            "calendar": {"events": {}},
            "drive": {"files": {}, "folders": {}},
        }
        return {
            "account": account_email,
            "persona": persona,
            "dry_run": True,
            "planned_services": sorted(services),
            "known_baseline_objects": {
                "gmail_messages": len(baseline["gmail"]["messages"]),
                "calendar_events": len(baseline["calendar"]["events"]),
                "drive_files": len(baseline["drive"]["files"]),
                "drive_folders": len(baseline["drive"]["folders"]),
            },
            "warning": (
                "offline reset preview cannot count model-created remote objects; "
                "live reset re-verifies the account and enumerates each service"
            ),
        }
    _report(progress, "AUTHENTICATING", f"Authenticating {account_email} before destructive reset.")
    credentials = credentials_for_account(
        client_secret=config["client_secret"],
        token_dir=config["token_dir"],
        account_email=account_email,
        interactive=False,
    )
    google = services_for_credentials(credentials)
    verify_account(google["gmail"], account_email)
    result: dict[str, Any] = {"account": account_email, "persona": persona, "dry_run": False}
    if "gmail" in services:
        _report(progress, "RESET_GMAIL", "Removing all Gmail messages, drafts, and user labels.")
        result["gmail"] = reset_gmail_all(google["gmail"], dry_run=False)
    if "calendar" in services:
        _report(progress, "RESET_CALENDAR", "Removing all primary-calendar events without notifications.")
        result["calendar"] = reset_calendar_all(google["calendar"], dry_run=False)
    if "drive" in services:
        _report(progress, "RESET_DRIVE", "Removing all owned Drive items, including trash.")
        result["drive"] = reset_drive_all(google["drive"], dry_run=False)
    if manifest_path.exists():
        manifest_path.unlink()
    return result


def authenticate(config_path: str | Path, *, account_email: str) -> dict[str, Any]:
    config = load_config(config_path)
    configured = {
        str(account["email"]).casefold()
        for account in config["accounts"].values()
        if account.get("email")
    }
    if account_email.casefold() not in configured:
        raise RuntimeError(
            f"account {account_email} is not configured; run "
            "`gab-seed accounts --config CONFIG_PATH` to list valid account emails"
        )
    credentials = credentials_for_account(
        client_secret=config["client_secret"],
        token_dir=config["token_dir"],
        account_email=account_email,
        interactive=True,
        persist=False,
    )
    google = services_for_credentials(credentials)
    try:
        profile = verify_account(google["gmail"], account_email)
    except AuthenticationError as exc:
        delete_credentials(token_dir=config["token_dir"], account_email=account_email)
        raise AuthenticationError(
            f"{exc}. No token was kept; rerun auth and explicitly select {account_email}."
        ) from exc
    save_credentials(
        credentials,
        token_dir=config["token_dir"],
        account_email=account_email,
    )
    return profile

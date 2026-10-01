from __future__ import annotations

from typing import Any

SEED = "seed"
DELTA = "delta"
RESEED = "reseed"


def last_seeded_persona(manifest: dict[str, Any] | None, email: str, row: dict[str, Any] | None = None) -> str:
    """Persona last written to this Google account.

    Supabase ``gab_accounts.last_reset_persona`` is the source of truth (same
    column the EC2 reset API uses). Local run state is only a fallback when
    Supabase is unset or the lookup fails.
    """
    email = (email or "").strip().lower()
    try:
        import db_hooks

        remote = db_hooks.last_reset_persona(email)
        if remote:
            return remote
    except Exception:
        pass
    by_email = (manifest or {}).get("last_persona_by_email") or {}
    found = str(by_email.get(email) or "").strip()
    if found:
        return found
    push = (row or {}).get("push") or {}
    return str(push.get("last_persona") or "").strip()


def decide_provision_mode(
    *,
    target_persona: str,
    last_persona: str | None,
    only_skipped: bool = False,
) -> str:
    """First load -> seed. Same persona reset -> delta. Persona switch -> reseed.

    ``only_skipped`` is a narrower recovery of the current persona (still not a wipe).
    """
    if only_skipped:
        return DELTA
    target = (target_persona or "").strip()
    last = (last_persona or "").strip()
    if not last:
        return SEED
    if last == target:
        return DELTA
    return RESEED


def apply_mode(mode: str) -> dict[str, Any]:
    """Flags the planner/pipeline use for each route."""
    if mode == RESEED:
        return {"mode": RESEED, "wipe": True, "delta": False}
    if mode == DELTA:
        return {"mode": DELTA, "wipe": False, "delta": True}
    return {"mode": SEED, "wipe": False, "delta": False}


def describe_mode(mode: str, *, last_persona: str = "", target_persona: str = "") -> str:
    if mode == SEED:
        return f"First load of {target_persona or 'this persona'} — full upload, no wipe"
    if mode == DELTA:
        return f"Same persona {target_persona or last_persona} — only missing or changed items"
    return (
        f"Persona changed {last_persona or '?'} → {target_persona or '?'} "
        "— wipe account, then full upload"
    )

"""Deterministic failure explainer for the onboard/reset UI.

Admins run uploads/resets but can't read raw engine strings like
``reset incomplete (reseed): 25 baseline item(s) failed permanently; first error:
HttpError 404 ... File not found: .`` — so they ping the platform owner, who SSHes into
EC2 and translates it. This module encodes that translation as a pure function so the UI
can show plain-English *what happened + what to do* without anyone touching journald.

It is deliberately a deterministic classifier (not an LLM): the failure set is finite and
known, and admins act on the output, so reliability beats flexibility. It reuses the
engine's existing hint tables (``engine._QUOTA_HINTS`` etc.) rather than duplicating them.
"""

from __future__ import annotations

from typing import Any

from . import engine

_CONSUMER_DOMAINS = {"gmail.com", "googlemail.com"}


def _agg(services: dict[str, Any]) -> tuple[int, int, int, bool]:
    """(failed, done, left, any_service_still_inflight) across gmail/calendar/drive."""
    failed = done = left = 0
    inflight = False
    for svc in (services or {}).values():
        if not isinstance(svc, dict):
            continue
        failed += int(svc.get("failed") or 0)
        done += int(svc.get("done") or 0)
        left += int(svc.get("left") or 0)
        if svc.get("state") in ("in_progress", "pending"):
            inflight = True
    return failed, done, left, inflight


def _evidence(raw_logs: str, limit: int = 6) -> list[str]:
    """A few log lines that justify the verdict — the error/quota/wipe lines, newest last."""
    out: list[str] = []
    for line in (raw_logs or "").splitlines():
        t = line.strip()
        if not t:
            continue
        low = t.lower()
        if any(k in low for k in (
            "error", "failed", "quota", "invalid_grant", "unauthorized",
            "404", "not found", "timed out", "timeout", "exception", "retry",
        )):
            out.append(t)
    return out[-limit:]


def _result(category, severity, headline, detail, action, *,
            retry_safe, self_heal, raw, evidence):
    return {
        "category": category,
        "severity": severity,          # ok | info | warning | error
        "headline": headline,
        "detail": detail,
        "action": action,
        "retry_safe": retry_safe,      # is clicking Retry/Upload safe?
        "self_heal": self_heal,        # will a plain retry most likely fix it?
        "raw": (raw or "").strip()[:600],
        "evidence": evidence,
    }


def diagnose(
    *,
    status: str | None,
    error: str | None = None,
    mode: str | None = None,
    persona: str | None = None,
    email: str | None = None,
    services: dict[str, Any] | None = None,
    verify: dict[str, Any] | None = None,
    raw_logs: str = "",
) -> dict[str, Any]:
    """Classify one upload/reset outcome into a plain-English, actionable explanation.

    Inputs mirror what the reset-session record + ``engine.read_progress`` already hold;
    ``raw_logs`` is an optional on-disk log tail that carries finer signatures (e.g.
    ``unauthorized_client`` / ``Invalid email or User ID``) the stored ``error`` drops.
    """
    services = services or {}
    err = error or ""
    low = (err + "\n" + (raw_logs or "")).lower()
    domain = email.split("@")[-1].lower() if email and "@" in email else ""
    is_consumer = domain in _CONSUMER_DOMAINS
    failed, done, left, inflight = _agg(services)
    ev = _evidence(raw_logs)

    def R(cat, sev, head, det, act, *, retry_safe=True, self_heal=False):
        return _result(cat, sev, head, det, act,
                       retry_safe=retry_safe, self_heal=self_heal, raw=err, evidence=ev)

    # --- healthy / display-only states -------------------------------------
    if (status or "") == "completed" and not err and failed == 0 and not inflight:
        return R("success", "ok", "Seeded successfully",
                 "The environment uploaded and verified with no failures.",
                 "Nothing to do.", retry_safe=False, self_heal=False)

    if (status or "") == "completed" and inflight:
        # Finished session, but the shared per-account counts belong to a later/overlapping
        # run of the same account — a display lag, not a real failure.
        return R("stale_display", "info", "Display lag — not a real failure",
                 "This upload finished, but the per-service counts shown come from the "
                 "account's shared job store, which a later run of the same account rebuilds.",
                 "Click Verify (or Refresh) to recount live; no re-upload needed.",
                 retry_safe=False, self_heal=True)

    # --- known failure signatures (most specific first) --------------------
    if "unknown persona" in low or "persona doesn't exist" in low \
            or "not present in the environment archive" in low:
        return R("unknown_persona", "error", "Persona name not recognized",
                 "The persona for this row isn't one the environment archive knows.",
                 "Fix the Persona column in the CSV to an exact known persona, then re-upload.",
                 retry_safe=False, self_heal=False)

    if "invalid email or user id" in low:
        return R("user_missing", "error", "That Workspace user doesn't exist yet",
                 "Google rejected impersonation because this user account isn't present in "
                 "the Workspace domain.",
                 "Create the user in the Google Admin console (or fix the email), then Retry.",
                 retry_safe=True, self_heal=False)

    if "unauthorized_client" in low or ("not authorized" in low and "scope" in low):
        return R("auth_dwd_grant", "error",
                 "Domain-wide delegation not granted for this domain",
                 "Google refused to mint a token for this account's domain — the service "
                 "account's client_id either isn't granted domain-wide delegation on this "
                 "Workspace, or is missing one of the required scopes (it's all-or-nothing).",
                 "In this domain's Admin console → API controls → Domain-Wide Delegation, add "
                 "the service account's client_id with all three scopes "
                 "(https://mail.google.com/ , calendar, drive), wait ~5 min, then Retry.",
                 retry_safe=True, self_heal=False)

    if ("baseline item" in low and ("404" in low or "file not found" in low)) \
            or "'root' in parents" in low:
        return R("transient_drive_404", "warning",
                 "Transient Google Drive hiccup under load",
                 "Google Drive briefly returned 404 on a folder lookup while many accounts "
                 "seeded at once. It's not a real missing file — the account is usually "
                 "almost fully seeded, with only the few items that hit the blip missing.",
                 "Click Retry skipped — a delta restores only the missing items and clears "
                 "this. If it was a whole subtree, re-running at lower concurrency helps.",
                 retry_safe=True, self_heal=True)

    if "quota" in low or engine._is_quota(low):
        return R("quota", "warning", "Google API rate limit",
                 "This account hit a Google per-user API rate/quota limit during seeding. "
                 "The pipeline auto-retries these, so it often clears on its own.",
                 "If the row still failed, Retry later or run a smaller batch; a hard daily "
                 "quota resets within 24h.",
                 retry_safe=True, self_heal=True)

    if engine._needs_full_reset(low) or any(h in low for h in engine._DELTA_SAFETY_HINTS):
        return R("delta_safety", "warning", "Delta retry isn't safe for this account",
                 "The account's current state conflicts with the baseline (e.g. cross-seed or "
                 "ambiguous items), so a delta/restore-missing can't proceed safely.",
                 "Run a full reseed for this account instead of a delta/Retry-skipped.",
                 retry_safe=False, self_heal=False)

    if any(h in low for h in engine._VERIFY_FAIL_HINTS) \
            or "verify: empty" in low or "verify failed" in low:
        return R("verify_failed", "warning", "Seed ran but verification is short",
                 "The seed applied but the post-apply verification found fewer items than "
                 "expected — often an eventual-consistency lag on Google's side.",
                 "Click Verify again in a minute; if still short, Retry skipped to refill.",
                 retry_safe=True, self_heal=True)

    # Generic auth: the stored error is genericized ('sign-in expired'); split by domain.
    if engine._is_auth(low) or "sign-in expired" in low or "re-authorize" in low:
        if is_consumer:
            return R("auth_expired", "error", "Google sign-in expired",
                     "This consumer (gmail.com) account's saved OAuth token expired or was "
                     "revoked, so seeding couldn't authenticate.",
                     "Re-authorize this account on /onboard, then Retry.",
                     retry_safe=True, self_heal=False)
        return R("auth_dwd_grant", "error",
                 "Account not authorized for this domain",
                 "Token minting failed for this Workspace account. The usual causes are the "
                 "service account's domain-wide delegation not being granted on this domain "
                 "(or missing a scope), or the user not existing yet.",
                 "Confirm the SA's client_id is granted with all three scopes in this domain's "
                 "Admin console and that the user exists; wait ~5 min after granting, then Retry.",
                 retry_safe=True, self_heal=False)

    # Partial: some items failed but most succeeded.
    if failed and done and failed < done:
        return R("partial_success", "warning", "Mostly seeded — a few items missing",
                 f"{failed} item(s) failed while {done} succeeded. The environment is "
                 "largely in place; only the failed items are missing.",
                 "Click Retry skipped — a delta restores only the missing items.",
                 retry_safe=True, self_heal=True)

    if engine._is_transient(low) or "timed out" in low or "timeout" in low:
        return R("timeout", "warning", "Transient network/timeout",
                 "The run hit a network stall or timeout, not a data or auth problem.",
                 "Retry — this is safe and usually succeeds on a second attempt.",
                 retry_safe=True, self_heal=True)

    # --- fallthrough -------------------------------------------------------
    if not err and (status or "") not in ("failed", "completed"):
        return R("in_progress", "info", "Still running",
                 "This account is still seeding — no failure yet.",
                 "Wait for it to finish, then check again.",
                 retry_safe=False, self_heal=False)

    return R("unknown", "error", "Unrecognized error",
             "This failure didn't match a known cause.",
             "Review the raw error and log tail below. A Retry is usually safe; if it "
             "persists, escalate with the raw error.",
             retry_safe=True, self_heal=False)

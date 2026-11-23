"""Seed / restore / wipe a Wave 2 persona into one account, for the reset platform.

The seeding itself is Google's reference GAB v2 seeder (seeder/pilot10/gab_seed.py),
driven here with the platform's own credentials instead of the pilot CLI's token files:

  * seed     Gmail (labels, threads, read/sent/starred, drafts), Contacts, Calendar
             (secondary calendars, recurrence, time zones), Drive (folders, Docs/Sheets
             from text, PDF/Office via export), Tasks; then a baseline snapshot.
  * restore  back to that baseline: delete what an agent added, recreate anything changed
             or missing (the Wave 2 equivalent of reconcile/delta).
  * wipe     everything reachable by API: the production full wipes for Gmail, Drive and
             the primary calendar, plus drafts, user labels, secondary calendars and
             subscriptions, contacts and contact groups, task lists and tasks, Drive trash
             and files shared in by other people.

Keep and Google Chat have no consumer API; they are counted and reported as manual.

State (the pilot's per-item record + baseline) lives in <state root>/_seed_state/,
one file per (persona, account).
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
import types
from pathlib import Path
from typing import Any, Callable

from wave2.creds import Wave2AuthError, wave2_credentials
from wave2.personas import WorldCtx

LOG = logging.getLogger("wave2")
PILOT_DIR = Path(__file__).resolve().parent.parent / "pilot10"
SURFACES = ("GMAIL", "CALENDAR", "DRIVE", "CONTACTS", "TASKS")

_base_lock = threading.Lock()
_base_mod = None
_TEXTISH = {
    "application/typescript", "application/javascript", "application/x-javascript",
    "application/x-python", "application/x-sh", "application/xml", "application/yaml",
    "application/x-yaml", "application/sql", "application/x-httpd-php",
}


def state_root() -> Path:
    raw = (os.environ.get("GAB_WAVE2_STATE_DIR") or "").strip()
    if raw:
        root = Path(raw).expanduser()
    elif (os.environ.get("GAB_STATE_DIR") or "").strip():
        root = Path(os.environ["GAB_STATE_DIR"]).expanduser() / "wave2"
    else:
        root = Path(__file__).resolve().parent.parent / "runs" / "wave2"
    root.mkdir(parents=True, exist_ok=True)
    return root


def base():
    """The pilot seeder module, configured once for in-process use.

    Only stateless module globals are set here (state directory root and the log sink),
    so concurrent resets for different accounts are safe: every per-run value travels in
    the ctx / services / state arguments.
    """
    global _base_mod
    with _base_lock:
        if _base_mod is None:
            if str(PILOT_DIR) not in sys.path:
                sys.path.insert(0, str(PILOT_DIR))
            import gab_seed  # noqa: PLC0415 - Google's reference seeder, unchanged

            gab_seed.log = lambda msg="": LOG.info("%s", msg)
            # Source files with application/* MIME types are plain text; the reference
            # seeder only treats text/* as text and would skip them.
            _orig_kind = gab_seed.file_kind

            def _file_kind(f):
                kind = _orig_kind(f)
                return "text" if kind == "skip" and f.get("mime_type") in _TEXTISH else kind

            gab_seed.file_kind = _file_kind
            # Every seeded/restored item ends with save_state(ctx, st); piggy-back on it to
            # publish live per-surface counts for the Wave 2 page (throttled, per account).
            _orig_save = gab_seed.save_state

            def _save_and_tick(ctx, st):
                _orig_save(ctx, st)
                _tick(ctx, st)

            gab_seed.save_state = _save_and_tick
            _base_mod = gab_seed
        # Re-read every time (same value in production) so the state dir always follows
        # the current configuration instead of whatever it was at first import.
        _base_mod.ROOT = str(state_root())
        return _base_mod


# ----------------------------------------------------------------- live progress
# One small JSON per (persona, account) under <state root>/progress/, read by the Wave 2
# page. Written at stage changes and (throttled) after every seeded item. Never affects
# the reset itself: any write error is swallowed.
_LIVE: dict[str, dict[str, Any]] = {}
_TICK_EVERY_S = 1.5


def progress_path(key: str) -> Path:
    return state_root() / "progress" / f"{key}.json"


def write_progress(key: str, **fields: Any) -> None:
    try:
        p = progress_path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        cur = {}
        if p.exists():
            try:
                cur = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                cur = {}
        cur.update(fields)
        cur["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cur, default=str), encoding="utf-8")
        os.replace(tmp, p)
    except Exception:  # noqa: BLE001 - progress is cosmetic; never break a reset over it
        LOG.debug("wave2 progress write failed", exc_info=True)


def read_progress_file(key: str) -> dict[str, Any] | None:
    p = progress_path(key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


def _tick(ctx, st) -> None:
    live = _LIVE.get(getattr(ctx, "persona", ""))
    if not live:
        return
    now = time.monotonic()
    if now - live.get("last", 0.0) < _TICK_EVERY_S:
        return
    live["last"] = now
    write_progress(ctx.persona, counts=seeded_counts(st))


def _stage(ctx, stage: str, st: dict[str, Any] | None = None) -> None:
    fields: dict[str, Any] = {"stage": stage}
    if st is not None:
        fields["counts"] = seeded_counts(st)
    write_progress(ctx.persona, **fields)


def services(creds) -> types.SimpleNamespace:
    """Google clients the pilot functions expect, built with the platform's HTTP timeout
    (a plain discovery client can hang forever on a half-open socket)."""
    from materialize.auth import build_service

    return types.SimpleNamespace(
        gmail=build_service("gmail", "v1", creds),
        cal=build_service("calendar", "v3", creds),
        drive=build_service("drive", "v3", creds),
        sheets=build_service("sheets", "v4", creds),
        people=build_service("people", "v1", creds),
        tasks=build_service("tasks", "v1", creds),
    )


# ----------------------------------------------------------------- plan / verify
def plan(ctx: WorldCtx) -> dict[str, Any]:
    """Offline: what the world holds and how much of it is recreatable per surface."""
    b = base()
    bc = ctx.bc
    events = b.w_events(bc)
    timed = [e for e in events if b.ev_time(e.get("start"), ctx.tz_name)]
    files = b.w_files(bc)
    drive_ok = [f for f in files if b.file_kind(f) != "skip"]
    lists, notes = b.w_keep(bc)
    chat_msgs = sum(len(it.get("chat_messages") or []) for it in b.w_chat(bc))
    return {
        "expected": {
            "GMAIL": len(b.w_emails(bc)),
            "CALENDAR": len(timed),
            "DRIVE": len(b.w_folders(bc)) + len(drive_ok),
            "CONTACTS": len(b.w_contacts(bc)),
            "TASKS": len(b.w_tasks(bc)),
        },
        "not_recreatable": {
            "drive_files": [f"{f.get('name')} ({f.get('mime_type')})" for f in files if b.file_kind(f) == "skip"],
            "calendar_events_without_start": len(events) - len(timed),
        },
        "manual": {"keep_items": len(lists) + len(notes), "chat_messages": chat_msgs},
    }


def seeded_counts(st: dict[str, Any]) -> dict[str, int]:
    return {
        "GMAIL": len(st.get("gmail") or {}),
        "CALENDAR": len(st.get("events") or {}),
        "DRIVE": len(st.get("drive_folders") or {}) + len(st.get("drive_files") or {}),
        "CONTACTS": len(st.get("contacts") or {}),
        "TASKS": len(st.get("tasks") or {}),
    }


def verify(ctx: WorldCtx, st: dict[str, Any]) -> dict[str, Any]:
    p = plan(ctx)
    got = seeded_counts(st)
    per = {s: {"expected": p["expected"][s], "seeded": got[s], "ok": got[s] >= p["expected"][s]} for s in SURFACES}
    return {"surfaces": per, "ok": all(v["ok"] for v in per.values()), "manual": p["manual"],
            "not_recreatable": p["not_recreatable"]}


# ----------------------------------------------------------------- wipe
def _step(label: str, fn: Callable[[], Any], errors: list[str], log: Callable[[str], None]) -> Any:
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - one surface failing must not stop the rest
        errors.append(f"{label}: {type(exc).__name__}: {exc}")
        log(f"wave2 wipe: could not {label}: {type(exc).__name__}: {exc}")
        return None


def extra_wipe(S, log: Callable[[str], None]) -> dict[str, Any]:
    """The parts of a full wipe the production clean does not cover (all API-reachable)."""
    b = base()
    out: dict[str, Any] = {"errors": []}
    err = out["errors"]
    g = S.gmail.users()

    def drafts() -> int:
        n = 0
        for d in b.pages(g.drafts().list, "drafts", userId="me", maxResults=500):
            b.ex_ok(g.drafts().delete(userId="me", id=d["id"]))
            n += 1
        return n

    def labels() -> int:
        n = 0
        for lab in (b.ex(g.labels().list(userId="me")) or {}).get("labels", []):
            if lab.get("type") == "user":
                b.ex_ok(g.labels().delete(userId="me", id=lab["id"]))
                n += 1
        return n

    def calendars() -> dict[str, int]:
        owned = subs = 0
        for c in b.list_calendars(S):
            if c.get("primary"):
                continue
            if c.get("accessRole") == "owner":
                b.ex_ok(S.cal.calendars().delete(calendarId=c["id"]))
                owned += 1
            else:
                b.ex_ok(S.cal.calendarList().delete(calendarId=c["id"]))
                subs += 1
        return {"secondary_deleted": owned, "subscriptions_removed": subs}

    def contacts() -> int:
        rns = [p["resourceName"] for p in b.list_contacts(S)]
        for i in range(0, len(rns), 500):
            b.ex(S.people.people().batchDeleteContacts(body={"resourceNames": rns[i:i + 500]}))
        return len(rns)

    def contact_groups() -> int:
        n = 0
        for grp in b.pages(S.people.contactGroups().list, "contactGroups", pageSize=1000):
            if grp.get("groupType") == "USER_CONTACT_GROUP":
                b.ex_ok(S.people.contactGroups().delete(resourceName=grp["resourceName"]))
                n += 1
        return n

    def tasks() -> int:
        lists = b.pages(S.tasks.tasklists().list, "items", maxResults=100)
        for lst in lists[1:]:
            b.ex_ok(S.tasks.tasklists().delete(tasklist=lst["id"]))
        n = 0
        for t in b.list_tasks(S, "@default"):
            if not t.get("deleted"):
                b.ex_ok(S.tasks.tasks().delete(tasklist="@default", task=t["id"]))
                n += 1
        b.ex_ok(S.tasks.tasks().clear(tasklist="@default"))
        return n

    def shared_in() -> int:
        me = (b.ex(S.drive.about().get(fields="user(permissionId)")) or {}).get("user", {}).get("permissionId")
        n = 0
        if not me:
            return 0
        for f in b.pages(S.drive.files().list, "files", q="sharedWithMe=true and trashed=false",
                         pageSize=1000, fields="nextPageToken,files(id)"):
            try:
                b.ex(S.drive.permissions().delete(fileId=f["id"], permissionId=me))
                n += 1
            except Exception:  # noqa: BLE001 - link-shared files have no per-user permission to drop
                pass
        return n

    out["gmail_drafts"] = _step("delete Gmail drafts", drafts, err, log)
    out["gmail_user_labels"] = _step("delete Gmail labels", labels, err, log)
    out["calendars"] = _step("remove secondary calendars", calendars, err, log)
    out["contacts"] = _step("delete contacts", contacts, err, log)
    out["contact_groups"] = _step("delete contact groups", contact_groups, err, log)
    out["tasks"] = _step("delete tasks", tasks, err, log)
    out["drive_shared_in_removed"] = _step("remove shared-in Drive files", shared_in, err, log)
    return out


def full_wipe(S, creds, log: Callable[[str], None]) -> dict[str, Any]:
    """Everything API-reachable. Reuses the production full wipes (proven, parallel)."""
    from materialize.calendar_sync import wipe_seeded_events
    from materialize.drive_sync import wipe_seed_folder
    from materialize.gmail_sync import wipe_seeded_mail

    errors: list[str] = []
    out: dict[str, Any] = {}
    out["gmail"] = _step("wipe Gmail", lambda: wipe_seeded_mail(S.gmail, log), errors, log)
    out["calendar_primary"] = _step("clear primary calendar", lambda: wipe_seeded_events(S.cal, log), errors, log)
    out["drive"] = _step("wipe Drive", lambda: wipe_seed_folder(S.drive, "", log, creds=creds, workers=10), errors, log)
    _step("empty Drive trash", lambda: S.drive.files().emptyTrash().execute(), errors, log)
    extra = extra_wipe(S, log)
    errors += extra.pop("errors")
    out.update(extra)
    out["errors"] = errors
    return out


# ----------------------------------------------------------------- seed / restore
def _archive_state(ctx: WorldCtx) -> None:
    p = Path(base().state_dir()) / f"{ctx.persona}.json"
    if p.exists():
        p.rename(p.with_name(p.name + f".wiped-{int(time.time())}"))


def seed(ctx: WorldCtx, S, st: dict[str, Any]) -> None:
    b = base()
    st["folder"], st["persona_id"] = ctx.folder, ctx.persona_id
    for stage, fn in (("gmail", b.seed_gmail), ("contacts", b.seed_contacts), ("calendar", b.seed_calendar),
                      ("drive", b.seed_drive), ("tasks", b.seed_tasks)):
        _stage(ctx, stage, st)
        fn(ctx, S, st)
    _stage(ctx, "baseline", st)
    try:
        b.delete_security_alerts(S)  # the seeding sign-in triggers Google "Security alert" mail
    except Exception as exc:  # noqa: BLE001 - cosmetic, never fail the seed on it
        LOG.warning("wave2: could not remove security-alert mail: %s", exc)
    time.sleep(10)  # let Contacts/Drive settle before the baseline snapshot
    b.snapshot(ctx, S, st)


def restore(ctx: WorldCtx, S, st: dict[str, Any]) -> None:
    b = base()
    for stage, fn in (("gmail", b.restore_gmail), ("contacts", b.restore_contacts),
                      ("calendar", b.restore_calendar), ("drive", b.restore_drive), ("tasks", b.restore_tasks)):
        _stage(ctx, "restore_" + stage, st)
        fn(ctx, S, st)
        b.save_state(ctx, st)
    _stage(ctx, "baseline", st)
    time.sleep(5)
    b.snapshot(ctx, S, st)


def _summary(folder: str, mode: str, v: dict[str, Any]) -> str:
    parts = [f"{s.lower()} {x['seeded']}/{x['expected']}" for s, x in v["surfaces"].items()]
    man = v["manual"]
    tail = f"; manual (no API): keep {man['keep_items']} items, chat {man['chat_messages']} messages"
    return f"wave2 {mode} {folder}: " + ", ".join(parts) + tail


def run(email: str, folder: str, mode: str, log: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Bring ``email`` to the Wave 2 persona ``folder``.

    seed / reseed / upload -> full wipe, seed, baseline.
    delta / reconcile      -> restore to baseline (or a full reseed if there is none yet).

    Returns {ok, detail, mode, action, verify, wipe?, skipped}. Raises Wave2AuthError when
    the account must be re-authorized first.
    """
    log = log or (lambda m: LOG.info("%s", m))
    b = base()
    ctx = WorldCtx(folder, email)
    p = plan(ctx)
    settings_hint = {"name": ctx.persona_name, "home_address": ctx.home_address(), "timezone": ctx.tz_name}
    # Fresh progress file for this run (replaces the previous run's).
    try:
        progress_path(ctx.persona).unlink(missing_ok=True)
    except OSError:
        pass
    write_progress(ctx.persona, email=ctx.account, persona=folder, mode=mode, stage="starting",
                   expected=p["expected"], manual=p["manual"], account_settings=settings_hint,
                   started_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), ok=None, detail=None)
    _LIVE[ctx.persona] = {"last": 0.0}
    try:
        out = _run(ctx, folder, mode, log)
    except Exception as exc:
        write_progress(ctx.persona, stage="failed", ok=False, detail=f"{type(exc).__name__}: {exc}"[:400])
        raise
    finally:
        _LIVE.pop(ctx.persona, None)
    write_progress(ctx.persona, stage="done" if out["ok"] else "failed", ok=out["ok"], detail=out["detail"],
                   action=out["action"], counts=seeded_counts_from(out), verify=out["verify"]["surfaces"])
    return out


def seeded_counts_from(out: dict[str, Any]) -> dict[str, int]:
    return {s: v["seeded"] for s, v in ((out.get("verify") or {}).get("surfaces") or {}).items()}


def _run(ctx: WorldCtx, folder: str, mode: str, log: Callable[[str], None]) -> dict[str, Any]:
    b = base()
    _stage(ctx, "checking access")
    creds = wave2_credentials(ctx.account)
    S = services(creds)
    prof = b.ex(S.gmail.users().getProfile(userId="me")) or {}
    got = (prof.get("emailAddress") or "").lower()
    if got and got != ctx.account:
        raise Wave2AuthError(f"token belongs to {got}, not {ctx.account}")

    st = b.load_state(ctx)
    has_baseline = bool(st.get("baseline")) and st.get("folder") == folder
    action = "restore" if mode in ("delta", "reconcile") and has_baseline else "reseed"
    write_progress(ctx.persona, action=action)
    log(f"wave2 {action} start email={ctx.account} persona={folder} mode={mode} tz={ctx.tz_name} "
        f"persona_email={ctx.persona_email or '-'} -> rewritten to account email")

    out: dict[str, Any] = {"mode": mode, "action": action, "persona": folder}
    if action == "restore":
        restore(ctx, S, st)
    else:
        _stage(ctx, "wipe")
        out["wipe"] = full_wipe(S, creds, log)
        _archive_state(ctx)
        st = b.load_state(ctx)
        seed(ctx, S, st)

    _stage(ctx, "verify", st)
    v = verify(ctx, st)
    out["verify"] = v
    out["skipped"] = dict(st.get("skipped") or {})
    wipe_errors = (out.get("wipe") or {}).get("errors") or []
    out["ok"] = v["ok"] and not wipe_errors
    out["detail"] = _summary(folder, action, v)
    if wipe_errors:
        out["detail"] += f"; wipe errors: {'; '.join(wipe_errors)[:300]}"
    out["account_settings"] = {"name": ctx.persona_name, "home_address": ctx.home_address(),
                               "timezone": ctx.tz_name}
    return out


def clean_extras(email: str, log: Callable[[str], None] | None = None) -> dict[str, Any] | None:
    """Extra wipe for the clean (empty-universe) path. None when the account has no Wave 2
    grant (nothing of these surfaces can exist via us, and nothing to do)."""
    log = log or (lambda m: LOG.info("%s", m))
    try:
        creds = wave2_credentials(email)
    except Wave2AuthError:
        return None
    S = services(creds)
    return extra_wipe(S, log)


# ----------------------------------------------------------------- page helpers (read-only)
_SUMMARY_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def persona_summaries() -> list[dict[str, Any]]:
    """Per Wave 2 persona: display name, timezone, what will be seeded, what is manual.
    Cached per persona until its world file changes."""
    from wave2.personas import WORLD_REL, persona_folders, wave2_root

    root = wave2_root()
    if root is None:
        return []
    out = []
    for folder in persona_folders():
        mtime = (root / folder / WORLD_REL).stat().st_mtime
        hit = _SUMMARY_CACHE.get(folder)
        if not hit or hit[0] != mtime:
            ctx = WorldCtx(folder, "")
            p = plan(ctx)
            hit = (mtime, {"folder": folder, "name": ctx.persona_name, "timezone": ctx.tz_name,
                           "home_address": ctx.home_address(),
                           "expected": p["expected"], "manual": p["manual"],
                           "drive_not_recreatable": len(p["not_recreatable"]["drive_files"])})
            _SUMMARY_CACHE[folder] = hit
        out.append(hit[1])
    return out


def account_status(email: str) -> dict[str, Any]:
    """Does this account's saved grant include the Wave 2 scopes (Contacts + Tasks)?"""
    try:
        wave2_credentials(email)
        return {"wave2_ready": True, "reason": None}
    except Wave2AuthError as exc:
        return {"wave2_ready": False, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - network hiccup: unknown, not "not ready"
        return {"wave2_ready": None, "reason": f"{type(exc).__name__}: {exc}"[:200]}


def progress_for(email: str, persona: str) -> dict[str, Any] | None:
    from wave2.personas import match, state_key

    folder = match(persona)
    if not folder:
        return None
    return read_progress_file(state_key(folder, email.strip().lower()))

"""Wave 2 (GAB v2 turn-split) persona discovery and the seeding context.

A Wave 2 persona is a folder under ``GAB_WAVE2_ROOT``::

    <GAB_WAVE2_ROOT>/<persona_id>/manifest.json
    <GAB_WAVE2_ROOT>/<persona_id>/turn_1/turn_1_world.json

The world file holds ``persona`` (profile, ``persona_email``), ``bard_contexts`` (one JSON
string per surface: GMAIL, CALENDAR, DRIVE, CONTACTS, TASKS, KEEP, GOOGLE_CHAT) and
``descriptive_contexts`` (city, country, timezone).

This module is pure (no Google calls) so the API can use it to decide routing and which
OAuth scopes to request. When ``GAB_WAVE2_ROOT`` is unset or missing, every function
reports "no Wave 2 personas" and the platform behaves exactly as before.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

WORLD_REL = Path("turn_1") / "turn_1_world.json"

# Leading honorifics in the tasking sheet's "Synthetic person" column
# ("Dr. Marcus Vance", "Chief Joseph Sholotan") that are not part of the folder name.
_TITLES = ("dr", "chief", "mr", "mrs", "ms", "miss", "prof", "professor", "rev", "pastor")


def wave2_root() -> Path | None:
    """The Wave 2 persona root, or None when the feature is not configured."""
    raw = (os.environ.get("GAB_WAVE2_ROOT") or "").strip()
    if not raw:
        return None
    root = Path(raw).expanduser()
    return root if root.is_dir() else None


def enabled() -> bool:
    return wave2_root() is not None


def normalize(name: str | None) -> str:
    """Loose persona key: case/space/punctuation-insensitive, honorifics and the
    ' : <archetype>' tail dropped. 'Dr. Claire Sterling-Vance : The ...' ->
    'claire_sterling_vance'."""
    s = (name or "").split(":", 1)[0].strip().lower()
    words = re.split(r"[^a-z0-9]+", s)
    words = [w for w in words if w]
    while words and words[0] in _TITLES:
        words = words[1:]
    return "_".join(words)


def persona_folders() -> list[str]:
    root = wave2_root()
    if root is None:
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and (p / WORLD_REL).is_file())


def match(persona: str | None) -> str | None:
    """The Wave 2 folder for a loosely-typed persona name, or None (not a Wave 2 persona)."""
    want = normalize(persona)
    if not want:
        return None
    for folder in persona_folders():
        if normalize(folder) == want:
            return folder
    return None


def _as_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return {}
    return value or {}


def load_world(folder: str) -> dict[str, Any]:
    """Parse a persona folder: world, bard_contexts (decoded), descriptive context, manifest."""
    root = wave2_root()
    if root is None:
        raise FileNotFoundError("GAB_WAVE2_ROOT is not configured")
    pdir = root / folder
    world = json.loads((pdir / WORLD_REL).read_text(encoding="utf-8"))
    manifest_path = pdir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.is_file() else {}
    bc = {k: _as_json(v) for k, v in (world.get("bard_contexts") or {}).items()}
    normalize_contacts(bc)
    normalize_attendees(bc, ((world.get("persona") or {}).get("persona_email") or ""))
    merge_duplicate_folders(bc)
    collapse_recurring_events(bc)
    desc = {}
    for v in (world.get("descriptive_contexts") or {}).values():
        d = _as_json(v)
        if isinstance(d, dict):
            desc.update(d)
    return {"pdir": pdir, "world": world, "manifest": manifest, "bc": bc, "descriptive": desc}


def normalize_attendees(bc: dict[str, Any], persona_email: str = "") -> int:
    """Bring calendar attendees to the shape the reference seeder understands, in place.

    Events use either ``attendee: [{email, display_name, is_self}]`` or (8 of 13 Wave 2
    personas) ``attendees: ["a@x.com", ...]`` -- plain strings. Strings become
    ``{"email": s}``; the persona's own address is marked ``is_self`` so it is not invited.
    Returns how many attendees were converted."""
    me = (persona_email or "").strip().lower()
    fixed = 0

    def walk(o: Any) -> None:
        nonlocal fixed
        if isinstance(o, dict):
            for key in ("attendees", "attendee"):
                lst = o.get(key)
                if isinstance(lst, list) and any(isinstance(a, str) for a in lst):
                    out = []
                    for a in lst:
                        if isinstance(a, str):
                            if not a.strip():
                                continue
                            a = {"email": a.strip(), **({"is_self": True} if a.strip().lower() == me else {})}
                            fixed += 1
                        out.append(a)
                    o[key] = out
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(bc.get("CALENDAR") or {})
    _dedupe_event_ids(bc.get("CALENDAR") or {})
    return fixed


def _dedupe_event_ids(cal: Any) -> int:
    """A few events share an ``id`` but are different events (other date/calendar), e.g.
    Marcus's three 'Lift - Block 6' sessions. The seeder keys events by id and would keep
    only the first, so a repeat gets a stable suffix ('<id>__2'). Deterministic (document
    order), so seed and restore always agree. Returns how many ids were changed."""
    seen: dict[str, int] = {}
    changed = 0

    def walk(o: Any) -> None:
        nonlocal changed
        if isinstance(o, dict):
            if isinstance(o.get("id"), str) and "start" in o and "summary" in o:
                n = seen.get(o["id"], 0) + 1
                seen[o["id"]] = n
                if n > 1:
                    o["id"] = f"{o['id']}__{n}"
                    changed += 1
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(cal)
    return changed


def _event_start(e: dict[str, Any]) -> tuple[int, int] | None:
    """(utc ms, offset ms) of an event's start, or None."""
    dt = ((e.get("start") or {}).get("date_time") or {}) if isinstance(e.get("start"), dict) else {}
    if isinstance(dt, dict) and dt.get("time_ms") is not None:
        return int(dt["time_ms"]), int(dt.get("offset_ms") or 0)
    return None


def collapse_recurring_events(bc: dict[str, Any]) -> int:
    """One Google series per repeating event, in place.

    The Wave 2 worlds store every weekly/daily occurrence of a repeating event as its own
    record, each carrying the full RRULE (Adrian's Sunday long run: 8 records, each
    'RRULE:FREQ=WEEKLY;BYDAY=SU'). Creating each one makes 8 overlapping series, so every
    Sunday shows 8 copies. Records of the same series (calendar, title, rule, local time of
    day, length — and weekday when the rule has no BYDAY) keep only the earliest, which
    already produces all the later occurrences. Returns how many records were dropped."""
    import datetime as _dt

    cal = bc.get("CALENDAR")
    events = ((cal or {}).get("calendar_events_info") or {}).get("events") if isinstance(cal, dict) else None
    if not isinstance(events, list):
        return 0
    first: dict[tuple, dict[str, Any]] = {}
    drop: set[int] = set()
    for i, e in enumerate(events):
        if not isinstance(e, dict):
            continue
        rules = [r.strip() for r in (e.get("recurrence_data") or e.get("recurrence") or []) if isinstance(r, str) and r.strip()]
        st = _event_start(e)
        if not rules or st is None:
            continue
        local = _dt.datetime.utcfromtimestamp((st[0] + st[1]) / 1000)
        en = (e.get("end") or {}).get("date_time") if isinstance(e.get("end"), dict) else None
        length = (int(en["time_ms"]) - st[0]) if isinstance(en, dict) and en.get("time_ms") is not None else None
        by_day = any("BYDAY=" in r or "BYMONTHDAY=" in r or "FREQ=DAILY" in r for r in rules)
        key = (e.get("calendar_id"), " ".join(str(e.get("summary") or "").split()).lower(), tuple(sorted(rules)),
               local.strftime("%H:%M"), length, None if by_day else local.weekday())
        keep = first.get(key)
        if keep is None:
            first[key] = {"i": i, "t": st[0]}
        elif st[0] < keep["t"]:
            drop.add(keep["i"])
            first[key] = {"i": i, "t": st[0]}
        else:
            drop.add(i)
    if drop:
        cal["calendar_events_info"]["events"] = [e for i, e in enumerate(events) if i not in drop]
    return len(drop)


def _drive_ctx(bc: dict[str, Any]) -> dict[str, Any]:
    d = bc.get("DRIVE") or {}
    if not isinstance(d, dict):
        return {}
    ctx = (d.get("mock_context") or {}).get("drive_mock_context") or d.get("drive_mock_context") or d
    return ctx if isinstance(ctx, dict) else {}


def folder_merge_map(folders: list[dict[str, Any]]) -> dict[str, str]:
    """{duplicate folder_id: kept folder_id} for folders with the same name under the same
    parent path (e.g. Adrian's 'Client Marketing' x3: the persona's fld_11 plus a copy from
    each of two task files). The first folder in document order is kept, so the persona's own
    folders win and the result is the same on every run (seed, restore and verify agree)."""
    by_id = {f.get("folder_id"): f for f in folders if isinstance(f, dict) and f.get("folder_id")}

    def path(fid: str, depth: int = 0) -> str:
        f = by_id.get(fid)
        if f is None or depth > 50:
            return ""
        parent = f.get("parent_folder_id")
        return f"{path(parent, depth + 1) if parent in by_id else ''}/{(f.get('name') or '').strip().lower()}"

    kept: dict[str, str] = {}
    out: dict[str, str] = {}
    for f in folders:
        fid = f.get("folder_id") if isinstance(f, dict) else None
        if not fid:
            continue
        p = path(fid)
        if p in kept:
            out[fid] = kept[p]
        else:
            kept[p] = fid
    return out


def merge_duplicate_folders(bc: dict[str, Any]) -> int:
    """One Drive folder per name and place, in place. Files and subfolders of a duplicate move
    to the kept folder; the duplicate is dropped. Folder and file names are unchanged.
    Returns how many folders were dropped."""
    ctx = _drive_ctx(bc)
    folders = ctx.get("folders")
    if not isinstance(folders, list):
        return 0
    merge = folder_merge_map(folders)
    if not merge:
        return 0
    ctx["folders"] = [f for f in folders if not (isinstance(f, dict) and f.get("folder_id") in merge)]
    for item in ctx["folders"] + [f for f in (ctx.get("files") or []) if isinstance(f, dict)]:
        if isinstance(item, dict) and item.get("parent_folder_id") in merge:
            item["parent_folder_id"] = merge[item["parent_folder_id"]]
    return len(merge)


def normalize_contacts(bc: dict[str, Any]) -> int:
    """Bring every contact to the shape the reference seeder understands, in place.

    Most Wave 2 contacts are ``{contact_id, name, endpoints:[{value,label}]}``, but a few use
    ``{name, emails:[], phone_numbers:[], relationship}`` with no id (6 across 3 personas).
    Those get a STABLE id derived from their content (so re-runs and restores match the same
    contact), their emails/phones become endpoints, and the relationship becomes the note.
    Returns how many contacts were converted.
    """
    import hashlib

    contacts = ((bc.get("CONTACTS") or {}).get("contact_lookup_result") or {}).get("contacts") or []
    fixed = 0
    for c in contacts:
        if not isinstance(c, dict) or c.get("contact_id"):
            continue
        emails = [str(e).strip() for e in (c.get("emails") or []) if str(e).strip()]
        phones = [str(p).strip() for p in (c.get("phone_numbers") or []) if str(p).strip()]
        if not c.get("endpoints"):
            c["endpoints"] = ([{"value": e, "label": "email"} for e in emails]
                              + [{"value": p, "label": "mobile"} for p in phones])
        if c.get("relationship") and not c.get("notes"):
            c["notes"] = f"Relationship: {c['relationship']}"
        key = json.dumps([c.get("name", ""), emails, phones], sort_keys=True)
        c["contact_id"] = "w2_contact_" + hashlib.sha1(key.encode()).hexdigest()[:12]
        fixed += 1
    return fixed


class WorldCtx:
    """Duck-typed stand-in for the pilot seeder's ``Ctx`` (seeder/pilot10/gab_seed.py).

    The pilot seed/restore/snapshot functions read only ``account``, ``bc``, ``rw()``,
    ``tz``, ``tz_name`` and (through save_state/load_state) ``persona``. ``persona`` is the
    STATE KEY: it includes the account so two accounts seeded with the same persona never
    share a state file.
    """

    def __init__(self, folder: str, account: str, *, rewrite: bool = True):
        data = load_world(folder)
        self.folder = folder
        self.pdir = data["pdir"]
        self.world = data["world"]
        self.manifest = data["manifest"]
        self.bc = data["bc"]
        self.descriptive = data["descriptive"]
        prof = self.world.get("persona") or {}
        self.persona_id = prof.get("id") or self.manifest.get("persona_id") or folder
        self.persona_email = (prof.get("persona_email") or "").strip()
        self.persona_name = _profile_name(prof.get("profile_doc") or "") or folder.replace("_", " ").title()
        self.account = account.strip().lower()
        self.persona = state_key(folder, self.account)
        self.tz_name = self.descriptive.get("timezone") or "UTC"
        try:
            self.tz = ZoneInfo(self.tz_name)
        except Exception:  # noqa: BLE001 - unknown tz name -> UTC, like the pilot seeder
            self.tz_name, self.tz = "UTC", ZoneInfo("UTC")
        # Keep the account's real address; swap the synthetic persona address inside the
        # seeded content (mail headers/bodies, invites, contacts, Drive text) for it.
        self.rewrite = bool(rewrite and self.account and self.persona_email)

    def rw(self, s: Any) -> Any:
        """Replace the synthetic persona email with the real account email."""
        if not isinstance(s, str) or not self.rewrite:
            return s
        return re.sub(re.escape(self.persona_email), self.account, s, flags=re.I)

    def birthday(self) -> str:
        return profile_birthday((self.world.get("persona") or {}).get("profile_doc") or "")

    def home_address(self) -> str:
        doc = (self.world.get("persona") or {}).get("profile_doc") or ""
        for pat in (
            r"\*\*Home Address:\*\*\s*(.+)",
            r"\|\s*\*\*(?:Location\s*/\s*)?(?:Residence|Home) Address\*\*\s*\|\s*([^|\n]+?)\s*\|",
            r"(?:Residence|Home) Address\**\s*[:|]\s*\**\s*([^|\n]+)",
        ):
            m = re.search(pat, doc)
            if m and m.group(1).strip():
                # Street address only: drop "(neighbourhood ...)" notes and anything after them.
                addr = m.group(1).split("(")[0].strip().rstrip(".,; ")
                return with_unit(addr, json.dumps(self.world.get("bard_contexts") or {}))
        return ""


_UNIT_RE = r"(?:Apt|Apartment|Unit|Suite|Ste|#)\.?\s*[A-Za-z0-9-]+"


def with_unit(addr: str, text: str) -> str:
    """Add the apartment/unit the profile row leaves out but the persona's own data uses
    ('285 Lafayette St, Apt 4B, New York, NY 10012' in bills and leases). The unit is taken
    only when it directly follows this exact street; an address that already has one, or
    whose street never appears with a unit, is returned unchanged."""
    if not addr or re.search(r"\b" + _UNIT_RE + r"\b", addr):
        return addr
    street, sep, rest = addr.partition(",")
    m = re.search(r"(?<![\w-])" + re.escape(street.strip()) + r",?\s+(" + _UNIT_RE + r")\b", text or "")
    if not m:
        return addr
    unit = re.sub(r"\s+", " ", m.group(1)).strip()
    return f"{street.strip()}, {unit}{sep}{rest}" if sep else f"{street.strip()}, {unit}"


_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]


def profile_birthday(doc: str) -> str:
    """The persona's OWN birth date from the profile table row
    '| **Date of Birth & Age** | April 22, 1958 (Age 68) |' as ISO 'YYYY-MM-DD' ('' if absent).
    Only that row is read, so family members' ages/dates elsewhere are never picked up."""
    m = re.search(r"\*\*Date of Birth[^*|]*\*\*\s*\|\s*([A-Za-z]+)\.?\s+(\d{1,2}),?\s+(\d{4})", doc or "")
    if not m or m.group(1).lower()[:3] not in [x[:3] for x in _MONTHS]:
        return ""
    month = [x[:3] for x in _MONTHS].index(m.group(1).lower()[:3]) + 1
    return f"{int(m.group(3)):04d}-{month:02d}-{int(m.group(2)):02d}"


def _profile_name(doc: str) -> str:
    """'# Diana Kowalczyk — The Small Business ...' / '# Adrian Castellanos: The ...' ->
    the name. Splits on ':' or a SPACED dash, so 'Ferrante-Ross' stays whole."""
    m = re.search(r"^#\s*(.+)$", doc, re.M)
    if not m:
        return ""
    return re.split(r"\s*:\s*|\s+[—–-]\s+", m.group(1), maxsplit=1)[0].strip()


def state_key(folder: str, account: str) -> str:
    acct = re.sub(r"[^a-z0-9]+", "_", (account or "").lower()).strip("_")
    return f"{folder}__{acct}"

#!/usr/bin/env python3
"""
GAB v2 Pilot-10 seeder, Deccan extension of Google's gab_seed.py.

gab_seed.py (next to this file) is Google's reference seeder. It is kept byte-for-byte
unchanged. This file imports it and only adds or overrides behaviour:

  1. Drive originals: files that carry file_attachment.inline_bytes (PDFs, images, docx/xlsx/pptx)
     are uploaded as the REAL original file instead of being regenerated from content_summary
     (PDF) or skipped (images, Slides). Office bytes whose world mime is a Google type are
     converted on upload (docx->Doc, xlsx->Sheet, pptx->Slides). The persona email is rewritten
     inside Office files before upload. Anything that fails falls back to gab_seed's text path.
  2. Home address: also reads the "Location / Residence Address" table row in persona_doc.
  3. Full wipe (Google's Batch-2 requirement): on top of gab_seed's wipe it also removes Gmail
     drafts and custom labels, empties Drive Trash, unsubscribes non-owned calendars, deletes
     custom contact groups, clears completed Tasks. `wipe-check` lists anything left.
  4. `manual` writes <persona>_MANUAL.md: every step code cannot do (Keep, Gemini settings,
     Trash folders, 3P apps, Turn-1 attachments, name/address) plus Keep/Chat content.
  5. Every command is appended to _seed_state/runlog.csv (for the Eval Sheet "Email id" column).

Commands (same as gab_seed.py, plus wipe-check and manual):
  init | plan P | auth P | audit P | wipe P --confirm EMAIL | wipe-check P | seed P | verify P
  | reset P | manual P

  --root DIR   folder that holds the unzipped persona folders, accounts.csv, client_secret.json
               (default: $GAB_PILOT_ROOT, else this file's folder)
"""
import argparse, base64, csv, io, json, os, re, sys, time, zipfile
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import gab_seed as base  # noqa: E402  (Google's original, unchanged)

log, die, dig, ex, ex_ok, pages = base.log, base.die, base.dig, base.ex, base.ex_ok, base.pages

SLIDES = "application/vnd.google-apps.presentation"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OFFICE_FOR = {"word": base.DOCX, "xl": base.XLSX, "ppt": PPTX}
GOOGLE_FOR = {base.DOCX: base.DOC, base.XLSX: base.SHEET, PPTX: SLIDES}
RESUMABLE_OVER = 5 * 1024 * 1024


# ----------------------------------------------------------------- 1. Drive originals
def decode_inline(f):
    b64 = dig(f, "file_attachment", "inline_bytes")
    if not b64 or not isinstance(b64, str):
        return None
    pad = "=" * (-len(b64) % 4)
    for dec in (base64.b64decode, base64.urlsafe_b64decode):
        try:
            return dec(b64 + pad)
        except Exception:
            pass
    return None


def sniff(data):
    """Real mime of the bytes (the world mime often says Google Doc while the bytes are docx)."""
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:2] == b"PK":
        try:
            names = zipfile.ZipFile(io.BytesIO(data)).namelist()
        except zipfile.BadZipFile:
            return None
        for top, mime in OFFICE_FOR.items():
            if any(n.startswith(top + "/") for n in names):
                return mime
    return None


def upload_plan(f):
    """(bytes, upload_mime, target_mime) when the file should be uploaded from its original bytes."""
    data = decode_inline(f)
    if not data:
        return None
    world = f.get("mime_type", "")
    real = sniff(data) or (dig(f, "file_attachment", "mime_type") or "")
    if world in (base.DOC, base.SHEET, SLIDES):
        if real in GOOGLE_FOR and GOOGLE_FOR[real] == world:
            return data, real, world          # Office bytes -> convert to the Google type
        return None                           # bytes don't match; let gab_seed rebuild from text
    up = real or world or "application/octet-stream"
    return data, up, up                       # PDF / image / Office kept as-is


def rewrite_office(ctx, data):
    """Swap the synthetic persona email for the account email inside docx/xlsx/pptx XML."""
    if not (ctx.rewrite and ctx.persona_email and data[:2] == b"PK"):
        return data, 0
    old = ctx.persona_email.encode()
    new = ctx.account.encode()
    pat = re.compile(re.escape(old), re.I)
    src = zipfile.ZipFile(io.BytesIO(data))
    out, hits = io.BytesIO(), 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            blob = src.read(info.filename)
            if info.filename.endswith((".xml", ".rels")):
                blob, n = pat.subn(new, blob)
                hits += n
            dst.writestr(info, blob)
    return (out.getvalue(), hits) if hits else (data, 0)


_base_file_kind = base.file_kind
_base_create_drive_file = base.create_drive_file


def file_kind(f):
    return "original" if upload_plan(f) else _base_file_kind(f)


def create_drive_file(ctx, S, st, f):
    plan = upload_plan(f)
    if not plan:
        return _base_create_drive_file(ctx, S, st, f)
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaInMemoryUpload
    data, up_mime, target = plan
    data, hits = rewrite_office(ctx, data)
    meta = {"name": f.get("name") or "Untitled", "mimeType": target,
            "appProperties": {"gab_id": base.gid(f["file_id"]), "gab_src": "original"}}
    par = f.get("parent_folder_id") or ""
    if par in st["drive_folders"]:
        meta["parents"] = [st["drive_folders"][par]["id"]]
    media = MediaInMemoryUpload(data, mimetype=up_mime, resumable=len(data) > RESUMABLE_OVER)
    try:
        fid = ex(S.drive.files().create(body=meta, media_body=media, fields="id"))["id"]
    except HttpError as e:
        log("    ! original upload failed for '%s' (%s); falling back to text rebuild" % (meta["name"], base.http_status(e)))
        return _base_create_drive_file(ctx, S, st, f)
    if f.get("modified_time"):
        ex(S.drive.files().update(fileId=fid, body={"modifiedTime": f["modified_time"]}))
    got = ex(S.drive.files().get(fileId=fid, fields="id,version")) or {}
    rec = {"id": fid, "version": got.get("version"), "src": "original"}
    if hits:
        rec["email_rewrites"] = hits
    return rec


base.file_kind = file_kind
base.create_drive_file = create_drive_file  # used by seed_drive and restore_drive


# ----------------------------------------------------------------- 2. home address
_ADDR_PATTERNS = [
    r"\*\*Home Address:\*\*\s*(.+)",
    r"\|\s*\*\*(?:Location\s*/\s*)?(?:Residence|Home) Address\*\*\s*\|\s*([^|\n]+?)\s*\|",
    r"(?:Residence|Home) Address\**\s*[:|]\s*\**\s*([^|\n]+)",
]


def home_address(self):
    doc = self.manifest.get("persona_doc", "") or ""
    for p in _ADDR_PATTERNS:
        m = re.search(p, doc)
        if m and m.group(1).strip():
            # drop trailing "(Neighborhood) (US)" notes so it pastes cleanly into Google's address field
            return re.sub(r"(\s*\([^)]*\))+\s*$", "", m.group(1).strip())
    return "(not found in persona_doc; check manifest.json)"


base.Ctx.home_address = home_address


# ----------------------------------------------------------------- 3. full wipe + wipe-check
def _try(label, fn):
    from googleapiclient.errors import HttpError
    try:
        return fn()
    except HttpError as e:
        log("  (could not %s: HTTP %s)" % (label, base.http_status(e)))
        return None


def extra_state(S):
    """Things gab_seed's audit does not count but Google's full-wipe list covers."""
    out = {}
    out["gmail_drafts"] = len(pages(S.gmail.users().drafts().list, "drafts", userId="me", maxResults=500))
    labels = (ex(S.gmail.users().labels().list(userId="me")) or {}).get("labels", [])
    out["gmail_custom_labels"] = len([l for l in labels if l.get("type") == "user"])
    out["drive_trash"] = len(pages(S.drive.files().list, "files", q="trashed=true and 'me' in owners",
                                   pageSize=1000, fields="nextPageToken,files(id)"))
    out["drive_shared_with_me"] = len(pages(S.drive.files().list, "files", q="sharedWithMe=true and trashed=false",
                                            pageSize=1000, fields="nextPageToken,files(id)"))
    cals = base.list_calendars(S)
    out["calendar_subscriptions_not_owned"] = len([c for c in cals if c.get("accessRole") != "owner"
                                                   and not c.get("primary")])
    groups = pages(S.people.contactGroups().list, "contactGroups", pageSize=1000)
    out["contact_groups_custom"] = len([g for g in groups if g.get("groupType") == "USER_CONTACT_GROUP"])
    out["task_lists"] = len(pages(S.tasks.tasklists().list, "items", maxResults=100))
    return out


def cmd_wipe(ctx):
    base.cmd_wipe(ctx)  # dies before touching anything unless --confirm matches the account
    S = base.services(ctx)
    g = S.gmail.users()
    n_dr = 0
    for d in pages(g.drafts().list, "drafts", userId="me", maxResults=500):
        ex_ok(g.drafts().delete(userId="me", id=d["id"]))
        n_dr += 1
    n_lb = 0
    for l in (ex(g.labels().list(userId="me")) or {}).get("labels", []):
        if l.get("type") == "user":
            ex_ok(g.labels().delete(userId="me", id=l["id"]))
            n_lb += 1
    _try("empty Drive trash", lambda: ex(S.drive.files().emptyTrash()))
    n_sh = 0
    me = (ex(S.drive.about().get(fields="user(permissionId)")) or {}).get("user", {}).get("permissionId")
    for f in pages(S.drive.files().list, "files", q="sharedWithMe=true and trashed=false",
                   pageSize=1000, fields="nextPageToken,files(id)"):
        if me and _try("remove shared-in file", lambda: ex(S.drive.permissions().delete(fileId=f["id"], permissionId=me))) is not None:
            n_sh += 1
    n_cal = 0
    for c in base.list_calendars(S):
        if c.get("accessRole") != "owner" and not c.get("primary"):
            _try("unsubscribe calendar", lambda: ex_ok(S.cal.calendarList().delete(calendarId=c["id"])))
            n_cal += 1
    n_grp = 0
    for grp in pages(S.people.contactGroups().list, "contactGroups", pageSize=1000):
        if grp.get("groupType") == "USER_CONTACT_GROUP":
            ex_ok(S.people.contactGroups().delete(resourceName=grp["resourceName"]))
            n_grp += 1
    _try("clear completed tasks", lambda: ex(S.tasks.tasks().clear(tasklist="@default")))
    log("Extra wipe: %d drafts, %d custom labels, Drive trash emptied, %d shared-in files removed, "
        "%d calendar subscriptions removed, %d contact groups." % (n_dr, n_lb, n_sh, n_cal, n_grp))
    log("Now run: wipe-check %s   then do the manual wipe steps in: manual %s" % (ctx.persona, ctx.persona))


def cmd_wipe_check(ctx):
    S = base.services(ctx)
    c = base.counts(S)
    c.update(extra_state(S))
    left = {k: v for k, v in c.items() if v and not (k == "task_lists" and v <= 1)}
    log("Wipe check for %s:" % ctx.account)
    for k, v in c.items():
        log("  %-34s %s%s" % (k, v, "   <-- not empty" if k in left else ""))
    log("\nAPI-side wipe: %s" % ("CLEAN" if not left else "NOT clean (%s)" % ", ".join(left)))
    log("API cannot see: Keep (+trash), Calendar trash, Contacts trash, 'Other contacts', Gemini chats / "
        "Saved Info / Scheduled actions. Do those by hand (manual %s)." % ctx.persona)
    return not left


def cmd_audit(ctx):
    base.cmd_audit(ctx)
    S = base.services(ctx)
    for k, v in extra_state(S).items():
        log("  %-34s %s" % (k, v))


def cmd_plan(ctx):
    base.cmd_plan(ctx)
    orig = [f for f in base.w_files(ctx.bc) if upload_plan(f)]
    if orig:
        by = {}
        for f in orig:
            _, up, tgt = upload_plan(f)
            short = {base.DOCX: "docx", base.XLSX: "xlsx", PPTX: "pptx", base.DOC: "Google Doc",
                     base.SHEET: "Google Sheet", SLIDES: "Google Slides"}
            key = ("%s -> %s" % (short.get(up, up), short.get(tgt, tgt))) if up != tgt else short.get(up, up.split("/")[-1])
            by[key] = by.get(key, 0) + 1
        log("\nv2: %d Drive files uploaded from their ORIGINAL bytes (not counted in the Drive line above): %s"
            % (len(orig), ", ".join("%d %s" % (n, k) for k, n in sorted(by.items()))))
    left = [f for f in base.w_files(ctx.bc) if file_kind(f) == "skip"]
    log("v2: Drive files still not seedable: %s" % (", ".join("%s (%s)" % (f.get("name"), f.get("mime_type")) for f in left) or "none"))
    write_manual(ctx)


# ----------------------------------------------------------------- 4. manual checklist
def task_json(ctx, tid):
    p = os.path.join(ctx.pdir, "turn_1", "tasks", tid + ".json")
    return json.load(open(p)) if os.path.exists(p) else None


def write_manual(ctx):
    lists, notes = base.w_keep(ctx.bc)
    chat = base.w_chat(ctx.bc)
    acct = ctx.account or "(account from accounts.csv)"
    L = ["# Manual steps: %s -> %s" % (ctx.persona_name, acct), "",
         "Code seeds Gmail, Contacts, Calendar, Drive and Tasks. Everything below is by hand.", "",
         "## A. Before seeding (once per account)",
         "- [ ] Account is a test user on the OAuth consent screen (Google Cloud > APIs & Services > Audience).",
         "- [ ] `auth %s` done with all 6 scopes (token expires after 7 days in Testing mode)." % ctx.persona, "",
         "## B. Full wipe: parts the API cannot reach",
         "- [ ] Keep: delete every note and list, then empty Keep Trash (keep.google.com > Trash > Empty).",
         "- [ ] Calendar Trash: calendar.google.com > Settings gear > Trash > Empty.",
         "- [ ] Contacts Trash: contacts.google.com > Trash > Empty trash.",
         "- [ ] Other contacts: contacts.google.com/other > select all > delete.",
         "- [ ] Drive 'Shared with me' leftovers (if wipe-check shows any): right-click > Remove.",
         "- [ ] Gemini: delete all chats (gemini.google.com > Activity > Delete all time).",
         "- [ ] Gemini: Saved Info > delete all.",
         "- [ ] Gemini: Scheduled actions > delete all.",
         "- [ ] Run `wipe-check %s` and confirm API-side wipe is CLEAN." % ctx.persona, "",
         "## C. Account settings after seeding",
         "- [ ] Gemini Apps Activity = ON (myactivity.google.com/product/gemini).",
         "- [ ] Gemini Workspace extensions = ON (gemini.google.com > Settings > Apps: Gmail, Calendar, Drive, Keep, Tasks).",
         "- [ ] Name: myaccount.google.com > Personal info > Name = `%s`" % ctx.persona_name,
         "- [ ] Home address: myaccount.google.com > Personal info > Addresses > Home = `%s`" % ctx.home_address(),
         "- [ ] ChatGPT (GPT side): Settings > Apps & Connectors > connect this same Google account.",
         "- [ ] Log `%s` in the Eval Sheet 'Email id' column for every run (see _seed_state/runlog.csv)." % acct, "",
         "## D. Per active task"]
    for tid in ctx.active:
        t = task_json(ctx, tid)
        if t is None:
            L += ["### %s" % tid, "- [ ] **Task file missing from this persona's zip.** Ask Google for it before running.", ""]
            continue
        L.append("### %s" % tid)
        att = t.get("turn1_attach_files") or []
        if att:
            L.append("- [ ] Turn-1 attachments (rater uploads in chat, NOT seeded): %s, from the 'Turn-1 Attachments' folder."
                     % ", ".join(att))
        L.append("- 1P fallback allowed: %s" % t.get("allow_1p_fallback"))
        scope = (t.get("three_p_scope") or "").strip()
        if scope:
            L.append("- 3P (not seedable by us; needs the 3P app/connector or the fallback):")
            L += ["  > " + ln for ln in scope.splitlines() if ln.strip()]
        L.append("")
    planted = ctx.world.get("planted_records") or {}
    needs = lambda s: [t for t in ctx.active if s in (planted.get(t) or {})]
    if lists or notes:
        k = needs("KEEP")
        L += ["## E. Keep content", "",
              "%d lists, %d notes. Full text: _seed_state/%s_KEEP_manual.md" % (len(lists), len(notes), ctx.persona),
              ("**Active task(s) %s plant Keep records: enter those by hand.**" % ", ".join(k)) if k else
              "No active task plants Keep records; entering them is optional (realism only).", ""]
    if chat:
        c = needs("GOOGLE_CHAT")
        L += ["## F. Google Chat", "", "%d spaces in the snapshot. Chat cannot be seeded on consumer accounts "
              "(no API, and messages need a second real sender). %s" % (len(chat),
              ("Active task(s) %s plant Chat records: ask Google." % ", ".join(c)) if c else
              "No active task plants Chat records."), ""]
    p = os.path.join(base.state_dir(), ctx.persona + "_MANUAL.md")
    with open(p, "w") as fh:
        fh.write("\n".join(L) + "\n")
    base.write_keep_file(ctx)
    log("  Manual checklist written to %s" % p)
    return p


def cmd_manual(ctx):
    p = write_manual(ctx)
    log(open(p).read())


# ----------------------------------------------------------------- 5. run log
def runlog(cmd, ctx, outcome):
    p = os.path.join(base.state_dir(), "runlog.csv")
    new = not os.path.exists(p)
    with open(p, "a", newline="") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["utc_time", "command", "persona", "account_email", "active_tasks", "outcome"])
        w.writerow([datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"), cmd,
                    ctx.persona if ctx else "", ctx.account if ctx else "",
                    " ".join(ctx.active) if ctx else "", outcome])


# ----------------------------------------------------------------- main
COMMANDS = {"plan": cmd_plan, "auth": base.cmd_auth, "audit": cmd_audit, "wipe": cmd_wipe,
            "wipe-check": cmd_wipe_check, "seed": base.cmd_seed, "verify": base.cmd_verify,
            "reset": base.cmd_reset, "manual": cmd_manual}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init"] + list(COMMANDS))
    ap.add_argument("persona", nargs="?")
    ap.add_argument("--root", default=os.environ.get("GAB_PILOT_ROOT") or HERE,
                    help="folder holding persona folders, accounts.csv, client_secret.json")
    ap.add_argument("--tasks", help="override active task IDs, comma-separated")
    ap.add_argument("--no-rewrite", action="store_true")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--confirm", help="wipe: the account email, to confirm")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--keep-alerts", action="store_true")
    args = ap.parse_args(argv)
    base.ROOT = os.path.abspath(os.path.expanduser(args.root))
    base.ARGS = args
    if args.command == "init":
        return base.cmd_init()
    if not args.persona:
        die("Give a persona folder name, e.g.: gab_seed_v2.py %s elena_alvarez_gomez" % args.command)
    ctx = base.Ctx(args.persona, need_account=args.command not in ("plan", "manual"))
    outcome = "ok"
    try:
        r = COMMANDS[args.command](ctx)
        if args.command == "wipe-check" and r is False:
            outcome = "not clean"
    except SystemExit as e:
        outcome = "stopped (exit %s)" % e.code
        raise
    except Exception as e:
        outcome = "error: %s" % str(e)[:200]
        raise
    finally:
        if args.command != "plan":
            runlog(args.command, ctx, outcome)


if __name__ == "__main__":
    main()

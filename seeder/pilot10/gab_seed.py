#!/usr/bin/env python3
"""
GAB v2 demo-account seeder (Deccan).

Loads a persona Turn-Split snapshot (<persona>/turn_1/turn_1_world.json -> bard_contexts)
into a Google demo account, records a baseline, and restores that baseline between agent runs.

Commands (run from the folder that holds the unzipped persona folders):
  init                 create accounts.csv (persona -> demo account email)
  plan    <persona>    offline: what will be seeded, what can't be, active-task coverage
  auth    <persona>    sign in to the demo account in a browser, save a token
  audit   <persona>    count what is already in the demo account
  wipe    <persona>    delete EVERYTHING in the account (dedicated accounts only)
  seed    <persona>    load the snapshot, then record the baseline
  verify  <persona>    confirm every planted record for the active tasks is live
  reset   <persona>    restore the account to the post-seed baseline (between agents / tasks)

Seeded via API: Gmail, Contacts, Calendar, Drive (Docs/Sheets/PDF/Office), Tasks.
Not seedable via API on consumer accounts: Keep, Google Chat (plan writes a Keep file for manual entry).
"""
import argparse, base64, csv, hashlib, html, json, os, random, re, sys, time, types
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/contacts",
    "https://www.googleapis.com/auth/tasks",
]

# Active 10-task set (guide section 3.1). Keyed by manifest persona_id.
ACTIVE_TASKS = {
    "elena_alvarez_gomez": ["GAB-Local-068", "GAB-Health-061"],
    "elena_lindqvist": ["GAB-EDU-015"],
    "grace_and_peter_mensah": ["GAB-Shopping-040"],
    "hannah_and_david_lindqvist": ["GAB-Ent-014"],
    "hannah_o_connor": ["GAB-Coding-014"],
    "julian_brooks": ["GAB-Shopping-043", "GAB-Local-057", "GAB-Shopping-025"],
    "nadia_al_mansoor": ["GAB-Coding-018"],
}

SEEDED_SURFACES = {"GMAIL", "CALENDAR", "DRIVE", "CONTACTS", "TASKS"}
MANUAL_SURFACES = {"KEEP", "GOOGLE_CHAT"}

FOLDER = "application/vnd.google-apps.folder"
DOC = "application/vnd.google-apps.document"
SHEET = "application/vnd.google-apps.spreadsheet"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
EXPORTED = {"application/pdf": DOC, DOCX: DOC, "application/msword": DOC, XLSX: SHEET}
GMAIL_SYSTEM = {"INBOX", "SENT", "STARRED", "IMPORTANT", "UNREAD", "SPAM", "TRASH",
                "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
                "CATEGORY_UPDATES", "CATEGORY_FORUMS"}
GMAIL_UNMODIFIABLE = {"SENT", "DRAFT", "CHAT"}
HTML_RE = re.compile(r"<\s*(html|body|div|p|br|table|span|a)\b", re.I)

ROOT = os.path.dirname(os.path.abspath(__file__))
ARGS = None


# ----------------------------------------------------------------- utilities
def log(msg=""):
    print(msg, flush=True)


def die(msg):
    print("ERROR: " + msg, file=sys.stderr)
    sys.exit(1)


def gid(src_id):
    return hashlib.sha1(str(src_id).encode()).hexdigest()[:16]


def dig(d, *keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def as_json(v):
    if isinstance(v, str):
        try:
            return json.loads(v)
        except ValueError:
            return {}
    return v or {}


def http_status(e):
    try:
        return int(e.resp.status)
    except Exception:
        return 0


def ex(req, tries=7):
    """Execute a Google API request with backoff on rate limits / 5xx."""
    from googleapiclient.errors import HttpError
    for i in range(tries):
        try:
            return req.execute()
        except HttpError as e:
            code = http_status(e)
            body = (e.content or b"").decode("utf-8", "ignore")
            if code in (429, 500, 502, 503, 504) or (code == 403 and "ateLimit" in body):
                time.sleep(min(60, 2 ** i + random.random()))
                continue
            raise
    return req.execute()


def ex_ok(req, ok=(404, 410)):
    """Execute, ignoring 'already gone' errors."""
    from googleapiclient.errors import HttpError
    try:
        return ex(req)
    except HttpError as e:
        if http_status(e) in ok:
            return None
        raise


def pages(method, key, **kw):
    out, tok = [], None
    while True:
        r = ex(method(**kw, **({"pageToken": tok} if tok else {}))) or {}
        out += r.get(key, []) or []
        tok = r.get("nextPageToken")
        if not tok:
            return out


# ----------------------------------------------------------------- world access
def w_emails(bc):
    return dig(bc, "GMAIL", "mock_context", "gmail_mock_context", "emails") or []


def w_calendars(bc):
    return dig(bc, "CALENDAR", "calendars_info", "calendars") or []


def w_events(bc):
    return dig(bc, "CALENDAR", "calendar_events_info", "events") or []


def w_folders(bc):
    return dig(bc, "DRIVE", "mock_context", "drive_mock_context", "folders") or []


def w_files(bc):
    return dig(bc, "DRIVE", "mock_context", "drive_mock_context", "files") or []


def w_contacts(bc):
    return dig(bc, "CONTACTS", "contact_lookup_result", "contacts") or []


def w_tasks(bc):
    return dig(bc, "TASKS", "tasks") or []


def w_keep(bc):
    k = dig(bc, "KEEP", "mock_context", "keep_mock_context") or {}
    return k.get("lists", []) or [], k.get("notes", []) or []


def w_chat(bc):
    return dig(bc, "GOOGLE_CHAT", "mock_context", "chat_mock_context", "items") or []


def event_key(ev):
    return ev.get("id") or gid(json.dumps(ev, sort_keys=True))


def extract_ids(surface, data):
    """(source_id, label) pairs for one surface of a planted_records / bard_contexts block."""
    bc = {surface: data}
    if surface == "GMAIL":
        return [(e["email_id"], e.get("subject", "")) for e in w_emails(bc)]
    if surface == "DRIVE":
        return ([(f["folder_id"], f.get("name", "")) for f in w_folders(bc)] +
                [(f["file_id"], f.get("name", "")) for f in w_files(bc)])
    if surface == "CONTACTS":
        return [(c["contact_id"], c.get("name", "")) for c in w_contacts(bc)]
    if surface == "CALENDAR":
        return [(event_key(e), e.get("summary", "")) for e in w_events(bc)]
    if surface == "TASKS":
        return [(t.get("task_id"), dig(t, "task_properties", "title") or "") for t in w_tasks(bc)]
    if surface == "KEEP":
        lists, notes = w_keep(bc)
        return [(x.get("title", ""), x.get("title", "")) for x in lists + notes]
    return [(surface, "%d item(s)" % len(json.dumps(data)))]


# ----------------------------------------------------------------- context / config
def accounts_path():
    return os.path.join(ROOT, "accounts.csv")


def read_accounts():
    p = accounts_path()
    if not os.path.exists(p):
        return {}
    with open(p, newline="") as fh:
        return {r["persona"].strip(): (r.get("account_email") or "").strip()
                for r in csv.DictReader(fh) if r.get("persona")}


class Ctx:
    def __init__(self, persona, need_account=True):
        self.persona = persona.strip("/").split("/")[-1]
        self.pdir = os.path.join(ROOT, self.persona)
        wpath = os.path.join(self.pdir, "turn_1", "turn_1_world.json")
        if not os.path.exists(wpath):
            die("Can't find %s. Run from the folder holding the unzipped persona folders." % wpath)
        self.manifest = json.load(open(os.path.join(self.pdir, "manifest.json")))
        self.world = json.load(open(wpath))
        self.bc = {k: as_json(v) for k, v in (self.world.get("bard_contexts") or {}).items()}
        facts = self.manifest.get("facts", {})
        self.persona_id = self.manifest.get("persona_id") or self.persona
        self.persona_name = self.manifest.get("persona_name") or self.persona
        self.persona_email = facts.get("primary_email", "")
        self.tz_name = facts.get("timezone") or "UTC"
        try:
            self.tz = ZoneInfo(self.tz_name)
        except Exception:
            self.tz_name, self.tz = "UTC", timezone.utc
        self.account = read_accounts().get(self.persona, "")
        if need_account and not self.account:
            die("No account_email for '%s' in accounts.csv" % self.persona)
        self.rewrite = bool(self.account) and not ARGS.no_rewrite
        self.active = ARGS.tasks.split(",") if ARGS.tasks else \
            ACTIVE_TASKS.get(self.persona_id, ACTIVE_TASKS.get(self.persona, []))

    def rw(self, s):
        """Replace the synthetic persona email with the real demo account email."""
        if not isinstance(s, str) or not self.rewrite or not self.persona_email:
            return s
        return re.sub(re.escape(self.persona_email), self.account, s, flags=re.I)

    def home_address(self):
        m = re.search(r"\*\*Home Address:\*\*\s*(.+)", self.manifest.get("persona_doc", ""))
        return m.group(1).strip() if m else "(see persona_doc in manifest.json)"


def state_dir():
    d = os.path.join(ROOT, "_seed_state")
    os.makedirs(d, exist_ok=True)
    return d


def token_path(persona):
    d = os.path.join(ROOT, "_seed_tokens")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, persona + ".json")


def load_state(ctx):
    p = os.path.join(state_dir(), ctx.persona + ".json")
    st = json.load(open(p)) if os.path.exists(p) else {}
    for k in ("gmail", "gmail_labels", "calendars", "events", "drive_folders", "drive_files",
              "contacts", "tasks"):
        st.setdefault(k, {})
    st.setdefault("skipped", {})
    st["persona"], st["account"] = ctx.persona, ctx.account
    return st


def save_state(ctx, st):
    p = os.path.join(state_dir(), ctx.persona + ".json")
    tmp = p + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(st, fh, indent=1)
    os.replace(tmp, p)


# ----------------------------------------------------------------- auth / services
def build_services(creds):
    from googleapiclient.discovery import build
    b = lambda n, v: build(n, v, credentials=creds, cache_discovery=False)
    return types.SimpleNamespace(gmail=b("gmail", "v1"), cal=b("calendar", "v3"),
                                 drive=b("drive", "v3"), sheets=b("sheets", "v4"),
                                 people=b("people", "v1"), tasks=b("tasks", "v1"))


def services(ctx):
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    tok = token_path(ctx.persona)
    if not os.path.exists(tok):
        die("No token for %s. Run: python3 gab_seed.py auth %s" % (ctx.persona, ctx.persona))
    creds = Credentials.from_authorized_user_file(tok, SCOPES)
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            open(tok, "w").write(creds.to_json())
        else:
            die("Token invalid. Re-run auth.")
    S = build_services(creds)
    email = ex(S.gmail.users().getProfile(userId="me"))["emailAddress"]
    if email.lower() != ctx.account.lower():
        die("Token belongs to %s but accounts.csv says %s. Re-run auth." % (email, ctx.account))
    return S


def cmd_auth(ctx):
    from google_auth_oauthlib.flow import InstalledAppFlow
    cs = os.path.join(ROOT, "client_secret.json")
    if not os.path.exists(cs):
        die("client_secret.json not found next to gab_seed.py (README step 3: Google OAuth client).")
    flow = InstalledAppFlow.from_client_secrets_file(cs, SCOPES)
    log("Sign in as %s (use the Chrome profile for this demo account)." % ctx.account)
    creds = flow.run_local_server(
        port=0, open_browser=not ARGS.no_browser, login_hint=ctx.account, prompt="consent",
        authorization_prompt_message="If no browser opened, paste this URL into the demo "
                                     "account's Chrome profile:\n{url}\n")
    S = build_services(creds)
    email = ex(S.gmail.users().getProfile(userId="me"))["emailAddress"]
    if email.lower() != ctx.account.lower():
        die("You signed in as %s, expected %s. Nothing saved; run auth again." % (email, ctx.account))
    open(token_path(ctx.persona), "w").write(creds.to_json())
    log("OK: token saved for %s" % email)


# ----------------------------------------------------------------- content conversion
def to_html(text):
    looks_md = re.search(r"^\s{0,3}(#{1,6} |\|.*\||[-*] |\d+\. )", text, re.M)
    if not looks_md and re.search(r"\S {4,}\S", text):
        body = "<pre style='font-family:Courier New,monospace;font-size:9pt'>%s</pre>" % html.escape(text)
    else:
        try:
            import markdown
            body = markdown.markdown(text, extensions=["tables", "fenced_code", "sane_lists"])
        except ImportError:
            body = "<pre>%s</pre>" % html.escape(text)
    return "<html><head><meta charset='utf-8'></head><body>%s</body></html>" % body


def parse_sheet(text):
    tabs, cur, seen = [], None, set()

    def new_tab(title):
        base = (title or "Sheet1").strip()[:90] or "Sheet1"
        t, n = base, 2
        while t.lower() in seen:
            t, n = "%s (%d)" % (base, n), n + 1
        seen.add(t.lower())
        tab = {"title": t, "rows": []}
        tabs.append(tab)
        return tab

    for line in text.splitlines():
        m = re.match(r"^\s*#+\s*Tab:\s*(.+?)\s*$", line, re.I)
        if m:
            cur = new_tab(m.group(1))
            continue
        if not line.strip() or re.match(r"^\s*\|?\s*:?-{3,}", line):
            continue
        if cur is None:
            cur = new_tab("Sheet1")
        s = line.strip()
        if re.match(r"^#+\s", s):
            cells = [s.lstrip("#").strip()]
        elif "|" in s:
            cells = [c.strip() for c in s.strip("|").split("|")]
        else:
            cells = [s]
        cur["rows"].append(cells)
    return tabs


def cell(v):
    if re.fullmatch(r"-?\d+(\.\d+)?", v) and not (len(v) > 1 and v.startswith("0") and not v.startswith("0.")):
        return float(v) if "." in v else int(v)
    return v


# ----------------------------------------------------------------- GMAIL
def safe_id(s):
    return re.sub(r"[^A-Za-z0-9._-]", "-", s)


def build_raw(ctx, e, refs):
    from email.message import EmailMessage
    from email.utils import format_datetime
    clean = lambda s: (s or "").replace("\r", " ").replace("\n", " ").strip()
    m = EmailMessage()
    m["From"] = clean(ctx.rw(e.get("sender"))) or ctx.account
    rc = e.get("recipients") or []
    rc = [rc] if isinstance(rc, str) else rc
    if rc:
        m["To"] = ", ".join(clean(ctx.rw(r)) for r in rc)
    for k in ("cc", "bcc"):
        v = e.get(k)
        if v:
            m[k.capitalize()] = ", ".join(clean(ctx.rw(x)) for x in (v if isinstance(v, list) else [v]))
    m["Subject"] = clean(e.get("subject"))
    ts = e.get("timestamp_seconds") or e.get("timestamp")
    dt = datetime.fromtimestamp(int(float(ts)), ctx.tz) if ts else datetime.now(ctx.tz)
    m["Date"] = format_datetime(dt)
    mid = "<%s@mail.example.com>" % safe_id(e["email_id"])
    m["Message-ID"] = mid
    if refs:
        m["In-Reply-To"] = refs[-1]
        m["References"] = " ".join(refs)
    body = ctx.rw(e.get("body") or "")
    if HTML_RE.search(body):
        m.set_content(re.sub(r"<[^>]+>", "", body))
        m.add_alternative(body, subtype="html")
    else:
        m.set_content(body)
    return base64.urlsafe_b64encode(m.as_bytes()).decode(), mid


def gmail_label_ids(S, st, names):
    out = []
    for n in names:
        if n in GMAIL_SYSTEM:
            out.append(n)
            continue
        if n == "DRAFT":
            continue
        lid = st["gmail_labels"].get(n)
        if not lid:
            have = {l["name"]: l["id"] for l in (ex(S.gmail.users().labels().list(userId="me")) or {}).get("labels", [])}
            lid = have.get(n) or ex(S.gmail.users().labels().create(userId="me", body={"name": n}))["id"]
            st["gmail_labels"][n] = lid
        out.append(lid)
    return out


def insert_email(ctx, S, st, e, thread):
    raw, mid = build_raw(ctx, e, thread["refs"])
    labels = e.get("labels") or []
    if "DRAFT" in labels:
        msg = {"raw": raw, **({"threadId": thread["tid"]} if thread["tid"] else {})}
        r = ex(S.gmail.users().drafts().create(userId="me", body={"message": msg}))["message"]
    else:
        body = {"raw": raw, "labelIds": gmail_label_ids(S, st, labels)}
        if thread["tid"]:
            body["threadId"] = thread["tid"]
        r = ex(S.gmail.users().messages().insert(userId="me", body=body, internalDateSource="dateHeader"))
    got = ex(S.gmail.users().messages().get(userId="me", id=r["id"], format="minimal")) or {}
    tid = got.get("threadId") or r.get("threadId")
    thread["tid"] = thread["tid"] or tid
    thread["refs"].append(mid)
    return {"id": r["id"], "threadId": tid, "mid": mid, "labelIds": sorted(got.get("labelIds", []))}


def sorted_emails(ctx):
    return sorted(w_emails(ctx.bc), key=lambda e: float(e.get("timestamp_seconds") or 0))


def seed_gmail(ctx, S, st):
    threads, n = {}, 0
    for e in sorted_emails(ctx):
        th = threads.setdefault(e.get("thread_id") or e["email_id"], {"tid": None, "refs": []})
        rec = st["gmail"].get(e["email_id"])
        if rec:
            th["tid"] = th["tid"] or rec["threadId"]
            th["refs"].append(rec["mid"])
            continue
        st["gmail"][e["email_id"]] = insert_email(ctx, S, st, e, th)
        save_state(ctx, st)
        n += 1
    log("  Gmail: %d inserted (%d total in snapshot)" % (n, len(w_emails(ctx.bc))))


# ----------------------------------------------------------------- CONTACTS
def contact_body(ctx, c, structured=False):
    name = c.get("name", "")
    if structured:
        names = [{"givenName": name}]
    else:
        names = [{"unstructuredName": name}]
    b = {"names": names, "clientData": [{"key": "gab_id", "value": gid(c["contact_id"])}]}
    emails, phones, addrs = [], [], []
    for ep in c.get("endpoints") or []:
        v = ctx.rw(str(ep.get("value", "")).strip())
        lab = (ep.get("label") or "").strip()
        if not v:
            continue
        if "@" in v and " " not in v:
            emails.append({"value": v, "type": lab if lab and lab.lower() != "email" else "other"})
        elif "address" in lab.lower() or not re.fullmatch(r"[\d\s()+.\-x]+", v):
            addrs.append({"formattedValue": v, "streetAddress": v, "type": "home"})
        else:
            phones.append({"value": v, "type": lab if lab and lab.lower() != "phone" else "mobile"})
    if emails:
        b["emailAddresses"] = emails
    if phones:
        b["phoneNumbers"] = phones
    if addrs:
        b["addresses"] = addrs
    if c.get("notes"):
        b["biographies"] = [{"value": ctx.rw(c["notes"]), "contentType": "TEXT_PLAIN"}]
    return b


def insert_contact(ctx, S, c):
    from googleapiclient.errors import HttpError
    try:
        r = ex(S.people.people().createContact(body=contact_body(ctx, c), personFields="names,metadata"))
    except HttpError as e:
        if http_status(e) != 400:
            raise
        r = ex(S.people.people().createContact(body=contact_body(ctx, c, True), personFields="names,metadata"))
    time.sleep(0.3)  # People API wants sequential, spaced writes per user
    return {"resourceName": r["resourceName"], "etag": r.get("etag")}


def seed_contacts(ctx, S, st):
    n = 0
    for c in w_contacts(ctx.bc):
        if c["contact_id"] in st["contacts"]:
            continue
        st["contacts"][c["contact_id"]] = insert_contact(ctx, S, c)
        save_state(ctx, st)
        n += 1
    log("  Contacts: %d created" % n)


# ----------------------------------------------------------------- CALENDAR
def ev_time(x, tzname):
    if not isinstance(x, dict):
        return None
    tz = x.get("time_zone") or tzname
    try:
        zi = ZoneInfo(tz)
    except Exception:
        tz, zi = tzname, ZoneInfo(tzname)
    v = x.get("date_time")
    if isinstance(v, dict) and "time_ms" in v:
        return {"dateTime": datetime.fromtimestamp(int(v["time_ms"]) / 1000, zi).isoformat(), "timeZone": tz}
    if isinstance(v, str):
        return {"dateTime": v, "timeZone": tz}
    d = x.get("date")
    if isinstance(d, dict):
        return {"date": "%04d-%02d-%02d" % (d["year"], d["month"], d["day"])}
    if isinstance(d, str):
        return {"date": d}
    return None


def pretty_cal(cid):
    return cid.split(":")[-1].replace("cal_", "").replace("_", " ").title()


def event_body(ctx, ev):
    start = ev_time(ev.get("start"), ctx.tz_name)
    if not start:
        return None
    b = {"summary": ev.get("summary", ""), "description": ctx.rw(ev.get("description", "")),
         "location": ev.get("location", ""), "start": start,
         "end": ev_time(ev.get("end"), ctx.tz_name) or start,
         "extendedProperties": {"private": {"gab_id": gid(event_key(ev))}}}
    rec = [r for r in (ev.get("recurrence_data") or ev.get("recurrence") or []) if isinstance(r, str) and r.strip()]
    if rec:
        b["recurrence"] = rec
    att = []
    for a in ev.get("attendee") or ev.get("attendees") or []:
        em = ctx.rw(a.get("email", ""))
        if not em or a.get("is_self") or em.lower() == ctx.account.lower():
            continue
        att.append({"email": em, **({"displayName": a["display_name"]} if a.get("display_name") else {})})
    if att:
        b["attendees"] = att
    return b


def calendar_map(ctx):
    cals = {c["id"]: c for c in w_calendars(ctx.bc)}
    for ev in w_events(ctx.bc):
        cid = ev.get("calendar_id") or "cal_primary"
        cals.setdefault(cid, {"id": cid, "summary": pretty_cal(cid), "is_primary": cid in ("cal_primary", "primary")})
    return cals


def ensure_calendar(ctx, S, st, cid, c):
    if c.get("is_primary") or cid in ("cal_primary", "primary"):
        st["calendars"][cid] = "primary"
    else:
        r = ex(S.cal.calendars().insert(body={"summary": c.get("summary") or pretty_cal(cid), "timeZone": ctx.tz_name}))
        st["calendars"][cid] = r["id"]


def insert_event(ctx, S, st, ev):
    b = event_body(ctx, ev)
    if not b:
        return None
    cal = st["calendars"][ev.get("calendar_id") or "cal_primary"]
    r = ex(S.cal.events().insert(calendarId=cal, body=b, sendUpdates="none"))
    return {"cal": cal, "id": r["id"], "etag": r.get("etag")}


def seed_calendar(ctx, S, st):
    ex(S.cal.calendars().patch(calendarId="primary", body={"timeZone": ctx.tz_name}))
    for cid, c in calendar_map(ctx).items():
        if cid not in st["calendars"]:
            ensure_calendar(ctx, S, st, cid, c)
            save_state(ctx, st)
    n = 0
    for ev in w_events(ctx.bc):
        k = event_key(ev)
        if k in st["events"] or k in st["skipped"]:
            continue
        rec = insert_event(ctx, S, st, ev)
        if rec:
            st["events"][k] = rec
            n += 1
        else:
            st["skipped"][k] = "calendar event without a start time"
        save_state(ctx, st)
    log("  Calendar: %d events, %d calendars (timezone %s)" % (n, len(st["calendars"]), ctx.tz_name))


# ----------------------------------------------------------------- DRIVE
def file_kind(f):
    mime = f.get("mime_type", "")
    if mime in (DOC, SHEET):
        return "doc" if mime == DOC else "sheet"
    if mime in EXPORTED:
        return "export"
    if mime.startswith("text/") or mime == "application/json":
        return "text"
    return "skip"


def make_google_file(S, gtype, text, meta):
    from googleapiclient.http import MediaInMemoryUpload
    meta = dict(meta, mimeType=gtype)
    if gtype == DOC:
        media = MediaInMemoryUpload(to_html(text).encode("utf-8"), mimetype="text/html", resumable=False)
        return ex(S.drive.files().create(body=meta, media_body=media, fields="id"))["id"]
    fid = ex(S.drive.files().create(body=meta, fields="id"))["id"]
    fill_sheet(S, fid, text)
    return fid


def fill_sheet(S, ssid, text):
    tabs = parse_sheet(text) or [{"title": "Sheet1", "rows": []}]
    meta = ex(S.sheets.spreadsheets().get(spreadsheetId=ssid, fields="sheets.properties")) or {}
    first = ((meta.get("sheets") or [{}])[0].get("properties") or {}).get("sheetId", 0)

    def grid(t):
        cols = max([len(r) for r in t["rows"]] + [26])
        return {"rowCount": max(1000, len(t["rows"]) + 50), "columnCount": cols, "frozenRowCount": 1 if len(t["rows"]) > 1 else 0}

    reqs = [{"updateSheetProperties": {"properties": {"sheetId": first, "title": tabs[0]["title"], "gridProperties": grid(tabs[0])},
                                       "fields": "title,gridProperties"}}]
    for t in tabs[1:]:
        reqs.append({"addSheet": {"properties": {"title": t["title"], "gridProperties": grid(t)}}})
    ex(S.sheets.spreadsheets().batchUpdate(spreadsheetId=ssid, body={"requests": reqs}))
    data = [{"range": "'%s'!A1" % t["title"].replace("'", "''"),
             "values": [[cell(c) for c in row] for row in t["rows"]]} for t in tabs if t["rows"]]
    if data:
        ex(S.sheets.spreadsheets().values().batchUpdate(spreadsheetId=ssid, body={"valueInputOption": "RAW", "data": data}))


def create_drive_file(ctx, S, st, f):
    from googleapiclient.http import MediaInMemoryUpload
    kind, mime = file_kind(f), f.get("mime_type", "")
    if kind == "skip":
        return None
    text = ctx.rw(f.get("content_summary") or f.get("content") or "")
    meta = {"name": f.get("name") or "Untitled", "appProperties": {"gab_id": gid(f["file_id"])}}
    par = f.get("parent_folder_id") or ""
    if par in st["drive_folders"]:
        meta["parents"] = [st["drive_folders"][par]["id"]]
    if kind in ("doc", "sheet"):
        fid = make_google_file(S, mime, text, meta)
    elif kind == "export":
        tmp = make_google_file(S, EXPORTED[mime], text, {"name": "_gab_tmp_" + meta["name"]})
        data = ex(S.drive.files().export(fileId=tmp, mimeType=mime))
        ex_ok(S.drive.files().delete(fileId=tmp))
        media = MediaInMemoryUpload(data, mimetype=mime, resumable=False)
        fid = ex(S.drive.files().create(body=dict(meta, mimeType=mime), media_body=media, fields="id"))["id"]
    else:
        media = MediaInMemoryUpload(text.encode("utf-8"), mimetype=mime, resumable=False)
        fid = ex(S.drive.files().create(body=dict(meta, mimeType=mime), media_body=media, fields="id"))["id"]
    if f.get("modified_time"):
        ex(S.drive.files().update(fileId=fid, body={"modifiedTime": f["modified_time"]}))
    got = ex(S.drive.files().get(fileId=fid, fields="id,version")) or {}
    return {"id": fid, "version": got.get("version")}


def create_folder(S, st, f):
    meta = {"name": f.get("name") or "Untitled folder", "mimeType": FOLDER,
            "appProperties": {"gab_id": gid(f["folder_id"])}}
    par = f.get("parent_folder_id") or ""
    if par in st["drive_folders"]:
        meta["parents"] = [st["drive_folders"][par]["id"]]
    r = ex(S.drive.files().create(body=meta, fields="id,version"))
    return {"id": r["id"], "version": r.get("version")}


def seed_folders(ctx, S, st, only=None):
    folders = [f for f in w_folders(ctx.bc) if only is None or f["folder_id"] in only]
    known = {f["folder_id"] for f in w_folders(ctx.bc)}
    pending = [f for f in folders if f["folder_id"] not in st["drive_folders"]]
    while pending:
        ready = [f for f in pending if not f.get("parent_folder_id") or f["parent_folder_id"] in st["drive_folders"]
                 or f["parent_folder_id"] not in known]
        ready = ready or pending[:1]  # break cycles
        for f in ready:
            st["drive_folders"][f["folder_id"]] = create_folder(S, st, f)
            pending.remove(f)
            save_state(ctx, st)


def seed_drive(ctx, S, st):
    seed_folders(ctx, S, st)
    n = 0
    for f in w_files(ctx.bc):
        fid = f["file_id"]
        if fid in st["drive_files"] or fid in st["skipped"]:
            continue
        rec = create_drive_file(ctx, S, st, f)
        if rec:
            st["drive_files"][fid] = rec
            n += 1
        else:
            st["skipped"][fid] = "Drive file '%s' (%s) has no recreatable content" % (f.get("name"), f.get("mime_type"))
        save_state(ctx, st)
    log("  Drive: %d folders, %d files" % (len(st["drive_folders"]), n))


# ----------------------------------------------------------------- TASKS
def task_body(ctx, t):
    p = t.get("task_properties") or t
    b = {"title": (p.get("title") or "").strip() or "(untitled)", "notes": ctx.rw(p.get("description") or "")}
    d = dig(p, "schedule_time", "date")
    if isinstance(d, dict):
        b["due"] = "%04d-%02d-%02dT00:00:00.000Z" % (d["year"], d["month"], d["day"])
    if p.get("completed"):
        b["status"] = "completed"
        b["completed"] = b.get("due") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    else:
        b["status"] = "needsAction"
    return b


def insert_task(ctx, S, t):
    r = ex(S.tasks.tasks().insert(tasklist="@default", body=task_body(ctx, t)))
    return {"list": "@default", "id": r["id"], "etag": r.get("etag")}


def seed_tasks(ctx, S, st):
    n = 0
    for t in w_tasks(ctx.bc):
        k = t.get("task_id") or gid(json.dumps(t, sort_keys=True))
        if k in st["tasks"]:
            continue
        st["tasks"][k] = insert_task(ctx, S, t)
        save_state(ctx, st)
        n += 1
    rec = sum(1 for t in w_tasks(ctx.bc) if t.get("task_recurrence_schedule"))
    log("  Tasks: %d created (%d recurrence rules + times of day dropped: Tasks API stores due dates only)" % (n, rec))


# ----------------------------------------------------------------- listing / baseline
def list_gmail_ids(S):
    return {m["id"] for m in pages(S.gmail.users().messages().list, "messages", userId="me",
                                   includeSpamTrash=True, maxResults=500)}


def list_drive_owned(S):
    return pages(S.drive.files().list, "files", q="'me' in owners", spaces="drive", pageSize=1000,
                 fields="nextPageToken,files(id,name,mimeType,version,trashed,parents)")


def list_calendars(S):
    return pages(S.cal.calendarList().list, "items", showHidden=True)


def list_events(S, cal):
    return pages(S.cal.events().list, "items", calendarId=cal, showDeleted=True, singleEvents=False, maxResults=2500)


def list_contacts(S):
    return pages(S.people.people().connections().list, "connections", resourceName="people/me",
                 personFields="names,metadata", pageSize=1000)


def list_tasks(S, lid):
    return pages(S.tasks.tasks().list, "items", tasklist=lid, showCompleted=True, showHidden=True,
                 showDeleted=True, maxResults=100)


def baseline_cals(st):
    return sorted(set(st["calendars"].values()) | {"primary"})


def snapshot(ctx, S, st):
    """Record everything in the account now; reset deletes anything created later."""
    b = {"gmail": sorted(list_gmail_ids(S))}
    drive = list_drive_owned(S)
    b["drive"] = sorted(f["id"] for f in drive)
    ver = {f["id"]: f.get("version") for f in drive}
    for group in ("drive_folders", "drive_files"):
        for rec in st[group].values():
            rec["version"] = ver.get(rec["id"], rec.get("version"))
    cals = list_calendars(S)
    b["calendars"] = sorted(c["id"] for c in cals if c.get("accessRole") == "owner" and not c.get("primary"))
    b["events"] = {}
    for cal in baseline_cals(st):
        evs = list_events(S, cal)
        b["events"][cal] = sorted(e["id"] for e in evs if e.get("status") != "cancelled")
        tag = {e["id"]: e.get("etag") for e in evs}
        for rec in st["events"].values():
            if rec["cal"] == cal and rec["id"] in tag:
                rec["etag"] = tag[rec["id"]]
    people = list_contacts(S)
    b["contacts"] = sorted(p["resourceName"] for p in people)
    tag = {p["resourceName"]: p.get("etag") for p in people}
    for rec in st["contacts"].values():
        rec["etag"] = tag.get(rec["resourceName"], rec.get("etag"))
    lists = pages(S.tasks.tasklists().list, "items", maxResults=100)
    b["tasklists"] = sorted(l["id"] for l in lists)
    b["tasks"] = {}
    for lid in ["@default"] + b["tasklists"]:
        items = list_tasks(S, lid)
        b["tasks"][lid] = sorted(t["id"] for t in items if not t.get("deleted"))
        if lid == "@default":
            tag = {t["id"]: t.get("etag") for t in items}
            for rec in st["tasks"].values():
                rec["etag"] = tag.get(rec["id"], rec.get("etag"))
    st["baseline"] = b
    st["baseline_at"] = datetime.now(timezone.utc).isoformat()
    save_state(ctx, st)


def delete_security_alerts(S):
    r = pages(S.gmail.users().messages().list, "messages", userId="me",
              q='from:no-reply@accounts.google.com subject:"Security alert"', maxResults=100)
    ids = [m["id"] for m in r]
    if ids:
        ex(S.gmail.users().messages().batchDelete(userId="me", body={"ids": ids}))
    return len(ids)


# ----------------------------------------------------------------- commands
def counts(S):
    prof = ex(S.gmail.users().getProfile(userId="me")) or {}
    out = {"gmail_messages": prof.get("messagesTotal", 0),
           "drive_files_owned": len([f for f in list_drive_owned(S) if not f.get("trashed")])}
    cals = list_calendars(S)
    out["secondary_calendars"] = len([c for c in cals if c.get("accessRole") == "owner" and not c.get("primary")])
    out["primary_events"] = len([e for e in list_events(S, "primary") if e.get("status") != "cancelled"])
    out["contacts"] = len(list_contacts(S))
    out["tasks"] = len([t for t in list_tasks(S, "@default") if not t.get("deleted")])
    return out


def cmd_audit(ctx):
    S = services(ctx)
    c = counts(S)
    log("Account %s currently holds:" % ctx.account)
    for k, v in c.items():
        log("  %-20s %s" % (k, v))
    if c["drive_files_owned"] or c["contacts"] or c["primary_events"] or c["gmail_messages"] > 10:
        log("\nNot empty. If this is a DEDICATED Deccan account, run wipe before seed.\n"
            "If it is a SHARED pool account, do not wipe; seed adds on top and reset will leave existing items alone.")


def cmd_wipe(ctx):
    if (ARGS.confirm or "").lower() != ctx.account.lower():
        die("wipe deletes everything in the account. Re-run with --confirm %s" % ctx.account)
    S = services(ctx)
    ids = sorted(list_gmail_ids(S))
    for i in range(0, len(ids), 1000):
        ex(S.gmail.users().messages().batchDelete(userId="me", body={"ids": ids[i:i + 1000]}))
    for f in list_drive_owned(S):
        ex_ok(S.drive.files().delete(fileId=f["id"]))
    for c in list_calendars(S):
        if c.get("accessRole") == "owner" and not c.get("primary"):
            ex_ok(S.cal.calendars().delete(calendarId=c["id"]))
    ex(S.cal.calendars().clear(calendarId="primary"))
    rns = [p["resourceName"] for p in list_contacts(S)]
    for i in range(0, len(rns), 500):
        ex(S.people.people().batchDeleteContacts(body={"resourceNames": rns[i:i + 500]}))
    lists = pages(S.tasks.tasklists().list, "items", maxResults=100)
    for l in lists[1:]:
        ex_ok(S.tasks.tasklists().delete(tasklist=l["id"]))
    for t in list_tasks(S, "@default"):
        if not t.get("deleted"):
            ex_ok(S.tasks.tasks().delete(tasklist="@default", task=t["id"]))
    p = os.path.join(state_dir(), ctx.persona + ".json")
    if os.path.exists(p):
        os.rename(p, p + ".wiped-%d" % int(time.time()))
    log("Wiped %s: %d emails, drive, calendars, %d contacts, tasks." % (ctx.account, len(ids), len(rns)))
    log("Note: 'Other contacts' (auto-saved from mail) can't be removed by API: contacts.google.com/other")


def cmd_seed(ctx):
    S = services(ctx)
    st = load_state(ctx)
    if st.get("baseline") and not ARGS.force:
        die("Already seeded (%s). Use reset between runs, or wipe + seed to start over." % st.get("baseline_at"))
    log("Seeding %s -> %s (persona email %s %s)" % (
        ctx.persona, ctx.account, ctx.persona_email, "rewritten to account email" if ctx.rewrite else "kept as-is"))
    seed_gmail(ctx, S, st)
    seed_contacts(ctx, S, st)
    seed_calendar(ctx, S, st)
    seed_drive(ctx, S, st)
    seed_tasks(ctx, S, st)
    if not ARGS.keep_alerts:
        n = delete_security_alerts(S)
        if n:
            log("  Removed %d Google 'Security alert' emails caused by the seeding sign-in" % n)
    log("  Waiting 10s for Contacts/Drive to settle, then recording baseline...")
    time.sleep(10)
    snapshot(ctx, S, st)
    if st["skipped"]:
        log("  Skipped (not recreatable):")
        for k, v in st["skipped"].items():
            log("    - %s" % v)
    log("Done. Next: python3 gab_seed.py verify %s" % ctx.persona)
    manual_checklist(ctx)


def world_index(ctx):
    idx = {}
    for s, data in ctx.bc.items():
        idx[s] = {i for i, _ in extract_ids(s, data)}
    return idx


def live_check(S, st, surface, sid):
    from googleapiclient.errors import HttpError
    try:
        if surface == "GMAIL":
            rec = st["gmail"].get(sid)
            if not rec:
                return False
            m = ex(S.gmail.users().messages().get(userId="me", id=rec["id"], format="minimal")) or {}
            return "TRASH" not in m.get("labelIds", [])
        if surface == "DRIVE":
            rec = st["drive_files"].get(sid) or st["drive_folders"].get(sid)
            return bool(rec) and not (ex(S.drive.files().get(fileId=rec["id"], fields="id,trashed")) or {}).get("trashed")
        if surface == "CALENDAR":
            rec = st["events"].get(sid)
            return bool(rec) and (ex(S.cal.events().get(calendarId=rec["cal"], eventId=rec["id"])) or {}).get("status") != "cancelled"
        if surface == "CONTACTS":
            rec = st["contacts"].get(sid)
            return bool(rec) and bool(ex(S.people.people().get(resourceName=rec["resourceName"], personFields="names")))
        if surface == "TASKS":
            rec = st["tasks"].get(sid)
            return bool(rec) and not (ex(S.tasks.tasks().get(tasklist=rec["list"], task=rec["id"])) or {}).get("deleted")
    except HttpError as e:
        if http_status(e) in (404, 410):
            return False
        raise
    return False


def task_file(ctx, tid, part):
    p = os.path.join(ctx.pdir, part, "tasks", tid + ".json")
    return json.load(open(p)) if os.path.exists(p) else {}


def coverage(ctx, S=None, st=None):
    """Per active task: where each planted record lives and whether it is (or will be) seeded."""
    idx, problems = world_index(ctx), 0
    planted = ctx.world.get("planted_records") or {}
    for tid in ctx.active:
        t1, lt = task_file(ctx, tid, "turn_1"), task_file(ctx, tid, "later_turns")
        attach = t1.get("turn1_attach_files") or []
        log("\n  %s" % tid)
        if tid not in planted:
            log("    WARNING: no planted_records for this task in the snapshot")
            problems += 1
        if attach:
            log("    Turn-1 attachments (rater attaches in chat, NOT seeded): %s" % ", ".join(attach))
        if t1.get("date_map"):
            log("    date_map present (dates shift; follow the updated guide/eval sheet): %s" % json.dumps(t1["date_map"])[:300])
        if lt.get("staged_records"):
            log("    WARNING: later_turns has staged_records (mid-run injection). Ask Google how to apply: %s"
                % json.dumps(lt["staged_records"])[:300])
            problems += 1
        if lt.get("rater_files_turn2_plus"):
            log("    Rater files for later turns: %s" % lt["rater_files_turn2_plus"])
        for surface, raw in (planted.get(tid) or {}).items():
            for sid, label in extract_ids(surface, as_json(raw)):
                if surface in MANUAL_SURFACES:
                    status, bad = "MANUAL - add by hand (no API)", True
                elif surface not in SEEDED_SURFACES:
                    status, bad = "UNSUPPORTED surface - ask Google", True
                elif sid not in idx.get(surface, set()):
                    if label in attach:
                        status, bad = "Turn-1 attachment (correctly not in account)", False
                    else:
                        status, bad = "not in snapshot world (not seeded)", True
                elif S is None:
                    skipped = st and sid in st.get("skipped", {})
                    status, bad = ("will SKIP (no content)", True) if skipped else ("will seed", False)
                else:
                    ok = live_check(S, st, surface, sid)
                    status, bad = ("OK live" if ok else "MISSING in account"), not ok
                problems += bad
                log("    [%s] %-8s %-55s %s" % ("!!" if bad else "ok", surface, label[:55], status))
    return problems


def manual_checklist(ctx):
    log("\nManual steps for %s (%s):" % (ctx.account, ctx.persona_name))
    log("  1. myaccount.google.com > Personal info > Name: set to '%s'" % ctx.persona_name)
    log("  2. myaccount.google.com > Personal info > Addresses > Home: %s" % ctx.home_address())
    log("  3. Gemini: Apps Activity ON, Workspace apps ON (gemini.google.com/apps)")
    log("  4. ChatGPT: connect this same account under Settings > Apps & Connectors")


def cmd_plan(ctx):
    bc = ctx.bc
    log("Persona   : %s (%s)" % (ctx.persona_name, ctx.persona_id))
    log("Persona ID: %s   timezone %s" % (ctx.persona_email, ctx.tz_name))
    log("Account   : %s" % (ctx.account or "(not set in accounts.csv yet)"))
    log("\nWill seed from bard_contexts:")
    log("  Gmail    %d emails (%d sent)" % (len(w_emails(bc)), sum("SENT" in (e.get("labels") or []) for e in w_emails(bc))))
    log("  Contacts %d" % len(w_contacts(bc)))
    log("  Calendar %d events in %d calendars" % (len(w_events(bc)), len(calendar_map(ctx))))
    kinds = {}
    for f in w_files(bc):
        kinds.setdefault(file_kind(f), []).append(f)
    log("  Drive    %d folders; files: %d Docs, %d Sheets, %d PDF/Office, %d text" % (
        len(w_folders(bc)), len(kinds.get("doc", [])), len(kinds.get("sheet", [])),
        len(kinds.get("export", [])), len(kinds.get("text", []))))
    for f in kinds.get("skip", []):
        log("           skip: %s (%s, no recreatable content)" % (f.get("name"), f.get("mime_type")))
    log("  Tasks    %d (recurrence and time of day dropped)" % len(w_tasks(bc)))
    lists, notes = w_keep(bc)
    log("\nCannot seed via API:")
    log("  Keep     %d lists + %d notes" % (len(lists), len(notes)))
    log("  Chat     %d spaces" % len(w_chat(bc)))
    other = [k for k in bc if k not in SEEDED_SURFACES | MANUAL_SURFACES]
    if other:
        log("  Other    %s (ask Google)" % ", ".join(other))
    write_keep_file(ctx)
    log("\nActive tasks: %s" % (", ".join(ctx.active) or "none listed (use --tasks)"))
    st = load_state(ctx) if ctx.account else None
    p = coverage(ctx, None, st)
    log("\n%s" % ("All planted records for the active tasks are seedable." if not p else
                  "%d planted record(s) need attention (marked !!)." % p))


def write_keep_file(ctx):
    lists, notes = w_keep(ctx.bc)
    if not (lists or notes):
        return
    p = os.path.join(state_dir(), ctx.persona + "_KEEP_manual.md")
    with open(p, "w") as fh:
        fh.write("# Keep notes for %s (enter by hand only if an active task needs Keep)\n\n" % ctx.persona_name)
        for l in lists:
            fh.write("## [List] %s\n" % l.get("title", ""))
            for it in l.get("items", []):
                fh.write("- [ ] %s\n" % (it if isinstance(it, str) else it.get("text", json.dumps(it))))
            fh.write("\n")
        for n in notes:
            fh.write("## [Note] %s\n%s\n\n" % (n.get("title", ""), n.get("content", "")))
    log("  (Keep content written to _seed_state/%s_KEEP_manual.md)" % ctx.persona)


def cmd_verify(ctx):
    S = services(ctx)
    st = load_state(ctx)
    if not st.get("baseline"):
        die("Not seeded yet. Run seed first.")
    log("Verifying %s on %s" % (ctx.persona, ctx.account))
    seeded = {"GMAIL": st["gmail"], "CONTACTS": st["contacts"], "CALENDAR": st["events"],
              "DRIVE": {**st["drive_folders"], **st["drive_files"]}, "TASKS": st["tasks"]}
    for s in sorted(SEEDED_SURFACES):
        world = len(extract_ids(s, ctx.bc.get(s, {}))) if s in ctx.bc else 0
        log("  %-8s seeded %3d / snapshot %3d" % (s, len(seeded[s]), world))
    p = coverage(ctx, S, st)
    log("\n%s" % ("PASS: every planted record for the active tasks is live." if not p else
                  "%d item(s) need attention (marked !!)." % p))
    manual_checklist(ctx)


def restore_gmail(ctx, S, st):
    from googleapiclient.errors import HttpError
    users = S.gmail.users()
    base = set(st["baseline"]["gmail"])
    new = sorted(list_gmail_ids(S) - base)
    for i in range(0, len(new), 1000):
        ex(users.messages().batchDelete(userId="me", body={"ids": new[i:i + 1000]}))
    fixed, recreated, threads = 0, 0, {}
    for e in sorted_emails(ctx):
        rec = st["gmail"].get(e["email_id"])
        th = threads.setdefault(e.get("thread_id") or e["email_id"], {"tid": None, "refs": []})
        if not rec:
            continue
        try:
            m = ex(users.messages().get(userId="me", id=rec["id"], format="minimal"))
        except HttpError as err:
            if http_status(err) != 404:
                raise
            m = None
        if m is None:
            new_rec = insert_email(ctx, S, st, e, th)
            st["gmail"][e["email_id"]] = new_rec
            recreated += 1
            continue
        th["tid"] = th["tid"] or m.get("threadId")
        th["refs"].append(rec["mid"])
        cur = set(m.get("labelIds", []))
        if "TRASH" in cur:
            ex(users.messages().untrash(userId="me", id=rec["id"]))
            cur = set((ex(users.messages().get(userId="me", id=rec["id"], format="minimal")) or {}).get("labelIds", []))
        want = set(rec["labelIds"])
        add, rem = sorted(want - cur - GMAIL_UNMODIFIABLE), sorted(cur - want - GMAIL_UNMODIFIABLE)
        if add or rem:
            ex(users.messages().modify(userId="me", id=rec["id"], body={"addLabelIds": add, "removeLabelIds": rem}))
            fixed += 1
    log("  Gmail: deleted %d new, relabeled %d, re-inserted %d" % (len(new), fixed, recreated))


def restore_drive(ctx, S, st):
    base = set(st["baseline"]["drive"])
    deleted = 0
    for f in list_drive_owned(S):
        if f["id"] not in base:
            ex_ok(S.drive.files().delete(fileId=f["id"]))
            deleted += 1
    cur = {f["id"]: f for f in list_drive_owned(S)}
    folders = {f["folder_id"]: f for f in w_folders(ctx.bc)}
    gone = [k for k, r in st["drive_folders"].items() if r["id"] not in cur]
    for k in gone:
        del st["drive_folders"][k]
    if gone:
        seed_folders(ctx, S, st, only=set(gone))
    for k, rec in st["drive_folders"].items():
        f = cur.get(rec["id"])
        if f and (f.get("trashed") or f.get("name") != folders.get(k, {}).get("name", f.get("name"))):
            ex(S.drive.files().update(fileId=rec["id"], body={"trashed": False, "name": folders[k]["name"]}))
    files = {f["file_id"]: f for f in w_files(ctx.bc)}
    recreated = 0
    for k, rec in list(st["drive_files"].items()):
        f = cur.get(rec["id"])
        moved = f and k in files and files[k].get("parent_folder_id") in st["drive_folders"] and \
            st["drive_folders"][files[k]["parent_folder_id"]]["id"] not in (f.get("parents") or [])
        if f and not f.get("trashed") and f.get("version") == rec.get("version") and not moved and not gone:
            continue
        if f:
            ex_ok(S.drive.files().delete(fileId=rec["id"]))
        st["drive_files"][k] = create_drive_file(ctx, S, st, files[k])
        recreated += 1
        save_state(ctx, st)
    log("  Drive: deleted %d new, recreated %d changed/missing files, %d folders" % (deleted, recreated, len(gone)))


def restore_calendar(ctx, S, st):
    base = st["baseline"]
    cals = list_calendars(S)
    for c in cals:
        if c.get("accessRole") == "owner" and not c.get("primary") and c["id"] not in base["calendars"]:
            ex_ok(S.cal.calendars().delete(calendarId=c["id"]))
    have = {c["id"] for c in cals}
    cmap = calendar_map(ctx)
    dead = set()
    for cid, gcal in list(st["calendars"].items()):
        if gcal != "primary" and gcal not in have:
            dead.add(gcal)
            ensure_calendar(ctx, S, st, cid, cmap.get(cid, {}))
    evmap = {event_key(e): e for e in w_events(ctx.bc)}
    deleted, recreated = 0, 0
    by_cal = {}
    for k, rec in st["events"].items():
        rec_cal = rec["cal"]
        if rec_cal in dead:
            rec["cal"] = st["calendars"][evmap[k].get("calendar_id") or "cal_primary"]
            rec["id"] = None
        by_cal.setdefault(rec["cal"], {})[k] = rec
    for cal in baseline_cals(st):
        evs = list_events(S, cal) if cal not in dead else []
        keep = set(base["events"].get(cal, []))
        seeded_ids = {r["id"]: k for k, r in by_cal.get(cal, {}).items()}
        dirty = set()
        cur = {}
        for e in evs:
            if e.get("recurringEventId"):
                if e["recurringEventId"] in seeded_ids:
                    dirty.add(seeded_ids[e["recurringEventId"]])
                elif e["recurringEventId"] not in keep and e.get("status") != "cancelled":
                    ex_ok(S.cal.events().delete(calendarId=cal, eventId=e["id"], sendUpdates="none"))
                continue
            cur[e["id"]] = e
            if e["id"] not in keep and e.get("status") != "cancelled":
                ex_ok(S.cal.events().delete(calendarId=cal, eventId=e["id"], sendUpdates="none"))
                deleted += 1
        for k, rec in by_cal.get(cal, {}).items():
            e = cur.get(rec["id"])
            if e and e.get("status") != "cancelled" and e.get("etag") == rec.get("etag") and k not in dirty:
                continue
            if e and e.get("status") != "cancelled":
                ex_ok(S.cal.events().delete(calendarId=cal, eventId=rec["id"], sendUpdates="none"))
            new = insert_event(ctx, S, st, evmap[k])
            if new:
                st["events"][k] = new
                recreated += 1
    log("  Calendar: deleted %d new, recreated %d changed/missing events" % (deleted, recreated))


def restore_contacts(ctx, S, st):
    base = set(st["baseline"]["contacts"])
    people = list_contacts(S)
    new = [p["resourceName"] for p in people if p["resourceName"] not in base]
    for i in range(0, len(new), 500):
        ex(S.people.people().batchDeleteContacts(body={"resourceNames": new[i:i + 500]}))
    tag = {p["resourceName"]: p.get("etag") for p in people}
    cmap = {c["contact_id"]: c for c in w_contacts(ctx.bc)}
    recreated = 0
    for k, rec in st["contacts"].items():
        rn = rec["resourceName"]
        if rn in tag and tag[rn] == rec.get("etag"):
            continue
        if rn in tag:
            ex_ok(S.people.people().deleteContact(resourceName=rn))
        st["contacts"][k] = insert_contact(ctx, S, cmap[k])
        recreated += 1
    log("  Contacts: deleted %d new, recreated %d changed/missing" % (len(new), recreated))


def restore_tasks(ctx, S, st):
    base = st["baseline"]
    for l in pages(S.tasks.tasklists().list, "items", maxResults=100):
        if l["id"] not in base["tasklists"]:
            ex_ok(S.tasks.tasklists().delete(tasklist=l["id"]))
    items = list_tasks(S, "@default")
    keep = set(base["tasks"].get("@default", []))
    deleted = 0
    for t in items:
        if t["id"] not in keep and not t.get("deleted"):
            ex_ok(S.tasks.tasks().delete(tasklist="@default", task=t["id"]))
            deleted += 1
    cur = {t["id"]: t for t in items}
    tmap = {(t.get("task_id") or gid(json.dumps(t, sort_keys=True))): t for t in w_tasks(ctx.bc)}
    recreated = 0
    for k, rec in st["tasks"].items():
        t = cur.get(rec["id"])
        if t and not t.get("deleted") and t.get("etag") == rec.get("etag"):
            continue
        if t and not t.get("deleted"):
            ex_ok(S.tasks.tasks().delete(tasklist="@default", task=rec["id"]))
        st["tasks"][k] = insert_task(ctx, S, tmap[k])
        recreated += 1
    log("  Tasks: deleted %d new, recreated %d changed/missing" % (deleted, recreated))


def cmd_reset(ctx):
    S = services(ctx)
    st = load_state(ctx)
    if not st.get("baseline"):
        die("No baseline. Run seed first.")
    log("Resetting %s to baseline from %s" % (ctx.account, st.get("baseline_at")))
    restore_gmail(ctx, S, st)
    save_state(ctx, st)
    restore_contacts(ctx, S, st)
    save_state(ctx, st)
    restore_calendar(ctx, S, st)
    save_state(ctx, st)
    restore_drive(ctx, S, st)
    save_state(ctx, st)
    restore_tasks(ctx, S, st)
    time.sleep(5)
    snapshot(ctx, S, st)
    log("Reset done. Run verify, then clear agent-side memory (README: Run protocol) before the next run.")


def cmd_init():
    rows = read_accounts()
    found = sorted(d for d in os.listdir(ROOT)
                   if os.path.exists(os.path.join(ROOT, d, "turn_1", "turn_1_world.json")))
    if not found:
        die("No unzipped persona folders found next to gab_seed.py")
    with open(accounts_path(), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["persona", "persona_email", "active_tasks", "account_email"])
        for d in found:
            m = json.load(open(os.path.join(ROOT, d, "manifest.json")))
            pid = m.get("persona_id", d)
            w.writerow([d, m.get("facts", {}).get("primary_email", ""),
                        " ".join(ACTIVE_TASKS.get(pid, ACTIVE_TASKS.get(d, []))), rows.get(d, "")])
    log("Wrote accounts.csv with %d personas: %s" % (len(found), ", ".join(found)))
    log("Fill in account_email for each (demo account emails only; never passwords).")


def main(argv=None):
    global ARGS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["init", "plan", "auth", "audit", "wipe", "seed", "verify", "reset"])
    ap.add_argument("persona", nargs="?", help="persona folder name, e.g. elena_lindqvist")
    ap.add_argument("--tasks", help="override active task IDs, comma-separated")
    ap.add_argument("--no-rewrite", action="store_true", help="keep the synthetic persona email instead of the account email")
    ap.add_argument("--no-browser", action="store_true", help="auth: print the sign-in URL instead of opening a browser")
    ap.add_argument("--confirm", help="wipe: the account email, to confirm")
    ap.add_argument("--force", action="store_true", help="seed: continue even if a baseline exists")
    ap.add_argument("--keep-alerts", action="store_true", help="seed: keep Google security-alert emails")
    ARGS = ap.parse_args(argv)
    if ARGS.command == "init":
        return cmd_init()
    if not ARGS.persona:
        die("Give a persona folder name, e.g.: python3 gab_seed.py %s elena_lindqvist" % ARGS.command)
    ctx = Ctx(ARGS.persona, need_account=ARGS.command != "plan")
    {"plan": cmd_plan, "auth": cmd_auth, "audit": cmd_audit, "wipe": cmd_wipe, "seed": cmd_seed,
     "verify": cmd_verify, "reset": cmd_reset}[ARGS.command](ctx)


if __name__ == "__main__":
    main()

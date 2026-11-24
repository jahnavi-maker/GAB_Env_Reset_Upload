"""READ-ONLY live check of Wave 2 accounts against their seed record (run on the EC2 host).

Usage (from ~/gab-env-platform/api, with the .env loaded):
    .venv/bin/python ../scripts/verify_wave2_accounts.py <email>:<persona_id> [...]
Prints one JSON line per account: per service want / missing / extra, Gmail Trash+Spam, Drive Trash,
duplicate Drive folders, calendar events shown twice, and `ok` + `problems`.
Google's own "birthday" calendar events (made from the profile birthday) are not counted as extras.
"""
import sys, json, glob, collections, concurrent.futures as cf
sys.path[:0] = ["/home/ubuntu/gab-env-platform/api", "/home/ubuntu/gab-env-platform/seeder"]
from wave2.creds import wave2_credentials
from wave2 import runner
FO = "application/vnd.google-apps.folder"

def check(acc, P):
    b = runner.base(); out = {"n": acc.split("user")[1][:3], "persona": P}
    try:
        S = runner.services(wave2_credentials(acc))
        st = json.load(open(glob.glob(f"/home/ubuntu/gab-data/wave2/state/_seed_state/{P}__{acc.replace('@','_').replace('.','_')}.json")[0]))
        g = S.gmail.users()
        mail = {m["id"] for m in b.pages(g.messages().list, "messages", userId="me", maxResults=500)}
        rec = {r["id"] for r in st["gmail"].values()}
        out["gmail"] = {"want": len(rec), "missing": len(rec - mail), "extra": len(mail - rec),
                        "trash": len(b.pages(g.messages().list, "messages", userId="me", labelIds=["TRASH"], includeSpamTrash=True)),
                        "spam": len(b.pages(g.messages().list, "messages", userId="me", labelIds=["SPAM"], includeSpamTrash=True))}
        recev = {r["id"] for r in st["events"].values()}
        found, slot, bdays = set(), collections.Counter(), []
        cals = [c for c in b.pages(S.cal.calendarList().list, "items") if c.get("accessRole") == "owner"]
        for c in cals:
            for e in b.pages(S.cal.events().list, "items", calendarId=c["id"], maxResults=2500):
                if e.get("eventType") == "birthday":   # Google-made from the profile/contacts birthday, not seed data
                    bdays.append(e.get("summary")); continue
                found.add(e["id"])
            for e in b.pages(S.cal.events().list, "items", calendarId=c["id"], singleEvents=True, timeMin="2026-09-01T00:00:00Z", timeMax="2027-01-31T00:00:00Z", maxResults=2500):
                slot[((e.get("summary") or "").lower(), e["start"].get("dateTime") or e["start"].get("date"))] += 1
        out["calendar"] = {"want": len(recev), "missing": len(recev - found), "extra": len(found - recev), "calendars": len(cals),
                           "shown_twice": sum(1 for v in slot.values() if v > 1), "google_birthdays": bdays}
        items = b.pages(S.drive.files().list, "files", q="'me' in owners and trashed=false", pageSize=1000, fields="nextPageToken,files(id,name,mimeType,parents)")
        by = {i["id"]: i for i in items}
        def path(i, d=0): return (path(by[i["parents"][0]], d + 1) if d < 20 and i.get("parents") and i["parents"][0] in by else "") + "/" + i["name"]
        recd = {r["id"] for k in ("drive_folders", "drive_files") for r in st[k].values()}
        ids = {i["id"] for i in items}
        fold = collections.Counter(path(i) for i in items if i["mimeType"] == FO)
        out["drive"] = {"want": len(recd), "missing": len(recd - ids), "extra": len(ids - recd), "dup_folders": sum(1 for v in fold.values() if v > 1),
                        "trash": len(b.pages(S.drive.files().list, "files", q="'me' in owners and trashed=true", pageSize=1000, fields="nextPageToken,files(id)"))}
        cons = {c["resourceName"] for c in b.pages(S.people.people().connections().list, "connections", resourceName="people/me", personFields="names", pageSize=1000)}
        recc = {r["resourceName"] for r in st["contacts"].values()}
        out["contacts"] = {"want": len(recc), "missing": len(recc - cons), "extra": len(cons - recc)}
        tids = {t["id"] for l in b.pages(S.tasks.tasklists().list, "items") for t in b.pages(S.tasks.tasks().list, "items", tasklist=l["id"], showCompleted=True, showHidden=True) if not t.get("deleted")}
        rect = {r["id"] for r in st["tasks"].values()}
        out["tasks"] = {"want": len(rect), "missing": len(rect - tids), "extra": len(tids - rect)}
        out["name"] = (S.drive.about().get(fields="user(displayName)").execute().get("user") or {}).get("displayName")
        out["persona_name"] = runner.WorldCtx(P, acc).persona_name
        bad = [s for s in ("gmail", "calendar", "drive", "contacts", "tasks") if out[s]["missing"] or out[s]["extra"]]
        bad += [k for k, v in (("gmail trash/spam", out["gmail"]["trash"] + out["gmail"]["spam"]), ("drive trash", out["drive"]["trash"]),
                               ("calendar shown twice", out["calendar"]["shown_twice"]), ("duplicate folders", out["drive"]["dup_folders"])) if v]
        out["ok"] = not bad; out["problems"] = bad
    except Exception as exc:  # noqa: BLE001
        out["ok"] = False; out["problems"] = [f"check failed: {type(exc).__name__}: {str(exc)[:150]}"]
    return out

pairs = [a.split(":") for a in sys.argv[1:]]
with cf.ThreadPoolExecutor(6) as pool:
    for r in pool.map(lambda p: check(*p), pairs):
        print(json.dumps(r), flush=True)

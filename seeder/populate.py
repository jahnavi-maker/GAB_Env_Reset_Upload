#!/usr/bin/env python3
"""Push an UltraEvals persona (or dropped JSON files) into the Google account you sign in as.

Desktop OAuth — put credentials.json next to this script (OAuth client type: Desktop).

  python populate.py --persona Student
  python populate.py --persona Backend_software_engineer --github
  python populate.py --calendar cal.json --gmail mail.json --drive files.json
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent
ENV_ROOT = Path(
    os.environ.get("GAB_PERSONA_ROOT") or (ROOT / ".." / "PKJA_UltraEvals_Environments_")
).expanduser().resolve()
SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/drive",
]

sys.path.insert(0, str(ROOT))
from materialize.runner import run_populate  # noqa: E402


def authenticate(token_path: Path, credentials_path: Path) -> Credentials:
    creds = None
    if token_path.exists():
        creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not credentials_path.exists():
                sys.exit(
                    "Missing credentials.json.\n"
                    "Google Cloud → enable Gmail, Calendar, Drive APIs → "
                    "create OAuth Desktop client → download JSON here as credentials.json"
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(credentials_path), SCOPES)
            creds = flow.run_local_server(port=0)
        token_path.write_text(creds.to_json())
    return creds


def persona_paths(name: str) -> dict[str, Path | None]:
    base = ENV_ROOT / name / "services"
    if not base.exists():
        available = ", ".join(sorted(p.name for p in ENV_ROOT.iterdir() if p.is_dir()))
        sys.exit(f"Unknown persona {name!r}. Folders: {available}")
    cal = base / "calendar" / "data.json"
    mail = base / "email" / "data.json"
    drive = base / "filesystem" / "data.json"
    gh = base / "github"
    return {
        "calendar": cal if cal.exists() else None,
        "gmail": mail if mail.exists() else None,
        "drive": drive if drive.exists() else None,
        "github": gh if gh.exists() else None,
    }


def main() -> None:
    personas = sorted(p.name for p in ENV_ROOT.iterdir() if p.is_dir()) if ENV_ROOT.exists() else []
    parser = argparse.ArgumentParser(description="Seed Gmail / Calendar / Drive from UltraEvals JSON")
    parser.add_argument("--persona", choices=personas, help="Folder under PKJA_UltraEvals_Environments_")
    parser.add_argument("--calendar", type=Path, help="Override calendar data.json")
    parser.add_argument("--gmail", type=Path, help="Override email data.json")
    parser.add_argument("--drive", type=Path, help="Override filesystem data.json")
    parser.add_argument("--no-calendar", action="store_true")
    parser.add_argument("--no-gmail", action="store_true")
    parser.add_argument("--no-drive", action="store_true")
    parser.add_argument("--github", action="store_true", help="Upload github/ tree to Drive (Backend_software_engineer)")
    parser.add_argument("--keep", action="store_true", help="Do not wipe previous seed")
    parser.add_argument("--credentials", type=Path, default=ROOT / "credentials.json")
    parser.add_argument("--token", type=Path, default=ROOT / "token.json")
    args = parser.parse_args()

    paths = {
        "calendar": args.calendar,
        "gmail": args.gmail,
        "drive": args.drive,
        "github": None,
    }
    persona = args.persona or "custom"
    if args.persona:
        bundled = persona_paths(args.persona)
        for key in paths:
            if paths[key] is None:
                paths[key] = bundled[key]
    if not any(paths[k] for k in ("calendar", "gmail", "drive")):
        parser.error("Pass --persona Student  or  --calendar/--gmail/--drive JSON paths")

    creds = authenticate(args.token, args.credentials)
    who = build("oauth2", "v2", credentials=creds).userinfo().get().execute().get("email")
    print(f"Signed in as {who}")
    print(f"Persona {persona}")
    for label, path in paths.items():
        print(f"  {label}: {path or '(none)'}")

    run_populate(
        creds,
        calendar_json=paths["calendar"],
        gmail_json=paths["gmail"],
        drive_json=paths["drive"],
        github_dir=paths["github"],
        persona=persona,
        do_calendar=not args.no_calendar,
        do_gmail=not args.no_gmail,
        do_drive=not args.no_drive,
        do_github=args.github,
        wipe=not args.keep,
        log=print,
        target_email=who,
    )


if __name__ == "__main__":
    main()

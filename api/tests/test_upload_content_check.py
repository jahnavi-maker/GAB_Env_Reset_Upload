"""First-upload good practice: wipe before seed only when the account already has content.

_account_has_content probes Drive/Gmail/Calendar and short-circuits on the first non-empty
surface, so a truly empty account is seeded without a wipe and a polluted one is wiped first.
"""
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

_SEEDER = str(Path(__file__).resolve().parents[2] / "seeder")
if _SEEDER not in sys.path:
    sys.path.insert(0, _SEEDER)

from reset_service import engine  # noqa: E402


class _Exec:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _FakeDrive:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self

    def list(self, **_kw):
        return _Exec({"files": self._files})


class _FakeGmail:
    def __init__(self, msgs):
        self._msgs = msgs

    def users(self):
        return self

    def messages(self):
        return self

    def list(self, **_kw):
        return _Exec({"messages": self._msgs})


class _FakeCal:
    def __init__(self, items):
        self._items = items

    def events(self):
        return self

    def list(self, **_kw):
        return _Exec({"items": self._items})


def _builder(drive_files, gmail_msgs, cal_items):
    def build(name, ver, creds):
        if name == "drive":
            return _FakeDrive(drive_files)
        if name == "gmail":
            return _FakeGmail(gmail_msgs)
        return _FakeCal(cal_items)
    return build


class AccountHasContentTests(unittest.TestCase):
    def _run(self, drive_files, gmail_msgs, cal_items):
        with patch("materialize.auth.build_service", _builder(drive_files, gmail_msgs, cal_items)):
            return engine._account_has_content(object(), log=lambda _m: None)

    def test_empty_account_has_no_content(self):
        self.assertFalse(self._run([], [], []))

    def test_drive_content_detected(self):
        self.assertTrue(self._run([{"id": "f1"}], [], []))

    def test_gmail_content_detected(self):
        self.assertTrue(self._run([], [{"id": "m1"}], []))

    def test_calendar_content_detected(self):
        self.assertTrue(self._run([], [], [{"id": "e1"}]))


if __name__ == "__main__":
    unittest.main()

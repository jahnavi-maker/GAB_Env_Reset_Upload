from __future__ import annotations

import unittest

from googleapiclient.errors import HttpError

import materialize.gmail_sync as gs
from materialize.gmail_sync import wipe_seeded_mail


class _Resp:
    def __init__(self, status):
        self.status = status
        self.reason = "Forbidden"


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Messages:
    def __init__(self, gmail):
        self.g = gmail

    def list(self, userId=None, maxResults=None, includeSpamTrash=None, pageToken=None):
        def go():
            # Return the live (non-trash) ids once; after they're trashed/deleted, empty.
            if self.g.remaining:
                batch = self.g.remaining
                if self.g.mode_hard_delete_allowed:
                    self.g.remaining = []  # deleted -> gone
                # in trash mode the caller trashes them, which our batchModify clears
                return {"messages": [{"id": i} for i in batch]}
            return {"messages": []}

        return _Exec(go)

    def batchDelete(self, userId=None, body=None):
        def go():
            self.g.batch_delete_calls += 1
            if not self.g.mode_hard_delete_allowed:
                raise HttpError(_Resp(403), b'{"error":{"message":"Insufficient Permission"}}')
            self.g.deleted += list(body["ids"])
            return {}

        return _Exec(go)

    def batchModify(self, userId=None, body=None):
        def go():
            self.g.batch_modify_calls += 1
            if "TRASH" in (body.get("addLabelIds") or []):
                self.g.trashed += list(body["ids"])
                self.g.remaining = []  # trashed -> drop out of non-trash listing
            return {}

        return _Exec(go)


class _Users:
    def __init__(self, gmail):
        self.g = gmail

    def messages(self):
        return _Messages(self.g)


class FakeGmail:
    def __init__(self, ids, hard_delete_allowed):
        self.remaining = list(ids)
        self.mode_hard_delete_allowed = hard_delete_allowed
        self.deleted: list[str] = []
        self.trashed: list[str] = []
        self.batch_delete_calls = 0
        self.batch_modify_calls = 0

    def users(self):
        return _Users(self)


class GmailWipeTests(unittest.TestCase):
    def setUp(self):
        self._sleep = gs.time.sleep
        gs.time.sleep = lambda *a, **k: None

    def tearDown(self):
        gs.time.sleep = self._sleep

    def test_delegation_scope_falls_back_to_trash(self):
        g = FakeGmail(["m1", "m2", "m3"], hard_delete_allowed=False)
        n = wipe_seeded_mail(g, lambda _m: None)
        self.assertEqual(n, 3)
        self.assertEqual(sorted(g.trashed), ["m1", "m2", "m3"])  # trashed, not hard-deleted
        self.assertEqual(g.deleted, [])
        self.assertGreaterEqual(g.batch_modify_calls, 1)

    def test_full_scope_hard_deletes(self):
        g = FakeGmail(["m1", "m2"], hard_delete_allowed=True)
        n = wipe_seeded_mail(g, lambda _m: None)
        self.assertEqual(n, 2)
        self.assertEqual(sorted(g.deleted), ["m1", "m2"])
        self.assertEqual(g.trashed, [])


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import MagicMock, patch

from googleapiclient.errors import HttpError

from materialize.gmail_sync import (
    _find_label_id_by_name,
    ensure_label,
    insert_one_message,
    label_is_valid,
    resolve_seed_label_id,
)


def _http_error(status: int, message: str) -> HttpError:
    resp = MagicMock(status=status)
    return HttpError(resp, message.encode())


class GmailLabelResolveTests(unittest.TestCase):
    def test_label_is_valid_false_on_404(self):
        gmail = MagicMock()
        labels = gmail.users.return_value.labels.return_value
        labels.get.return_value.execute.side_effect = _http_error(404, "Not Found")
        self.assertFalse(label_is_valid(gmail, "Label_stale"))

    def test_resolve_uses_hint_when_valid(self):
        gmail = MagicMock()
        labels = gmail.users.return_value.labels.return_value
        labels.get.return_value.execute.return_value = {"id": "Label_ok"}
        log = MagicMock()
        self.assertEqual(resolve_seed_label_id(gmail, log, "Label_ok"), "Label_ok")
        labels.create.assert_not_called()

    def test_resolve_refreshes_stale_hint(self):
        gmail = MagicMock()
        labels = gmail.users.return_value.labels.return_value
        labels.get.return_value.execute.side_effect = _http_error(400, "Invalid label")
        labels.list.return_value.execute.return_value = {
            "labels": [{"name": "GAB-SEED", "id": "Label_fresh"}]
        }
        log = MagicMock()
        self.assertEqual(resolve_seed_label_id(gmail, log, "Label_stale"), "Label_fresh")
        log.assert_called()

    def test_insert_refreshes_stale_label_and_retries(self):
        """A wiped account's stale seed label (400 Invalid label) must refresh+retry,
        not fail every message. Regression for user441/440/443 (26 failures)."""
        gmail = MagicMock()
        msgs = gmail.users.return_value.messages.return_value
        msgs.insert.return_value.execute.side_effect = [
            _http_error(400, "Invalid label"),       # stale label id
            {"id": "msg1", "threadId": "t1"},         # succeeds after refresh
        ]
        refreshed = []

        def _refresh():
            refreshed.append(True)
            return "Label_fresh"

        log = MagicMock()
        with patch("materialize.gmail_sync._build_raw", return_value=("rawb64", 0, [])):
            out = insert_one_message(
                gmail, {"email_id": "e1", "folder": "INBOX"}, {}, log,
                label_id="Label_stale", refresh_label=_refresh,
            )
        self.assertEqual(out["id"], "msg1")
        self.assertTrue(refreshed, "refresh_label was not invoked on a stale label")
        self.assertEqual(msgs.insert.return_value.execute.call_count, 2)

    def test_insert_without_refresh_raises(self):
        """Without a refresh callback a genuine invalid-label error still surfaces."""
        gmail = MagicMock()
        msgs = gmail.users.return_value.messages.return_value
        msgs.insert.return_value.execute.side_effect = _http_error(400, "Invalid label")
        log = MagicMock()
        with patch("materialize.gmail_sync._build_raw", return_value=("rawb64", 0, [])):
            with self.assertRaises(HttpError):
                insert_one_message(
                    gmail, {"email_id": "e1", "folder": "INBOX"}, {}, log,
                    label_id="Label_stale",
                )

    def test_find_label_absent_returns_none_without_list_next(self):
        """gmail.users.labels.list is not paginated: the resource has no list_next, so
        calling it raises AttributeError. Regression for the 'Resource has no attribute
        list_next' crash that failed every wiped account (user441 fail=171)."""
        gmail = MagicMock()
        labels = gmail.users.return_value.labels.return_value
        labels.list.return_value.execute.return_value = {
            "labels": [{"name": "INBOX", "id": "i"}]  # GAB-SEED absent
        }
        log = MagicMock()
        self.assertIsNone(_find_label_id_by_name(gmail, log))
        labels.list_next.assert_not_called()

    def test_ensure_label_409_relists_and_finds_existing(self):
        """Create -> 409 'Label name exists' must re-list and use the existing id,
        not fail the reset. Regression for user448 (3 failures)."""
        gmail = MagicMock()
        labels = gmail.users.return_value.labels.return_value
        labels.list.return_value.execute.side_effect = [
            {"labels": []},                                      # initial find: none
            {"labels": [{"name": "GAB-SEED", "id": "Label_x"}]},  # re-list after 409: found
        ]
        labels.list_next.return_value = None
        labels.create.return_value.execute.side_effect = _http_error(
            409, "Label name exists or conflicts"
        )
        log = MagicMock()
        self.assertEqual(ensure_label(gmail, log), "Label_x")


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import MagicMock

from googleapiclient.errors import HttpError

from materialize.gmail_sync import label_is_valid, resolve_seed_label_id


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


if __name__ == "__main__":
    unittest.main()

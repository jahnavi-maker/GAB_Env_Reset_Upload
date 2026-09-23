from gab_seeder.config import missing_attachment_policy_for_persona
from gab_seeder.drive import reset_drive_all
from gab_seeder.gmail import reset_gmail_all


class Request:
    def __init__(self, value=None, callback=None):
        self.value = value
        self.callback = callback

    def execute(self):
        if self.callback:
            self.callback()
        return self.value or {}


def test_persona_specific_missing_attachment_policy():
    config = {
        "missing_attachment_policy": "error",
        "missing_attachment_policies": {"Applied": "omit"},
    }
    assert missing_attachment_policy_for_persona(config, "Applied") == "omit"
    assert missing_attachment_policy_for_persona(config, "Backend") == "error"


def test_drive_reset_includes_trashed_owned_items(monkeypatch):
    queries = []
    deleted = []

    def fake_list(_drive, query):
        queries.append(query)
        return [{"id": "live", "mimeType": "text/plain"}, {"id": "trashed", "mimeType": "text/plain"}]

    class Files:
        def delete(self, *, fileId):
            return Request(callback=lambda: deleted.append(fileId))

    class Drive:
        def files(self):
            return Files()

    monkeypatch.setattr("gab_seeder.drive.list_drive_files", fake_list)
    result = reset_drive_all(Drive(), dry_run=False)
    assert queries == ["'me' in owners"]
    assert deleted == ["live", "trashed"]
    assert result["deleted"] == 2


def test_gmail_reset_removes_product_created_user_labels(monkeypatch):
    deleted_labels = []
    deleted_batches = []

    class Labels:
        def list(self, *, userId):
            assert userId == "me"
            return Request(
                {
                    "labels": [
                        {"id": "INBOX", "name": "INBOX", "type": "system"},
                        {"id": "baseline", "name": "GAB_BASELINE_x", "type": "user"},
                        {"id": "product", "name": "Created by Product A", "type": "user"},
                    ]
                }
            )

        def delete(self, *, userId, id):
            assert userId == "me"
            return Request(callback=lambda: deleted_labels.append(id))

    class Messages:
        def batchDelete(self, *, userId, body):
            assert userId == "me"
            return Request(callback=lambda: deleted_batches.append(body["ids"]))

    class Users:
        def labels(self):
            return Labels()

        def messages(self):
            return Messages()

    class Gmail:
        def users(self):
            return Users()

    monkeypatch.setattr("gab_seeder.gmail.list_message_ids", lambda _gmail: ["m1", "m2"])
    result = reset_gmail_all(Gmail(), dry_run=False)
    assert deleted_batches == [["m1", "m2"]]
    assert deleted_labels == ["baseline", "product"]
    assert result["user_labels_deleted"] == 2
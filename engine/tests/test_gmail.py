from email import policy
from email.parser import BytesParser

import httplib2
import pytest
from googleapiclient.errors import HttpError

from gab_seeder.gmail import (
    AttachmentError,
    _GmailQuotaPacer,
    _batch_delete_messages,
    _batch_get_draft_metadata,
    _batch_get_message_metadata,
    build_message,
)


def record():
    return {
        "email_id": "abc123",
        "folder": "INBOX",
        "sender": "sender@example.com",
        "recipients": ["receiver@example.com"],
        "cc": [],
        "subject": "Seeded subject",
        "content": "Seeded body",
        "timestamp": "2026-08-24T15:00:00Z",
        "parent_id": "parent456",
        "attachments": {"evidence.txt": "4 B"},
        "is_read": False,
    }


def test_build_message_preserves_thread_headers_and_attachment(tmp_path):
    attachment = tmp_path / "evidence.txt"
    attachment.write_bytes(b"data")
    raw = build_message(
        record(),
        attachment_index={"evidence.txt": (attachment, "text/plain")},
        missing_policy="error",
    )
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert message["X-GAB-Seed-ID"] == "abc123"
    assert "gab-parent456@seed.invalid" in message["In-Reply-To"]
    assert list(message.iter_attachments())[0].get_filename() == "evidence.txt"


def test_missing_attachment_fails():
    with pytest.raises(AttachmentError, match="missing attachment"):
        build_message(record(), attachment_index={}, missing_policy="error")


def test_missing_attachment_can_be_explicitly_omitted():
    raw = build_message(record(), attachment_index={}, missing_policy="omit")
    message = BytesParser(policy=policy.default).parsebytes(raw)
    assert list(message.iter_attachments()) == []


class Request:
    def __init__(self, value):
        self.value = value

    def execute(self):
        return self.value


def test_message_metadata_batches_stay_below_one_second_quota_budget(gmail_quota_units):
    class Batch:
        def __init__(self, owner):
            self.owner = owner
            self.items = []

        def add(self, request, request_id, callback):
            self.items.append((request, request_id, callback))

        def execute(self):
            self.owner.batch_sizes.append(len(self.items))
            for request, request_id, callback in self.items:
                callback(request_id, request.execute(), None)

    class Gmail:
        def __init__(self):
            self.batch_sizes = []

        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **kwargs):
            return Request({"id": kwargs["id"]})

        def new_batch_http_request(self):
            return Batch(self)

    gmail = Gmail()
    ids = [f"m{i}" for i in range(205)]
    assert [item["id"] for item in _batch_get_message_metadata(gmail, ids)] == ids
    assert gmail.batch_sizes == ([3] * 68) + [1]
    assert gmail_quota_units == ([60] * 68) + [20]


def test_draft_metadata_batches_stay_below_one_second_quota_budget(gmail_quota_units):
    class Batch:
        def __init__(self, owner):
            self.owner = owner
            self.items = []

        def add(self, request, request_id, callback):
            self.items.append((request, request_id, callback))

        def execute(self):
            self.owner.batch_sizes.append(len(self.items))
            for request, request_id, callback in self.items:
                callback(request_id, request.execute(), None)

    class Gmail:
        def __init__(self):
            self.batch_sizes = []

        def users(self):
            return self

        def drafts(self):
            return self

        def get(self, **kwargs):
            return Request({"id": kwargs["id"], "message": {"id": "m-" + kwargs["id"]}})

        def new_batch_http_request(self):
            return Batch(self)

    gmail = Gmail()
    ids = [f"d{i}" for i in range(201)]
    assert [item["id"] for item in _batch_get_draft_metadata(gmail, ids)] == ids
    assert gmail.batch_sizes == [3] * 67
    assert gmail_quota_units == [60] * 67


def test_batch_delete_messages_chunks_at_thousand(gmail_quota_units):
    class Messages:
        def __init__(self):
            self.deleted_batches = []

        def batchDelete(self, *, userId, body):
            assert userId == "me"
            return Request(self.deleted_batches.append(list(body["ids"])) or {})

    class Gmail:
        def __init__(self):
            self.messages_resource = Messages()

        def users(self):
            return self

        def messages(self):
            return self.messages_resource

    gmail = Gmail()
    ids = [f"m{i}" for i in range(1001)]
    assert _batch_delete_messages(gmail, ids) == 1001
    assert [len(batch) for batch in gmail.messages_resource.deleted_batches] == [1000, 1]
    assert gmail_quota_units == [50, 50]


def test_quota_pacer_spaces_reserved_units():
    now = [0.0]
    sleeps = []

    def clock():
        return now[0]

    def sleep(seconds):
        sleeps.append(seconds)
        now[0] += seconds

    pacer = _GmailQuotaPacer(units_per_second=100.0, clock=clock, sleeper=sleep)
    pacer.acquire(500)
    pacer.acquire(250)
    pacer.acquire(250)
    assert sleeps == [5.0, 2.5]
    assert pacer.next_available == 10.0


def test_batch_quota_error_propagates_without_immediate_replay(gmail_quota_units):
    response = httplib2.Response({"status": "403", "reason": "rate limited"})
    error = HttpError(
        response,
        b'{"error":{"errors":[{"reason":"rateLimitExceeded"}]}}',
        uri="https://gmail.googleapis.com/gmail/v1/users/me/messages/get",
    )

    class Batch:
        def __init__(self):
            self.items = []

        def add(self, request, request_id, callback):
            self.items.append((request_id, callback))

        def execute(self):
            for request_id, callback in self.items:
                callback(request_id, None, error)

    class Gmail:
        def users(self):
            return self

        def messages(self):
            return self

        def get(self, **kwargs):
            return Request({"id": kwargs["id"]})

        def new_batch_http_request(self):
            return Batch()

    with pytest.raises(HttpError) as captured:
        _batch_get_message_metadata(Gmail(), ["m1", "m2"])
    assert captured.value is error
    assert gmail_quota_units == [40]

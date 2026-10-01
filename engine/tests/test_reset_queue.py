import json
from pathlib import Path

import httplib2
from googleapiclient.errors import HttpError

from gab_seeder.reset_queue import JOB_HEADERS, OPERATOR_HEADERS, SheetResetQueue
from gab_seeder.reset_worker import ResetWorker


def retryable_http_error():
    response = httplib2.Response({"status": "403", "reason": "Calendar usage limits exceeded"})
    content = json.dumps(
        {
            "error": {
                "code": 403,
                "message": "Calendar usage limits exceeded.",
                "errors": [{"reason": "quotaExceeded", "message": "Calendar usage limits exceeded."}],
            }
        }
    ).encode()
    return HttpError(response, content, uri="https://www.googleapis.com/calendar/v3/calendars/primary/events")


class FakeSheets:
    def __init__(self):
        self.tables = {
            "'Reset Jobs [DO NOT EDIT]'!A:R": [list(JOB_HEADERS)],
            "'Operators'!A:D": [list(OPERATOR_HEADERS), ["operator@deccan.ai", "Operator", "TRUE", "operator"]],
        }
        self.updates = []

    def get_values(self, range_name):
        return self.tables.get(range_name, [])

    def update_values(self, range_name, values):
        self.updates.append((range_name, values))
        if range_name.startswith("'Reset Jobs"):
            row_number = int(range_name.split("A", 1)[1].split(":", 1)[0])
            table = self.tables["'Reset Jobs [DO NOT EDIT]'!A:R"]
            while len(table) < row_number:
                table.append([])
            table[row_number - 1] = values[0]
        return {"updatedRows": len(values)}


def job_row(mode="PREVIEW", status="QUEUED", requester="operator@deccan.ai"):
    values = {
        "Job ID": "RST-TEST",
        "Requested At": "2026-09-09T00:00:00+00:00",
        "Requester Email": requester,
        "Account ID": "test-account-410",
        "Account Email": "test-account-410@example.com",
        "Persona": "Student",
        "Mode": mode,
        "Status": status,
        "Phase": "QUEUED",
        "Progress": "0",
        "Detail": "Queued",
        "Client Nonce": "abcdefghijklmnopqrstuvwx",
    }
    return [values.get(header, "") for header in JOB_HEADERS]


def set_job(row, **values):
    row = list(row)
    for key, value in values.items():
        row[JOB_HEADERS.index(key)] = value
    return row


def config_file(tmp_path: Path):
    path = tmp_path / "config.json"
    archive = tmp_path / "archive.zip"
    archive.write_bytes(b"fixture")
    state = tmp_path / "state"
    path.write_text(
        """{
          "archive": "%s",
          "rubrics": "%s",
          "accounts_workbook": "%s",
          "client_secret": "%s",
          "token_dir": "%s",
          "state_dir": "%s",
          "accounts": {"Student": {"email": "test-account-410@example.com", "timezone": null}},
          "spare_accounts": []
        }"""
        % (archive, tmp_path / "rubrics.xlsx", tmp_path / "accounts.xlsx", tmp_path / "secret.json", tmp_path / "tokens", state),
        encoding="utf-8",
    )
    return path


def test_queue_claims_oldest_job():
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row())
    queue = SheetResetQueue(fake)
    claimed = queue.claim_next(worker_id="worker", now="2026-09-09T00:01:00+00:00")
    assert claimed is not None
    assert claimed["Status"] == "RUNNING"
    assert claimed["Worker ID"] == "worker"


def test_queue_honors_retry_wait_cooldown():
    fake = FakeSheets()
    row = job_row(mode="RESUME", status="RETRY_WAIT")
    row[JOB_HEADERS.index("Updated At")] = "2026-09-09T00:00:00+00:00"
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(row)
    queue = SheetResetQueue(fake)
    assert queue.claim_next(
        worker_id="worker", now="2026-09-09T00:29:59+00:00"
    ) is None
    claimed = queue.claim_next(
        worker_id="worker", now="2026-09-09T00:30:00+00:00"
    )
    assert claimed is not None
    assert claimed["Status"] == "RUNNING"
    assert claimed["Mode"] == "RESUME"


def test_retry_wait_cooldown_blocks_same_account_queued_job():
    fake = FakeSheets()
    waiting = job_row(mode="DELTA", status="RETRY_WAIT")
    waiting[JOB_HEADERS.index("Updated At")] = "2026-09-09T00:10:00+00:00"
    queued_same = job_row(status="QUEUED")
    queued_same[JOB_HEADERS.index("Job ID")] = "RST-SAME"
    queued_other = job_row(status="QUEUED")
    queued_other[JOB_HEADERS.index("Job ID")] = "RST-OTHER"
    queued_other[JOB_HEADERS.index("Account ID")] = "test-account-411"
    queued_other[JOB_HEADERS.index("Account Email")] = "other@example.com"
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].extend(
        [waiting, queued_same, queued_other]
    )
    queue = SheetResetQueue(fake)
    claimed = queue.claim_next(
        worker_id="worker", now="2026-09-09T00:20:00+00:00"
    )
    assert claimed is not None
    assert claimed["Job ID"] == "RST-OTHER"
    assert queue.find_job("RST-SAME")["Status"] == "QUEUED"


def test_queue_skips_same_account_but_claims_different_account():
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(status="RUNNING"))
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
        set_job(job_row(status="QUEUED"), **{"Job ID": "RST-SAME"})
    )
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
        set_job(
            job_row(status="QUEUED"),
            **{
                "Job ID": "RST-DIFFERENT",
                "Account ID": "test-account-411",
                "Account Email": "test-account-411@example.com",
            },
        )
    )
    queue = SheetResetQueue(fake)
    claimed = queue.claim_next(worker_id="worker", now="2026-09-09T00:01:00+00:00", max_concurrent=3)
    assert claimed is not None
    assert claimed["Job ID"] == "RST-DIFFERENT"


def test_queue_enforces_max_concurrent_running_jobs():
    fake = FakeSheets()
    for index in range(3):
        fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
            set_job(
                job_row(status="RUNNING"),
                **{
                    "Job ID": f"RST-RUN-{index}",
                    "Account ID": f"test-account-41{index}",
                    "Account Email": f"test-account-41{index}@example.com",
                },
            )
        )
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
        set_job(job_row(status="QUEUED"), **{"Job ID": "RST-WAITING"})
    )
    queue = SheetResetQueue(fake)
    assert queue.claim_next(worker_id="worker", now="2026-09-09T00:01:00+00:00", max_concurrent=3) is None


def test_queue_recovers_abandoned_running_job():
    fake = FakeSheets()
    row = job_row(status="RUNNING")
    row[JOB_HEADERS.index("Updated At")] = "2026-09-09T00:00:00+00:00"
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(row)
    queue = SheetResetQueue(fake)
    recovered = queue.recover_stale_running(
        now="2026-09-09T00:20:00+00:00", stale_after_seconds=900
    )
    assert recovered == ["RST-TEST"]
    job = queue.find_job("RST-TEST")
    assert job["Status"] == "FAILED"
    assert "prior worker stopped" in job["Detail"]


def test_worker_heartbeat_refreshes_running_job_timestamp(tmp_path):
    fake = FakeSheets()
    row = job_row(status="RUNNING")
    row[JOB_HEADERS.index("Updated At")] = "2026-09-09T00:00:00+00:00"
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(row)
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=False,
        worker_id="worker",
        host="host",
    )
    worker.heartbeat("BUSY", "RST-TEST", "Still working")
    assert queue.find_job("RST-TEST")["Updated At"] != "2026-09-09T00:00:00+00:00"


def test_preview_job_never_executes_live_reset(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(status="RUNNING"))
    queue = SheetResetQueue(fake)
    calls = []

    def reset_fn(*args, **kwargs):
        calls.append(kwargs)
        return {"dry_run": kwargs["dry_run"]}

    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=False,
        worker_id="worker",
        host="host",
        reset_fn=reset_fn,
    )
    job = queue.find_job("RST-TEST")
    assert worker.process(job) is True
    assert calls == [{
        "persona": "Student",
        "services": {"gmail", "drive", "calendar"},
        "confirm_account": "test-account-410@example.com",
        "dry_run": True,
    }]
    assert queue.find_job("RST-TEST")["Status"] == "COMPLETED"


def test_worker_accepts_any_verified_deccan_requester(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
        job_row(status="RUNNING", requester="new.employee@deccan.ai")
    )
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=False,
        worker_id="worker",
        host="host",
        reset_fn=lambda *args, **kwargs: {"dry_run": True},
    )
    assert worker.process(queue.find_job("RST-TEST")) is True


def test_worker_rejects_requester_outside_deccan_domain(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
        job_row(status="RUNNING", requester="outsider@example.com")
    )
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=False,
        worker_id="worker",
        host="host",
        reset_fn=lambda *args, **kwargs: {"dry_run": True},
    )
    assert worker.process(queue.find_job("RST-TEST")) is False
    assert "verified @deccan.ai user" in queue.find_job("RST-TEST")["Error"]


def test_live_job_is_rejected_when_worker_live_mode_is_disabled(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(mode="RESET", status="RUNNING"))
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=False,
        worker_id="worker",
        host="host",
        reset_fn=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not run")),
    )
    assert worker.process(queue.find_job("RST-TEST")) is False
    failed = queue.find_job("RST-TEST")
    assert failed["Status"] == "FAILED"
    assert "live reset is disabled" in failed["Error"]


def test_live_job_reports_progress_and_requires_successful_verification(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(mode="RESET", status="RUNNING"))
    queue = SheetResetQueue(fake)
    calls = []

    def reset_fn(*args, **kwargs):
        calls.append(("reset", kwargs["services"]))
        kwargs["progress"]("RESET_GMAIL", "Removing Gmail")
        kwargs["progress"]("RESET_DRIVE", "Removing Drive")
        return {"ok": True}

    def seed_fn(*args, **kwargs):
        calls.append(("seed", kwargs["services"]))
        kwargs["progress"]("SEED_DRIVE", "Restoring Drive")
        kwargs["progress"]("SEED_GMAIL", "Restoring Gmail")
        return {"ok": True}

    def verify_fn(*args, **kwargs):
        calls.append(("verify", kwargs["services"]))
        return {"ok": True}

    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=True,
        worker_id="worker",
        host="host",
        reset_fn=reset_fn,
        seed_fn=seed_fn,
        verify_fn=verify_fn,
    )
    assert worker.process(queue.find_job("RST-TEST")) is True
    completed = queue.find_job("RST-TEST")
    assert calls == [
        ("reset", {"gmail", "drive", "calendar"}),
        ("seed", {"gmail", "drive", "calendar"}),
        ("verify", {"gmail", "drive", "calendar"}),
    ]
    assert completed["Status"] == "COMPLETED"
    assert completed["Progress"] == "100"
    assert completed["Error"] == ""


def test_resume_job_skips_reset_and_continues_from_checkpoints(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(mode="RESUME", status="RUNNING"))
    queue = SheetResetQueue(fake)
    calls = []

    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=True,
        worker_id="worker",
        host="host",
        reset_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resume must not reset services")
        ),
        seed_fn=lambda *args, **kwargs: calls.append("seed") or {"ok": True},
        verify_fn=lambda *args, **kwargs: {"ok": True},
    )
    assert worker.process(queue.find_job("RST-TEST")) is True
    completed = queue.find_job("RST-TEST")
    assert calls == ["seed"]
    assert completed["Status"] == "COMPLETED"
    assert '"skipped": true' in completed["Result JSON"]


def test_transient_google_failure_waits_for_checkpointed_resume(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(
        job_row(mode="RESUME", status="RUNNING")
    )
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=True,
        worker_id="worker",
        host="host",
        reset_fn=lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("resume must not reset services")
        ),
        seed_fn=lambda *args, **kwargs: (_ for _ in ()).throw(retryable_http_error()),
    )
    assert worker.process(queue.find_job("RST-TEST")) is True
    waiting = queue.find_job("RST-TEST")
    assert waiting["Status"] == "RETRY_WAIT"
    assert waiting["Mode"] == "RESUME"
    state = json.loads(waiting["Result JSON"])
    assert state["checkpointed_retry_count"] == 1
    assert "quotaExceeded" in waiting["Error"]


def test_failed_verification_records_service_counts(tmp_path):
    fake = FakeSheets()
    fake.tables["'Reset Jobs [DO NOT EDIT]'!A:R"].append(job_row(mode="RESET", status="RUNNING"))
    queue = SheetResetQueue(fake)
    worker = ResetWorker(
        queue=queue,
        config_path=config_file(tmp_path),
        allow_live=True,
        worker_id="worker",
        host="host",
        reset_fn=lambda *args, **kwargs: {"ok": True},
        seed_fn=lambda *args, **kwargs: {"ok": True},
        verify_fn=lambda *args, **kwargs: {
            "drive": {"expected_seeded_objects": 10, "remote_seeded_objects": 0, "ok": False},
            "gmail": {"expected_seeded_messages": 8, "remote_seeded_messages": 8, "ok": True},
            "ok": False,
        },
    )
    assert worker.process(queue.find_job("RST-TEST")) is False
    failed = queue.find_job("RST-TEST")
    assert failed["Status"] == "FAILED"
    assert '"remote_seeded_objects": 0' in failed["Error"]
    assert '"expected_seeded_objects": 10' in failed["Error"]

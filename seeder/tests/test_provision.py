from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from googleapiclient.errors import HttpError

from materialize.provision.config import ProvisionConfig
from materialize.provision.env_builder import EnvironmentBuilder
from materialize.provision.errors import PERMANENT, TRANSIENT, backoff_seconds, classify_error
from materialize.provision.limiter import TokenBucket
from materialize.provision.planner import PlanError, plan_account_jobs, plan_generate_jobs, validate_accounts
from materialize.provision.pipeline import AccountWork, Pipeline
from materialize.provision.route import (
    DELTA,
    RESEED,
    SEED,
    apply_mode,
    decide_provision_mode,
    last_seeded_persona,
)
from materialize.provision.store import PENDING, PERMANENT_FAILURE, PROCESSING, RETRY, SUCCESS, Job, JobStore


def _http_error(status: int, reason: str = "") -> HttpError:
    resp = SimpleNamespace(status=status, reason=reason)
    return HttpError(resp, reason.encode() if reason else b"error")


def _work(email: str, persona: str, tmp: Path, **kwargs) -> AccountWork:
    env = tmp / persona / "services"
    (env / "calendar").mkdir(parents=True, exist_ok=True)
    (env / "email").mkdir(parents=True, exist_ok=True)
    (env / "filesystem").mkdir(parents=True, exist_ok=True)
    gh = env / "github" / "repo" / "src"
    gh.mkdir(parents=True, exist_ok=True)
    (gh / "main.py").write_text("print(1)\n", encoding="utf-8")
    (env / "github" / "README.md").write_text("hi\n", encoding="utf-8")
    (env / "calendar" / "data.json").write_text(
        json.dumps({"events": [
            {"event_id": "e1", "title": "Standup", "start": 1_700_000_000, "end": 1_700_000_600},
            {"event_id": "e2", "title": "Retro", "start": 1_700_100_000, "end": 1_700_100_600},
        ]}),
        encoding="utf-8",
    )
    (env / "email" / "data.json").write_text(
        json.dumps({"emails": [
            {"email_id": "m1", "sender": "a@x.com", "recipients": ["b@x.com"], "subject": "hi", "content": "x"},
            {"email_id": "m2", "parent_id": "m1", "sender": "b@x.com", "recipients": ["a@x.com"], "subject": "re", "content": "y"},
        ]}),
        encoding="utf-8",
    )
    (env / "filesystem" / "data.json").write_text(
        json.dumps({"files": [
            {"path": "docs/a.txt", "filename": "a.txt", "content": "hello", "size": 5},
            {"path": "docs/b.txt", "filename": "b.txt", "content": "world", "size": 5},
        ]}),
        encoding="utf-8",
    )
    defaults = dict(
        email=email,
        persona=persona,
        environment_id=persona,
        creds=object(),
        calendar_json=env / "calendar" / "data.json",
        gmail_json=env / "email" / "data.json",
        drive_json=env / "filesystem" / "data.json",
        github_dir=env / "github",
        do_calendar=True,
        do_gmail=True,
        do_drive=True,
        do_github=True,
        wipe=True,
        log=lambda _m: None,
    )
    defaults.update(kwargs)
    return AccountWork(**defaults)


class ErrorAndLimiterTests(unittest.TestCase):
    def test_classifies_transient_and_permanent(self):
        self.assertEqual(classify_error(_http_error(429, "rateLimitExceeded")), TRANSIENT)
        self.assertEqual(classify_error(_http_error(403, "usageLimits exceeded")), TRANSIENT)
        self.assertEqual(classify_error(_http_error(503, "backendError")), TRANSIENT)
        self.assertEqual(classify_error(_http_error(400, "invalid")), PERMANENT)
        self.assertEqual(classify_error(_http_error(401, "auth")), PERMANENT)
        self.assertEqual(classify_error(_http_error(403, "forbidden permission")), PERMANENT)
        self.assertEqual(classify_error(_http_error(404, "missing")), PERMANENT)
        self.assertEqual(classify_error(TimeoutError("stall")), TRANSIENT)

    def test_backoff_grows_with_jitter(self):
        self.assertEqual(backoff_seconds(0, jitter=0.5), 1.0)
        self.assertEqual(backoff_seconds(3, jitter=0.5), 8.0)
        self.assertEqual(backoff_seconds(10, jitter=0.5), 64.0)

    def test_token_bucket_smoothes_without_sleeping_per_call_when_tokens_exist(self):
        bucket = TokenBucket(rate=1000.0, burst=5)
        started = time.monotonic()
        for _ in range(5):
            self.assertTrue(bucket.acquire(timeout=0.2))
        self.assertLess(time.monotonic() - started, 0.2)
        self.assertFalse(bucket.try_acquire())


class PlannerTests(unittest.TestCase):
    def test_arbitrary_account_count_and_environment_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            works = [_work(f"user{i:04d}@ex.com", "Student", root) for i in range(200)]
            builder = EnvironmentBuilder()
            artifacts = validate_accounts(works, builder, lambda _m: None)
            self.assertEqual(len(artifacts), 1)
            store = JobStore(root / "jobs.sqlite")
            try:
                gen = plan_generate_jobs(works, artifacts, store)
                self.assertEqual(len(gen), 1)
                self.assertEqual(gen[0].synthetic_id, "env:Student")
                for work in works:
                    plan_account_jobs(work, artifacts["Student"], store, max_file_bytes=40 * 1024 * 1024, log=lambda _m: None)
                cal = [j for j in store.list_account(works[0].email) if j.service == "calendar" and j.action == "insert_event"]
                self.assertEqual(len(cal), 2)
                all_jobs = []
                for work in works:
                    all_jobs.extend(store.list_account(work.email))
                self.assertGreater(len(all_jobs), 200)
                self.assertTrue(all("160" not in j.synthetic_id for j in all_jobs))
            finally:
                store.close()

    def test_services_are_independent_except_github_folder_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root)
            builder = EnvironmentBuilder()
            artifacts = validate_accounts([work], builder, lambda _m: None)
            store = JobStore(root / "jobs.sqlite")
            plan_generate_jobs([work], artifacts, store)
            jobs = plan_account_jobs(work, artifacts["Student"], store, max_file_bytes=10**9, log=lambda _m: None)
            calendar = [j for j in jobs if j.service == "calendar" and j.action == "insert_event"]
            gmail = [j for j in jobs if j.service == "gmail" and j.action == "insert_message"]
            drive = [j for j in jobs if j.service == "drive"]
            for job in calendar:
                self.assertTrue(all(not d.startswith("generated") and not d.startswith("github") for d in job.depends_on))
            for job in gmail:
                self.assertFalse(any(d.startswith("github") for d in job.depends_on))
            file_job = next(j for j in drive if j.action == "upload" and j.source_type == "github" and j.source_path.endswith("main.py"))
            self.assertTrue(any("src" in d or d.endswith("src") for d in file_job.depends_on))
            folders = [j for j in drive if j.action == "create_folder" and j.source_type == "github"]
            self.assertTrue(any(j.payload.get("name") == "Github" for j in folders))
            self.assertTrue(any(j.source_path.endswith("src") or j.payload.get("name") == "src" for j in folders))

    def test_generated_files_become_normalized_drive_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root, do_github=False, wipe=False)
            builder = EnvironmentBuilder()
            artifacts = validate_accounts([work], builder, lambda _m: None)
            store = JobStore(root / "jobs.sqlite")
            jobs = plan_account_jobs(work, artifacts["Student"], store, max_file_bytes=10**9, log=lambda _m: None)
            uploads = [j for j in jobs if j.action == "upload" and j.source_type == "generated"]
            self.assertEqual({j.payload["rel"] for j in uploads}, {"docs/a.txt", "docs/b.txt"})

    def test_generated_drive_goes_into_my_drive_root_no_wrapper(self):
        # data.json files land directly in My Drive (no GAB_UltraEvals wrapper). The
        # data.json folder tree is preserved: a top-level dir parents to "root".
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root, do_github=False, wipe=False)
            builder = EnvironmentBuilder()
            artifacts = validate_accounts([work], builder, lambda _m: None)
            store = JobStore(root / "jobs.sqlite")
            jobs = plan_account_jobs(work, artifacts["Student"], store, max_file_bytes=10**9, log=lambda _m: None)
            gen_folders = [j for j in jobs if j.action == "create_folder" and j.source_type == "generated"]
            # No GAB_UltraEvals wrapper folder is created any more.
            self.assertFalse(any("GAB_UltraEvals" in (j.payload.get("name") or "") for j in gen_folders))
            # The top-level data.json dir ("docs") goes straight into My Drive root.
            docs = next(j for j in gen_folders if j.payload.get("name") == "docs")
            self.assertEqual(docs.payload.get("parent"), "root")
            self.assertNotIn("parent_sid", docs.payload)
            # Files under it still parent to the docs folder (structure preserved).
            for up in (j for j in jobs if j.action == "upload" and j.source_type == "generated"):
                self.assertEqual(up.payload.get("parent_sid"), docs.synthetic_id)

    def test_duplicate_and_malformed_records_fail_only_those_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root)
            cal = json.loads(work.calendar_json.read_text())
            cal["events"].append({"event_id": "e1", "title": "dup", "start": 1, "end": 2})
            cal["events"].append({"title": "broken"})
            work.calendar_json.write_text(json.dumps(cal), encoding="utf-8")
            builder = EnvironmentBuilder()
            artifacts = validate_accounts([work], builder, lambda _m: None)
            store = JobStore(root / "jobs.sqlite")
            jobs = plan_account_jobs(work, artifacts["Student"], store, max_file_bytes=10**9, log=lambda _m: None)
            fails = [j for j in jobs if j.service == "calendar" and j.status == PERMANENT_FAILURE]
            oks = [j for j in jobs if j.service == "calendar" and j.action == "insert_event" and j.status == PENDING]
            self.assertGreaterEqual(len(fails), 2)
            self.assertGreaterEqual(len(oks), 1)

    def test_missing_github_dir_fails_fast(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root, github_dir=root / "missing", do_github=True)
            builder = EnvironmentBuilder()
            with self.assertRaises(PlanError):
                validate_accounts([work], builder, lambda _m: None)

    def test_gmail_child_depends_on_parent_not_drive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root)
            builder = EnvironmentBuilder()
            artifacts = validate_accounts([work], builder, lambda _m: None)
            store = JobStore(root / "jobs.sqlite")
            jobs = plan_account_jobs(work, artifacts["Student"], store, max_file_bytes=10**9, log=lambda _m: None)
            child = next(j for j in jobs if j.synthetic_id == "mail/m2")
            self.assertIn("mail/m1", child.depends_on)
            self.assertFalse(any(d.startswith("generated/file") for d in child.depends_on))


class StoreResumeTests(unittest.TestCase):
    def test_success_is_not_replanned_and_stale_processing_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = JobStore(Path(tmp) / "db.sqlite")
            job = store.upsert(
                Job(
                    job_id="",
                    account_id="a@ex.com",
                    persona_id="p",
                    environment_id="p",
                    service="calendar",
                    action="insert_event",
                    synthetic_id="event/e1",
                    source_type="calendar",
                )
            )
            store.persist_success(job.job_id, "gcal-1")
            again = store.upsert(
                Job(
                    job_id="",
                    account_id="a@ex.com",
                    persona_id="p",
                    environment_id="p",
                    service="calendar",
                    action="insert_event",
                    synthetic_id="event/e1",
                    source_type="calendar",
                )
            )
            self.assertEqual(again.status, SUCCESS)
            self.assertEqual(again.google_object_id, "gcal-1")

            other = store.upsert(
                Job(
                    job_id="",
                    account_id="a@ex.com",
                    persona_id="p",
                    environment_id="p",
                    service="gmail",
                    action="insert_message",
                    synthetic_id="mail/m1",
                    source_type="gmail",
                )
            )
            store.mark(other.job_id, PROCESSING, claimed=True)
            time.sleep(0.05)
            reclaimed = store.reclaim_stale(0.01)
            self.assertEqual(len(reclaimed), 1)
            self.assertEqual(reclaimed[0].status, RETRY)


class FakeExecutor:
    def __init__(self, store: JobStore, events: list[str]):
        self.store = store
        self.events = events
        self.lock = threading.Lock()

    def execute(self, job: Job):
        with self.lock:
            self.events.append(f"{job.service}:{job.action}:{job.account_id}")
        time.sleep(0.01)
        return {"id": f"g-{job.synthetic_id}", "threadId": f"t-{job.synthetic_id}"}


class PipelineIndependenceTests(unittest.TestCase):
    def test_drive_calendar_gmail_progress_together(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            works = [
                _work("a@ex.com", "Student", root, wipe=False, do_github=False),
                _work("b@ex.com", "Student", root, wipe=False, do_github=False),
            ]
            cfg = ProvisionConfig(
                drive_workers=2,
                calendar_workers=2,
                gmail_workers=2,
                generate_workers=1,
                checksum_workers=0,
                drive_rate=100,
                calendar_rate=100,
                calendar_account_rate=100,
                gmail_rate=100,
                generate_rate=100,
                drive_burst=20,
                calendar_burst=20,
                gmail_burst=20,
                max_retries=3,
                stale_processing_s=30,
                max_file_bytes=10**9,
                adaptive=False,
            )
            store = JobStore(root / "pipe.sqlite")
            pipeline = Pipeline(works, run_id=None, log=lambda _m: None, config=cfg, store=store)
            seen: list[str] = []
            fake = FakeExecutor(store, seen)

            def execute(job):
                if job.service == "generate":
                    return {"id": "cache"}
                return fake.execute(job)

            with patch.object(pipeline, "_run_one", wraps=pipeline._run_one):
                original = pipeline._run_one

                def wrapped(job, executor):
                    executor.execute = execute
                    return original(job, executor)

                pipeline._run_one = wrapped
                results = pipeline.run()
            self.assertEqual(set(results), {"a@ex.com", "b@ex.com"})
            services_seen = {row.split(":")[0] for row in seen}
            self.assertTrue({"calendar", "gmail", "drive"} <= services_seen)
            first_cal = next(i for i, row in enumerate(seen) if row.startswith("calendar:"))
            first_drive = next(i for i, row in enumerate(seen) if row.startswith("drive:"))
            first_gmail = next(i for i, row in enumerate(seen) if row.startswith("gmail:"))
            self.assertLess(max(first_cal, first_drive, first_gmail) - min(first_cal, first_drive, first_gmail), 20)

    def test_no_hardcoded_160_in_provision_package(self):
        root = Path(__file__).resolve().parents[1] / "materialize" / "provision"
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("160 accounts", text)
            self.assertNotRegex(text, r"\baccounts\s*=\s*160\b")


class RouteTests(unittest.TestCase):
    def test_decide_mode_from_last_persona(self):
        self.assertEqual(decide_provision_mode(target_persona="Student", last_persona=""), SEED)
        self.assertEqual(decide_provision_mode(target_persona="Student", last_persona="Student"), DELTA)
        self.assertEqual(decide_provision_mode(target_persona="Teacher", last_persona="Student"), RESEED)
        self.assertEqual(
            decide_provision_mode(target_persona="Teacher", last_persona="Student", only_skipped=True),
            DELTA,
        )

    def test_apply_mode_wipe_only_on_reseed(self):
        self.assertFalse(apply_mode(SEED)["wipe"])
        self.assertFalse(apply_mode(DELTA)["wipe"])
        self.assertTrue(apply_mode(RESEED)["wipe"])

    def test_supabase_last_reset_persona_wins_over_local(self):
        manifest = {"last_persona_by_email": {"a@ex.com": "LocalPersona"}}
        row = {"push": {"last_persona": "LocalPersona"}}
        with patch("db_hooks.last_reset_persona", return_value="Student"):
            self.assertEqual(last_seeded_persona(manifest, "a@ex.com", row), "Student")

    def test_local_fallback_when_supabase_empty(self):
        manifest = {"last_persona_by_email": {"a@ex.com": "Student"}}
        with patch("db_hooks.last_reset_persona", return_value=None):
            self.assertEqual(last_seeded_persona(manifest, "a@ex.com", {}), "Student")

    def test_delta_plan_has_no_wipe_and_stamps_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            work = _work("a@ex.com", "Student", root, wipe=False, mode=DELTA)
            builder = EnvironmentBuilder()
            artifacts = validate_accounts([work], builder, lambda _m: None)
            store = JobStore(root / "jobs.sqlite")
            try:
                jobs = plan_account_jobs(
                    work,
                    artifacts[work.environment_id],
                    store,
                    max_file_bytes=10**9,
                    log=lambda _m: None,
                )
                self.assertFalse(any(j.action == "wipe" for j in jobs))
                self.assertTrue(any(j.action == "insert_event" and (j.extra or {}).get("mode") == DELTA for j in jobs))
                cal = next(j for j in jobs if j.action == "insert_event")
                self.assertIn("item", cal.payload)
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()

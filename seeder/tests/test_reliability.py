from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path

import materialize.jobs as jobs
import materialize.runstate as runstate
from materialize.csv_ingest import parse_accounts_csv


class RunStateReliabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_runs = runstate.RUNS
        runstate.RUNS = Path(self.tmp.name)

    def tearDown(self):
        runstate.RUNS = self.old_runs
        self.tmp.cleanup()

    def test_concurrent_atomic_saves_never_leave_invalid_json(self):
        run_id = "atomic"
        errors = []

        def writer(n):
            try:
                for i in range(40):
                    runstate.save_manifest(
                        run_id,
                        {
                            "schema_version": 1,
                            "run_id": run_id,
                            "writer": n,
                            "iteration": i,
                            "accounts": [],
                        },
                    )
                    json.loads(runstate.manifest_path(run_id).read_text())
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(n,)) for n in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(runstate.load_manifest(run_id)["run_id"], run_id)
        self.assertEqual(list(runstate.run_dir(run_id).glob("*.partial")), [])

    def test_restart_marks_running_rows_failed_and_clears_batch(self):
        run_id = "recover"
        runstate.save_manifest(
            run_id,
            {
                "run_id": run_id,
                "batch_job_id": "dead",
                "accounts": [
                    {
                        "email": "a@example.com",
                        "push": {"state": "running", "job_id": "dead"},
                    },
                    {"email": "b@example.com", "push": {"state": "ok"}},
                ],
            },
        )
        self.assertEqual(runstate.recover_interrupted_runs(), 1)
        recovered = runstate.load_manifest(run_id)
        self.assertNotIn("batch_job_id", recovered)
        self.assertEqual(recovered["schema_version"], 1)
        self.assertEqual(recovered["accounts"][0]["push"]["state"], "failed")
        self.assertIsNone(recovered["accounts"][0]["push"]["job_id"])
        self.assertIn("server restarted", recovered["accounts"][0]["push"]["detail"])
        self.assertEqual(recovered["accounts"][1]["push"]["state"], "ok")
        fail_text = (runstate.run_dir(run_id) / "failures.log").read_text()
        self.assertIn("account=a@example.com", fail_text)
        self.assertIn("stage=busy", fail_text)
        self.assertIn("job=dead", fail_text)
        self.assertIn("server restarted", fail_text)

    def test_update_manifest_does_not_lose_sibling_increments(self):
        run_id = "rmw"
        runstate.save_manifest(run_id, {"run_id": run_id, "n": 0, "accounts": []})

        def bump():
            for _ in range(25):
                def mut(data):
                    data["n"] = data.get("n", 0) + 1

                runstate.update_manifest(run_id, mut)

        threads = [threading.Thread(target=bump) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(runstate.load_manifest(run_id)["n"], 100)

    def test_public_sources_never_include_machine_paths(self):
        run_id = "pub"
        email = "a@example.com"
        runstate.save_manifest(
            run_id,
            {
                "run_id": run_id,
                "accounts": [{"email": email, "persona_dir": "", "auth": {}, "push": {}}],
            },
        )
        dest = runstate.drop_path(run_id, email, "calendar")
        dest.write_text('{"events":[]}', encoding="utf-8")
        pub = runstate.public_account(
            runstate.find_account(runstate.load_manifest(run_id), email),
            run_id,
        )
        cal = pub["sources"]["calendar"]
        self.assertEqual(cal["source"], "drop")
        self.assertEqual(cal["label"], "dropped file")
        self.assertEqual(cal["path"], "dropped file")
        self.assertNotIn("/", str(cal.get("path") or ""))
        self.assertNotIn("candidate", cal)
        dumped = json.dumps(pub)
        self.assertNotIn(str(dest.resolve()), dumped)
        self.assertNotIn("Users/", dumped)

    def test_corrupt_manifest_recovers_from_last_backup(self):
        run_id = "backup"
        runstate.save_manifest(run_id, {"run_id": run_id, "accounts": [], "value": 1})
        runstate.save_manifest(run_id, {"run_id": run_id, "accounts": [], "value": 2})
        runstate.manifest_path(run_id).write_text("{broken")
        recovered = runstate.load_manifest(run_id)
        self.assertEqual(recovered["value"], 1)
        self.assertEqual(json.loads(runstate.manifest_path(run_id).read_text())["value"], 1)


class DurableJobLogTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_runs = jobs.RUNS
        jobs.RUNS = Path(self.tmp.name)

    def tearDown(self):
        jobs.RUNS = self.old_runs
        self.tmp.cleanup()

    def test_complete_log_is_persisted_and_secrets_redacted(self):
        job_id = "a" * 32
        jobs.create_job(job_id, {"run_id": "run1", "email": "a@example.com"})
        log = jobs.logger(job_id, prefix="[a@example.com] ")
        log("stage=gmail")
        log("token ghp_ABCDEFG1234567890 must not survive")
        jobs.finish(job_id, "failed")

        text = (jobs.RUNS / "run1" / "jobs" / f"{job_id}.log").read_text()
        self.assertIn("[a@example.com] stage=gmail", text)
        self.assertIn("JOB_FINISHED status=failed", text)
        self.assertNotIn("ghp_ABCDEFG", text)
        self.assertIn("***", text)
        account = jobs.account_log_path("run1", "a@example.com")
        self.assertTrue(account.exists())
        account_text = account.read_text()
        self.assertIn("[a@example.com] stage=gmail", account_text)
        self.assertIn("JOB_FINISHED status=failed", account_text)
        self.assertNotIn("ghp_ABCDEFG", account_text)
        self.assertIn("thread=", account_text)
        self.assertIn("account=a@example.com", account_text)

    def test_parallel_appends_are_intact(self):
        batch = "b" * 32
        jobs.create_job(batch, {"run_id": "run1", "batch": True})

        def worker(index: int) -> None:
            job_id = f"{index:032x}"
            email = f"user{index}@example.com"
            jobs.create_job(job_id, {"run_id": "run1", "email": email, "persona": "student", "thread": 1})
            log = jobs.logger(job_id, mirror_id=batch, prefix=f"[{email}] ")
            for step in range(20):
                log(f"step={step}")
            jobs.finish(job_id, "ok")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        jobs.finish(batch, "ok")

        batch_text = (jobs.RUNS / "run1" / "jobs" / f"{batch}.log").read_text()
        self.assertIn("JOB_FINISHED status=ok", batch_text)
        for index in range(12):
            email = f"user{index}@example.com"
            account = jobs.account_log_path("run1", email, "student")
            text = account.read_text()
            self.assertEqual(text.count("step="), 20)
            self.assertIn("JOB_FINISHED status=ok", text)
            self.assertTrue(text.endswith("\n"))


class CsvGuardTests(unittest.TestCase):
    def test_missing_email_header_is_actionable(self):
        with self.assertRaisesRegex(ValueError, "needs an email column"):
            parse_accounts_csv(b"name,role\nAda,Student\n")

    def test_no_valid_accounts_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "no valid account rows"):
            parse_accounts_csv(b"email,role\nnot-an-email,Student\n")


class TokenMigrateTests(unittest.TestCase):
    def test_copies_unsuffixed_slug_once(self):
        import materialize.auth as auth

        with tempfile.TemporaryDirectory() as tmp:
            old = auth.TOKENS_DIR
            auth.TOKENS_DIR = Path(tmp)
            try:
                email = "user410@gmail.com"
                slug = "user410_at_gmail_com"
                (Path(tmp) / f"{slug}.json").write_text('{"token":"x"}', encoding="utf-8")
                self.assertTrue(auth.migrate_legacy_token(email))
                self.assertTrue(auth.token_path(email).exists())
                self.assertEqual(auth.token_path(email).read_text(encoding="utf-8"), '{"token":"x"}')
                self.assertIn("__", auth.token_path(email).name)
                self.assertFalse(auth.migrate_legacy_token(email))
            finally:
                auth.TOKENS_DIR = old


class JobRehydrateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_runs = jobs.RUNS
        jobs.RUNS = Path(self.tmp.name)

    def tearDown(self):
        jobs.RUNS = self.old_runs
        self.tmp.cleanup()

    def test_get_job_replays_finished_log_from_disk(self):
        job_id = "b" * 32
        dest = jobs.RUNS / "run1" / "jobs" / f"{job_id}.log"
        dest.parent.mkdir(parents=True)
        dest.write_text(
            '{"job_id":"%s","run_id":"run1"}\n12:00:00 hello\n12:00:01 JOB_FINISHED status=partial\n'
            % job_id
        )
        jobs.JOBS.pop(job_id, None)
        job = jobs.get_job(job_id)
        self.assertIsNotNone(job)
        self.assertTrue(job["done"])
        self.assertEqual(job["status"], "partial")
        self.assertTrue(any("hello" in line["message"] for line in job["lines"]))


class BatchStatusTests(unittest.TestCase):
    def test_aggregates_child_outcomes(self):
        from app import batch_status

        self.assertEqual(batch_status(["ok", "ok"]), "ok")
        self.assertEqual(batch_status(["ok", "failed"]), "partial")
        self.assertEqual(batch_status(["failed", "failed"]), "failed")
        self.assertEqual(batch_status(["ok", "partial"]), "partial")


class LoadKindRequiredTests(unittest.TestCase):
    def test_missing_required_source_raises_stage_error(self):
        from materialize.fail import StageError
        from materialize.runner import load_kind

        logged = []
        with self.assertRaises(StageError) as ctx:
            load_kind(Path("/no/such/calendar.json"), "calendar", logged.append, required=True)
        self.assertEqual(ctx.exception.stage, "calendar")
        self.assertTrue(any("FAIL" in line for line in logged))


class LoopbackHostTests(unittest.TestCase):
    def test_accepts_loopback_and_rejects_others(self):
        from app import ALLOWED_HOSTS, loopback_host

        self.assertEqual(loopback_host("127.0.0.1:8765"), "127.0.0.1")
        self.assertEqual(loopback_host("localhost:8765"), "localhost")
        self.assertEqual(loopback_host("[::1]:8765"), "[::1]")
        self.assertIn(loopback_host("127.0.0.1:8765"), ALLOWED_HOSTS)
        self.assertNotIn(loopback_host("evil.example"), ALLOWED_HOSTS)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from materialize.csv_ingest import parse_accounts_csv
from materialize.jobs import append, batch_since, create_job, finish, get_job, snapshot
from materialize.json_util import detect_kind, inspect_and_normalize
from materialize.rebase import rebase, rebase_calendar_events
from materialize.auth import safe_email
from materialize.drive_sync import drive_attempt_stats
from materialize.runner import gmail_attachment_block
from materialize.runstate import (
    batch_pool_settings,
    chunk_accounts,
    load_manifest,
    matched_for_push,
    merge_accounts,
    new_run,
    public_account,
    resolve_sources,
    set_persona_opt_in,
    update_account,
)


class RebaseTests(unittest.TestCase):
    def test_preserves_spacing(self):
        records = [{"timestamp": 100.0}, {"timestamp": 200.0}]
        delta, old_max, new_max = rebase(records, "timestamp", now=1000.0)
        self.assertEqual(old_max, 200.0)
        self.assertEqual(delta, 800.0)
        self.assertEqual(records[0]["timestamp"], 900.0)
        self.assertEqual(records[1]["timestamp"], 1000.0)
        self.assertEqual(new_max, 1000.0)

    def test_millis(self):
        records = [{"timestamp": 1_700_000_000_000}]
        _, old_max, _ = rebase(records, "timestamp", now=1_700_000_100)
        self.assertAlmostEqual(old_max, 1_700_000_000.0)

    def test_calendar_fields(self):
        events = [{"start_datetime": 10.0, "end_datetime": 20.0}]
        rebase_calendar_events(events, now=120.0)
        self.assertEqual(events[0]["start_datetime"], 110.0)
        self.assertEqual(events[0]["end_datetime"], 120.0)


class DetectKindTests(unittest.TestCase):
    def test_objects(self):
        self.assertEqual(detect_kind({"events": []}), "calendar")
        self.assertEqual(detect_kind({"emails": []}), "gmail")
        self.assertEqual(detect_kind({"files": []}), "filesystem")
        self.assertNotEqual(detect_kind({"emails": [{"subject": "x"}]}), "calendar")


class JobReplayTests(unittest.TestCase):
    def test_snapshot_keeps_history(self):
        jid = "jobtest1"
        create_job(jid)
        append(jid, "one")
        append(jid, "two")
        finish(jid, "ok")
        lines = snapshot(jid)
        self.assertEqual([x["message"] for x in lines], ["one", "two"])

    def test_consumer_at_buffer_limit_still_gets_new_lines(self):
        jid = "job-seq-2050"
        create_job(jid)
        for i in range(2000):
            append(jid, f"old-{i}")
        job = get_job(jid)
        _batch, last, dropped = batch_since(job, 0)
        self.assertEqual(last, 2000)
        self.assertEqual(dropped, 0)
        for i in range(50):
            append(jid, f"new-{i}")
        batch, last, dropped = batch_since(job, 2000)
        self.assertEqual(dropped, 0)
        self.assertEqual(len(batch), 50)
        self.assertEqual(last, 2050)
        self.assertTrue(all(line["message"].startswith("new-") for line in batch))


CSV = (
    "\ufeffGoogle account,Password,Benchmark persona/profile\r\n"
    "geminiapp.gab.demo.user410@gmail.com,secret410,Student\r\n"
    "geminiapp.gab.demo.user411@gmail.com,secret411,Applied ML and Data Scientist\r\n"
    "geminiapp.gab.demo.user413@gmail.com,secret413,Data Wizard\r\n"
    "geminiapp.gab.demo.user411@gmail.com,dup,Student\r\n"
    "\r\n\r\n\r\n\r\n\r\n\r\n\r\n\r\n"
)


class SourceResolutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        import materialize.runstate as rs

        self.rs = rs
        self._old = rs.RUNS
        rs.RUNS = Path(self.tmp.name) / "runs"
        rs.RUNS.mkdir()

    def tearDown(self):
        self.rs.RUNS = self._old
        self.tmp.cleanup()

    def test_new_run_auto_binds_matched_persona_files(self):
        parsed = parse_accounts_csv(CSV.encode("utf-8"))
        self.assertEqual(len(parsed["accounts"]), 4)
        self.assertNotIn("secrets", parsed)
        run_id = new_run(parsed["accounts"], parsed["warnings"], has_passwords=True)
        email = "geminiapp.gab.demo.user410@gmail.com"
        sources = resolve_sources(run_id, email, "Student", persona_key="student")
        self.assertEqual(sources["calendar"]["source"], "persona")
        self.assertTrue(str(sources["calendar"]["path"]).endswith("Student/services/calendar/data.json"))
        set_persona_opt_in(run_id, email, "calendar", False, persona_key="student")
        self.assertEqual(
            resolve_sources(run_id, email, "Student", persona_key="student")["calendar"]["source"],
            "unset",
        )
        set_persona_opt_in(run_id, email, "calendar", True, persona_key="student")
        self.assertEqual(
            resolve_sources(run_id, email, "Student", persona_key="student")["calendar"]["source"],
            "persona",
        )

    def test_reupload_merges_new_pairs_and_keeps_auth(self):
        first = parse_accounts_csv(
            (
                "email,role\r\n"
                "geminiapp.gab.demo.user410@gmail.com,Student\r\n"
                "geminiapp.gab.demo.user411@gmail.com,Applied ML and Data Scientist\r\n"
            ).encode()
        )
        run_id = new_run(first["accounts"], first["warnings"])

        def mark(row, _m):
            row["auth"] = {"state": "authorized", "verified_email": row["email"]}

        update_account(run_id, "geminiapp.gab.demo.user410@gmail.com", mark, persona="student")
        second = parse_accounts_csv(
            (
                "email,role\r\n"
                "geminiapp.gab.demo.user410@gmail.com,Student\r\n"
                "geminiapp.gab.demo.user411@gmail.com,Applied ML and Data Scientist\r\n"
                "geminiapp.gab.demo.user410@gmail.com,Applied ML and Data Scientist\r\n"
                "geminiapp.gab.demo.user412@gmail.com,Backend software engineer\r\n"
            ).encode()
        )
        merge_accounts(run_id, second["accounts"], second["warnings"])
        manifest = load_manifest(run_id)
        keys = {(a["email"], a["persona_key"]) for a in manifest["accounts"]}
        self.assertEqual(len(keys), 4)
        kept = next(
            a
            for a in manifest["accounts"]
            if a["email"].endswith("user410@gmail.com") and a["persona_key"] == "student"
        )
        self.assertEqual(kept["auth"]["state"], "authorized")
        self.assertTrue(any("Kept 2" in w for w in manifest["warnings"]))
        self.assertTrue(any("Added 2" in w for w in manifest["warnings"]))

    def test_drop_kind_mismatch(self):
        path = Path(self.tmp.name) / "gmail.json"
        path.write_text(json.dumps({"emails": [{"subject": "hi"}]}))
        inspected = inspect_and_normalize(path, "calendar")
        self.assertFalse(inspected["ok"])
        self.assertEqual(inspected["kind"], "gmail")
        self.assertIn("gmail", inspected["error"].lower())


class WorkspaceEnvTests(unittest.TestCase):
    def test_gmail_refused(self):
        os.environ["ENV_LOADER_AUTH_BACKEND"] = "workspace_delegation"
        os.environ["ENV_LOADER_WORKSPACE_DOMAIN"] = "gabdemo.example"
        os.environ["ENV_LOADER_SA_KEY"] = "/tmp/missing.json"
        from materialize.authbackend import WorkspaceDelegationBackend, reset_backend

        reset_backend()
        try:
            st = WorkspaceDelegationBackend().status("a@gmail.com")
            self.assertEqual(st["state"], "mismatch")
        finally:
            os.environ.pop("ENV_LOADER_AUTH_BACKEND", None)
            os.environ.pop("ENV_LOADER_WORKSPACE_DOMAIN", None)
            os.environ.pop("ENV_LOADER_SA_KEY", None)
            reset_backend()


class SafeEmailTests(unittest.TestCase):
    def test_dot_vs_underscore_do_not_collide(self):
        self.assertNotEqual(safe_email("a.b@x.com"), safe_email("a_b@x.com"))


class DriveAttemptTests(unittest.TestCase):
    def test_expect_equals_attempted_not_raw_count(self):
        files = [
            {"path": "ok.txt", "content": "hello"},
            {"path": "empty.txt", "content": ""},
            {"path": "huge.bin", "content": "x" * 20},
        ]
        stats = drive_attempt_stats(files, max_file_bytes=10)
        self.assertEqual(stats["total"], 3)
        self.assertEqual(stats["ineligible"], 1)
        self.assertEqual(stats["attempted"], 2)
        self.assertNotEqual(stats["attempted"], stats["total"])


class FileIndexBasenameTests(unittest.TestCase):
    def test_matches_path_basename_and_ignores_empty(self):
        from materialize.gmail_sync import _attachment_bytes
        from materialize.runner import file_index

        fs = {
            "files": [
                {"path": "School/Family_Welcome_Letter_Template.docx", "filename": "Family_Welcome_Letter_Template.docx", "content": "real-bytes"},
                {"path": "empty.txt", "filename": "empty.txt", "content": ""},
            ]
        }
        idx = file_index(fs, {"Family_Welcome_Letter_Template.docx"})
        self.assertIn("Family_Welcome_Letter_Template.docx", idx)
        self.assertEqual(idx["Family_Welcome_Letter_Template.docx"], b"real-bytes")
        placeholder_src = _attachment_bytes("reproducer.sh", "(see email body)", {"reproducer.sh": b"#!/bin/sh\n"})
        self.assertEqual(placeholder_src, b"#!/bin/sh\n")
        empty_src = _attachment_bytes("Family_Welcome_Letter_Template.docx", "", idx)
        self.assertEqual(empty_src, b"real-bytes")
        self.assertIsNone(_attachment_bytes("missing.pdf", "", {}))

    def test_build_raw_omits_missing_attachments(self):
        import base64

        from materialize.gmail_sync import _build_raw

        raw, _, omitted = _build_raw(
            {
                "email_id": "e1",
                "content": "body",
                "attachments": {"missing.pdf": "", "keep.txt": ""},
                "sender": "a@b.com",
                "recipients": ["c@d.com"],
                "subject": "s",
                "timestamp": 1,
            },
            {"keep.txt": b"hello-bytes"},
        )
        decoded = base64.urlsafe_b64decode(raw)
        self.assertEqual(omitted, ["missing.pdf"])
        self.assertIn(b'filename="keep.txt"', decoded)
        self.assertIn(b"aGVsbG8tYnl0ZXM=", decoded)
        self.assertNotIn(b"missing.pdf", decoded)
        self.assertNotIn(b"GAB placeholder", decoded)

    def test_skips_repair_when_done_or_filename_missing(self):
        from materialize.runner import attachment_fs_matches, should_repair_gmail_attachments

        mail = {"emails": [{"attachments": {"note.pdf": "", "missing.pdf": ""}}]}
        fs = {"files": [{"filename": "note.pdf", "content": "real-bytes"}]}
        self.assertEqual(attachment_fs_matches(mail, fs), {"note.pdf"})
        self.assertEqual(attachment_fs_matches({"emails": [{"attachments": {"missing.pdf": ""}}]}, fs), set())
        self.assertFalse(
            should_repair_gmail_attachments(has_fs_bytes=True, already_done=True, interrupted=False)
        )
        self.assertTrue(
            should_repair_gmail_attachments(has_fs_bytes=True, already_done=False, interrupted=False)
        )
        self.assertFalse(
            should_repair_gmail_attachments(has_fs_bytes=False, already_done=False, interrupted=False)
        )
        self.assertTrue(
            should_repair_gmail_attachments(has_fs_bytes=False, already_done=False, interrupted=True)
        )


class GmailAttachmentGateTests(unittest.TestCase):
    def test_refuses_without_filesystem(self):
        mail = {"emails": [{"subject": "a", "attachments": {"x": True}}, {"subject": "b"}]}
        msg = gmail_attachment_block(mail, has_filesystem=False, allow=False)
        self.assertIsNotNone(msg)
        self.assertIn("attachments", msg)
        self.assertIsNone(gmail_attachment_block(mail, has_filesystem=True, allow=False))
        self.assertIsNone(gmail_attachment_block(mail, has_filesystem=False, allow=True))


class GitRunTests(unittest.TestCase):
    def test_failure_includes_stderr(self):
        from materialize.github_repo import git_run

        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(RuntimeError) as ctx:
                git_run(["definitely-not-a-command"], cwd=Path(td), env=os.environ.copy())
        self.assertIn("failed", str(ctx.exception))
        self.assertIn("definitely-not-a-command", str(ctx.exception))

    def test_force_add_includes_gitignored_files(self):
        from materialize.github_repo import git_run

        env = os.environ.copy()
        root = Path(__file__).resolve().parent.parent / ".tmp" / "git-force-add"
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True)
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / ".gitignore").write_text("secret.txt\n")
        (root / "secret.txt").write_text("keep-me")
        (root / "ok.txt").write_text("ok")
        git_run(["init", "-b", "main", "--template="], cwd=root, env=env)
        git_run(["add", "-A"], cwd=root, env=env)
        names = git_run(["diff", "--cached", "--name-only"], cwd=root, env=env).stdout.decode()
        self.assertNotIn("secret.txt", names)
        git_run(["add", "-A", "-f"], cwd=root, env=env)
        names = git_run(["diff", "--cached", "--name-only"], cwd=root, env=env).stdout.decode()
        self.assertIn("secret.txt", names)
        self.assertIn("ok.txt", names)


class MatchedForPushTests(unittest.TestCase):
    def test_filters_one_persona(self):
        manifest = {
            "accounts": [
                {"email": "a@x.com", "persona_status": "matched", "persona_key": "student"},
                {"email": "b@x.com", "persona_status": "matched", "persona_key": "student"},
                {"email": "c@x.com", "persona_status": "matched", "persona_key": "backend_software_engineer"},
                {"email": "d@x.com", "persona_status": "missing", "persona_key": "student"},
            ]
        }
        all_matched = matched_for_push(manifest)
        self.assertEqual(len(all_matched), 3)
        students = matched_for_push(manifest, "Student")
        self.assertEqual([a["email"] for a in students], ["a@x.com", "b@x.com"])
        none = matched_for_push(manifest, "indie_game_designer")
        self.assertEqual(none, [])
        ordered = matched_for_push({
            "accounts": [
                {"email": "user10@deccanexperts.us", "persona_status": "matched", "persona_key": "student"},
                {"email": "user2@deccanexperts.us", "persona_status": "matched", "persona_key": "backend_software_engineer"},
                {"email": "user01@deccanexperts.us", "persona_status": "matched", "persona_key": "applied_ml_and_data_scientist"},
            ]
        })
        self.assertEqual(
            [a["email"] for a in ordered],
            ["user01@deccanexperts.us", "user2@deccanexperts.us", "user10@deccanexperts.us"],
        )


class GithubTreeTests(unittest.TestCase):
    def test_skips_git_dir(self):
        from materialize.github_sync import iter_github_files

        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(root, ignore_errors=True))
        (root / "src").mkdir()
        (root / "src" / "app.py").write_text("print(1)\n")
        (root / ".git").mkdir()
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        names = [p.name for p in iter_github_files(root)]
        self.assertEqual(names, ["app.py"])


class BatchPoolTests(unittest.TestCase):
    def test_defaults_and_chunks(self):
        threads, per = batch_pool_settings(None, None)
        self.assertEqual((threads, per), (10, 20))
        threads, per = batch_pool_settings(10, 20)
        self.assertEqual((threads, per), (10, 20))
        threads, per = batch_pool_settings(99, 0)
        self.assertEqual(threads, 20)
        self.assertEqual(per, 1)
        rows = [{"email": f"user{n}@x.com"} for n in (45, 1, 21, 2)]
        chunks = chunk_accounts(rows, 2)
        self.assertEqual([a["email"] for a in chunks[0]], ["user1@x.com", "user2@x.com"])
        self.assertEqual([a["email"] for a in chunks[1]], ["user21@x.com", "user45@x.com"])

    def test_module_slots_stay_bounded(self):
        from materialize.runstate import module_slot_limit

        self.assertEqual(module_slot_limit(), 12)
        os.environ["GAB_MODULE_SLOTS"] = "99"
        try:
            self.assertEqual(module_slot_limit(), 60)
        finally:
            os.environ.pop("GAB_MODULE_SLOTS", None)


if __name__ == "__main__":
    unittest.main()

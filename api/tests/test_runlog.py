"""Readable async run logs: per-account folder + shared common/all.log."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service.runlog import (  # noqa: E402
    RunLog,
    classify_event,
    detect_services,
    format_progress,
    purge_old_logs,
    read_account_progress,
)


class FormatTests(unittest.TestCase):
    def test_progress_includes_each_service_and_eta(self) -> None:
        text = format_progress(
            {
                "done": 40,
                "total": 100,
                "left": 60,
                "elapsed_s": 20,
                "services": {
                    "drive": {"done": 20, "total": 50, "left": 30, "failed": 0},
                    "gmail": {"done": 10, "total": 30, "left": 20, "failed": 1},
                    "calendar": {"done": 10, "total": 20, "left": 10, "failed": 0},
                },
            }
        )
        self.assertIn("40/100 jobs", text)
        self.assertIn("remaining", text)
        self.assertIn("drive 20/50", text)
        self.assertIn("gmail 10/30", text)
        self.assertIn("calendar 10/20", text)

    def test_detect_services(self) -> None:
        self.assertEqual(detect_services("Drive files 12/400 uploaded"), ["drive"])
        self.assertEqual(detect_services("gmail insert ok"), ["gmail"])
        self.assertEqual(detect_services("calendar event created"), ["calendar"])

    def test_classify_drops_pipeline_and_verify(self) -> None:
        self.assertIsNone(classify_event("[pipeline] jobs 12/400 (1.2/s)"))
        self.assertIsNone(classify_event("Verify gmail 350/250"))
        self.assertEqual(classify_event("Full Gmail wipe: permanently deleted 12 messages")[0], "wipe")
        self.assertEqual(classify_event("seeder start email=a@b.com")[0], "start")


class RunLogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())

    def test_writes_account_folder_and_common_log(self) -> None:
        rlog = RunLog("run-abc", "user@gmail.com", persona="Student", mode="reset", root=self.root)
        rlog.start()
        rlog.event("[pipeline] jobs 3/10 (1.0/s)")
        rlog.event("Verify gmail 10/10")
        rlog.event("Full Gmail wipe: permanently deleted 4 messages")
        rlog.progress(
            {
                "done": 3,
                "total": 10,
                "left": 7,
                "failed": 0,
                "services": {
                    "drive": {"done": 3, "total": 6, "left": 3, "failed": 0},
                    "gmail": {"done": 0, "total": 2, "left": 2, "failed": 0},
                    "calendar": {"done": 0, "total": 2, "left": 2, "failed": 0},
                },
            }
        )
        rlog.finish("completed")
        rlog.close()

        acct = self.root / "accounts" / "user_gmail.com"
        drive = (acct / "drive.log").read_text(encoding="utf-8")
        gmail = (acct / "gmail.log").read_text(encoding="utf-8")
        calendar = (acct / "calendar.log").read_text(encoding="utf-8")
        common = (self.root / "common" / "all.log").read_text(encoding="utf-8")

        self.assertIn("start", drive)
        self.assertIn("drive 3/6", drive)
        self.assertIn("done", drive)
        self.assertNotIn("[pipeline]", drive)
        self.assertNotIn("Verify gmail", gmail)
        self.assertIn("wipe", gmail)
        self.assertIn("calendar 0/2", calendar)
        self.assertIn("user@gmail.com", common)
        self.assertIn("start", common)
        self.assertNotIn("verified", common.lower())

        self.assertFalse((self.root / "runs").exists())
        self.assertFalse((self.root / "emails").exists())

        snap = read_account_progress("user@gmail.com", self.root)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["services"]["drive"]["done"], 3)
        self.assertEqual(snap["services"]["drive"]["state"], "in_progress")
        self.assertIn("updated_at", snap)
        json.loads((acct / "progress.json").read_text(encoding="utf-8"))

    def test_purge_deletes_files_older_than_retention(self) -> None:
        old = self.root / "accounts" / "old_x.com" / "gmail.log"
        fresh = self.root / "accounts" / "new_x.com" / "drive.log"
        old.parent.mkdir(parents=True)
        fresh.parent.mkdir(parents=True)
        old.write_text("old\n", encoding="utf-8")
        fresh.write_text("new\n", encoding="utf-8")
        stale = time.time() - 6 * 86400
        os.utime(old, (stale, stale))
        removed = purge_old_logs(self.root, days=5)
        self.assertGreaterEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())


if __name__ == "__main__":
    unittest.main()

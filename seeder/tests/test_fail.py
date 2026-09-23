from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from materialize.fail import (
    describe_exception,
    fail_line,
    log_fail,
    log_warn,
    next_for,
    persist_fail,
    redact,
)


class FailLineTests(unittest.TestCase):
    def test_includes_account_stage_error_and_next(self):
        line = fail_line("a@b.com", "gmail", "Invalid JSON in data.json")
        self.assertIn("FAIL", line)
        self.assertIn("account=a@b.com", line)
        self.assertIn("stage=gmail", line)
        self.assertIn("error=Invalid JSON", line)
        self.assertIn("next=", line)
        self.assertIn("why=", line)
        self.assertIn("type=", line)
        self.assertIn("Drop it on the matching card", line)

    def test_file_path_when_provided(self):
        line = fail_line("a@b.com", "filesystem", "file not found", path="data.json")
        self.assertIn("file=data.json", line)
        self.assertIn("Use persona JSON", line)

    def test_redacts_pat(self):
        line = fail_line("a@b.com", "github", "bad token ghp_ABCDEFG1234567890")
        self.assertNotIn("ghp_ABCDEFG", line)
        self.assertIn("***", line)

    def test_redacts_other_secret_classes(self):
        line = fail_line(
            "a@b.com",
            "auth",
            "ya29.A0ABearer and 1//refresh and GOCSPX-abc and github_pat_ZZZ_1 and Bearer abc.def",
        )
        self.assertNotIn("ya29.", line)
        self.assertNotIn("1//", line)
        self.assertNotIn("GOCSPX-", line)
        self.assertNotIn("github_pat_", line)
        self.assertNotIn("Bearer abc", line)

    def test_oauth_hint(self):
        self.assertIn("service-account", next_for("oauth", "invalid_grant expired"))
        self.assertIn("Client ID", next_for("oauth", "unauthorized_client scopes"))


class MailIdTests(unittest.TestCase):
    def test_strips_gab_message_id(self):
        from materialize.gmail_sync import _normalize_msgid

        self.assertEqual(_normalize_msgid("<abc-123@gab.ultraevals.local>"), "abc-123")

    def test_quota_hint(self):
        self.assertIn("Wait a few minutes", next_for("calendar", "quotaExceeded"))

    def test_includes_job_id_when_provided(self):
        line = fail_line("a@b.com", "gmail", "boom", job_id="abc123")
        self.assertIn("job=abc123", line)

    def test_persist_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            import materialize.runstate as runstate

            orig = runstate.RUNS
            runstate.RUNS = Path(tmp)
            try:
                persist_fail("run1", "FAIL account=a@b.com | stage=gmail | error=boom | next=retry")
                text = (Path(tmp) / "run1" / "failures.log").read_text()
                self.assertIn("account=a@b.com", text)
                self.assertIn("stage=gmail", text)
            finally:
                runstate.RUNS = orig


class RedactTests(unittest.TestCase):
    def test_collapses_whitespace(self):
        self.assertEqual(redact("a   b\n c"), "a b c")


class ExceptionDetailTests(unittest.TestCase):
    def test_runtime_error_includes_type_why_and_trace(self):
        seen: list[str] = []

        def boom() -> None:
            raise RuntimeError("calendar quotaExceeded for this user")

        try:
            boom()
        except RuntimeError as exc:
            detail = describe_exception(exc)
            self.assertEqual(detail["type"], "RuntimeError")
            self.assertIn("quotaExceeded", detail["message"])
            self.assertIn("RuntimeError", detail["trace"])
            self.assertIn("quotaExceeded", detail["trace"])
            line = log_fail(seen.append, "a@b.com", "calendar", exc, job_id="job1")
        self.assertIn("type=RuntimeError", line)
        self.assertIn("why=", line)
        self.assertTrue(any(row.startswith("EXCEPTION RuntimeError") for row in seen))
        self.assertTrue(any("TRACE" in row for row in seen))

    def test_redacts_secret_in_traceback(self):
        try:
            raise RuntimeError("token ghp_ABCDEFG1234567890 leaked")
        except RuntimeError as exc:
            detail = describe_exception(exc)
        self.assertNotIn("ghp_ABCDEFG", detail["message"])
        self.assertNotIn("ghp_ABCDEFG", detail["trace"])


class WarnTests(unittest.TestCase):
    def test_warn_does_not_persist(self):
        with tempfile.TemporaryDirectory() as tmp:
            import materialize.runstate as runstate

            orig = runstate.RUNS
            runstate.RUNS = Path(tmp)
            seen = []
            try:
                line = log_warn(seen.append, "a@b.com", "verify", "short counts")
                self.assertTrue(line.startswith("WARN"))
                self.assertEqual(seen, [line])
                self.assertFalse((Path(tmp) / "run1" / "failures.log").exists())
            finally:
                runstate.RUNS = orig


if __name__ == "__main__":
    unittest.main()

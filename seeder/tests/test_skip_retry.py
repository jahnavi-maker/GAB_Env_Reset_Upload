from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from materialize.skip_retry import empty_github_relpaths, parse_skip_log, plan_has_work


class ParseSkipLogTests(unittest.TestCase):
    def test_parses_github_drive_gmail(self):
        text = """
2026-09-20T04:02:01.492Z thread=gab-gh_5 | [user0160@deccanexperts.us] Skip GitHub file pkg/domain/ru_stats.go: The read operation timed out
2026-09-20T02:33:39.202Z | [user056@deccanexperts.us] Skip Drive file 722 (Repos/mechlens-main/docs/make_docs.py): The read operation timed out. next=Untick Drive
2026-09-19T20:58:42.026Z | [user0123@deccanexperts.us] Skip email 687ed8e2a0361ad7a1919694 thread/insert: <HttpError 502
"""
        parsed = parse_skip_log(text)
        self.assertEqual(parsed["github"], {"pkg/domain/ru_stats.go"})
        self.assertEqual(parsed["drive"], {"Repos/mechlens-main/docs/make_docs.py"})
        self.assertEqual(parsed["gmail"], {"687ed8e2a0361ad7a1919694"})

    def test_empty_github_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "keep.txt").write_text("x")
            (root / "empty.toml").write_text("")
            nested = root / "pkg"
            nested.mkdir()
            (nested / "BUILD.bazel").write_text("")
            found = empty_github_relpaths(root)
        self.assertEqual(found, {"empty.toml", "pkg/BUILD.bazel"})

    def test_plan_has_work(self):
        self.assertFalse(plan_has_work({"github": [], "drive": [], "gmail": []}))
        self.assertTrue(plan_has_work({"github": ["a"], "drive": [], "gmail": []}))


if __name__ == "__main__":
    unittest.main()

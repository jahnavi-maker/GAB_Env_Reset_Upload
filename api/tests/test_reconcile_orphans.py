"""Drive reconcile orphan detection (_drive_orphans).

Regression for the bug where a reconcile left model-A/agent files behind when they were
saved INSIDE a seeded persona folder: the old rule spared any file whose ancestor was any
seeded folder. The fix scopes that keep to the seeded GitHub subtree only, so:
  * an agent file in a seeded persona folder (e.g. Ad Concepts/) IS an orphan -> deleted
  * a dup-name repo file inside a seeded github folder is STILL kept (no job row of its own)
  * seeded files/folders are kept; a never-seeded account treats everything as an orphan
"""
import os
import unittest

os.environ.setdefault("GAB_RESET_SIMULATE", "1")
os.environ.setdefault("SUPABASE_URL", "")
os.environ.setdefault("SUPABASE_KEY", "")

from reset_service import engine  # noqa: E402


class _FakeDrive:
    """Minimal Drive stub: files().list(...).execute() -> {'files': [{id, parents}, ...]}."""

    def __init__(self, files):
        self._files = files

    def files(self):
        return self

    def list(self, **_kw):
        files = self._files

        class _Req:
            def execute(self_inner):
                return {"files": files}

        return _Req()


# A tree:  Ad Concepts/ (seeded persona folder) and Github/ (seeded repo folder)
FILES = [
    {"id": "F_persona", "parents": []},                 # seeded persona folder
    {"id": "S_seed", "parents": ["F_persona"]},         # seeded file in it
    {"id": "A_agent", "parents": ["F_persona"]},        # AGENT file in it  -> orphan
    {"id": "F_github", "parents": []},                  # seeded github folder
    {"id": "G_recorded", "parents": ["F_github"]},      # seeded repo file (id recorded)
    {"id": "G_collided", "parents": ["F_github"]},      # repo file w/ no job row (collision)
    {"id": "A_root", "parents": []},                    # AGENT file at root -> orphan
]
BASELINE = {"F_persona", "S_seed", "F_github", "G_recorded"}
GITHUB = {"F_github"}


class DriveOrphanTests(unittest.TestCase):
    def test_agent_file_in_persona_folder_is_orphan(self):
        orphans = engine._drive_orphans(_FakeDrive(FILES), BASELINE, GITHUB)
        self.assertIn("A_agent", orphans)   # the user501 bug: now correctly an orphan
        self.assertIn("A_root", orphans)

    def test_seeded_items_are_kept(self):
        orphans = engine._drive_orphans(_FakeDrive(FILES), BASELINE, GITHUB)
        for kept in ("F_persona", "S_seed", "F_github", "G_recorded"):
            self.assertNotIn(kept, orphans)

    def test_github_dupname_file_is_kept(self):
        # a repo file with no job row of its own is spared because it lives under a github folder
        orphans = engine._drive_orphans(_FakeDrive(FILES), BASELINE, GITHUB)
        self.assertNotIn("G_collided", orphans)

    def test_exact_orphan_set(self):
        orphans = engine._drive_orphans(_FakeDrive(FILES), BASELINE, GITHUB)
        self.assertEqual(orphans, {"A_agent", "A_root"})

    def test_without_github_scope_persona_file_still_orphan(self):
        # proves the fix: with no github protection, the OLD ancestry keep no longer applies,
        # so a file under a seeded persona folder is still flagged (not spared).
        orphans = engine._drive_orphans(_FakeDrive(FILES), BASELINE, set())
        self.assertIn("A_agent", orphans)
        self.assertIn("G_collided", orphans)  # no github scope -> not spared here

    def test_empty_baseline_everything_is_orphan(self):
        orphans = engine._drive_orphans(_FakeDrive(FILES), set(), GITHUB)
        self.assertEqual(orphans, {f["id"] for f in FILES})


if __name__ == "__main__":
    unittest.main()

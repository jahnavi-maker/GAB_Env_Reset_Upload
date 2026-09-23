from __future__ import annotations

import unittest

from materialize.verify import verify_seed


class _Exec:
    def __init__(self, payload):
        self._payload = payload

    def execute(self):
        return self._payload


class _Paged:
    """Returns each page in turn, so pagination is exercised rather than assumed."""

    def __init__(self, pages):
        self._pages = list(pages)
        self.calls = 0

    def list(self, **_kwargs):
        page = self._pages[min(self.calls, len(self._pages) - 1)]
        self.calls += 1
        return _Exec(page)


class FakeCalendar:
    def __init__(self, pages):
        self._paged = _Paged(pages)

    def events(self):
        return self._paged


class FakeGmail:
    def __init__(self, labels, pages):
        self._labels = _Exec({"labels": labels})
        self._messages = _Paged(pages)

    def users(self):
        return self

    def labels(self):
        return self

    def list(self, **_kwargs):
        return self._labels

    def messages(self):
        return self._messages


class FakeDrive:
    def __init__(self, pages):
        self._paged = _Paged(pages)

    def files(self):
        return self._paged


def _run(calendar, gmail, drive, *, expect_cal, expect_mail, expect_drive, ineligible=None):
    return verify_seed(
        None,
        persona="Student",
        expect_calendar=expect_cal,
        expect_gmail=expect_mail,
        expect_drive=expect_drive,
        folder_id="folder-1",
        log=lambda _msg: None,
        calendar=calendar,
        gmail=gmail,
        drive=drive,
        drive_ineligible=ineligible,
    )


def _calendar(n):
    return FakeCalendar([{"items": [{"id": f"e{i}"} for i in range(n)]}])


def _gmail(n):
    return FakeGmail(
        [{"id": "lab-1", "name": "GAB-SEED"}],
        [{"messages": [{"id": f"m{i}"} for i in range(n)]}],
    )


def _drive(files):
    return FakeDrive([{"files": files}])


def _file(name):
    return {"id": name, "name": name, "mimeType": "text/plain"}


class VerifyToneTests(unittest.TestCase):
    def test_exact_counts_are_ok(self):
        result = _run(
            _calendar(3),
            _gmail(2),
            _drive([_file("a.txt"), _file("b.txt")]),
            expect_cal=3,
            expect_mail=2,
            expect_drive=2,
        )
        self.assertEqual(result["overall"], "ok")
        self.assertEqual(result["modules"]["calendar"]["tone"], "ok")
        self.assertEqual(result["modules"]["gmail"]["got"], 2)
        self.assertEqual(result["modules"]["drive"]["got"], 2)

    def test_short_count_is_warn_not_failure(self):
        result = _run(
            _calendar(2),
            _gmail(2),
            _drive([_file("a.txt"), _file("b.txt")]),
            expect_cal=3,
            expect_mail=2,
            expect_drive=2,
        )
        self.assertEqual(result["modules"]["calendar"]["tone"], "warn")
        self.assertEqual(result["overall"], "partial")

    def test_zero_against_nonzero_source_is_error(self):
        result = _run(
            _calendar(0),
            _gmail(2),
            _drive([_file("a.txt")]),
            expect_cal=4,
            expect_mail=2,
            expect_drive=1,
        )
        self.assertEqual(result["modules"]["calendar"]["tone"], "err")
        self.assertEqual(result["overall"], "failed")

    def test_unselected_module_is_skipped_not_counted(self):
        result = _run(
            _calendar(0),
            _gmail(2),
            _drive([_file("a.txt")]),
            expect_cal=None,
            expect_mail=2,
            expect_drive=1,
        )
        self.assertIsNone(result["modules"]["calendar"]["got"])
        self.assertEqual(result["modules"]["calendar"]["tone"], "skip")
        self.assertEqual(result["overall"], "ok")


class VerifyCountingTests(unittest.TestCase):
    def test_github_zip_is_not_counted_as_persona_content(self):
        result = _run(
            _calendar(1),
            _gmail(1),
            _drive([_file("notes.txt"), _file("github-repo-snapshot.zip")]),
            expect_cal=1,
            expect_mail=1,
            expect_drive=1,
        )
        self.assertEqual(result["modules"]["drive"]["got"], 1)
        self.assertEqual(result["modules"]["drive"]["tone"], "ok")

    def test_github_folder_is_not_counted_as_persona_content(self):
        result = _run(
            _calendar(1),
            _gmail(1),
            FakeDrive(
                [
                    {
                        "files": [
                            _file("notes.txt"),
                            {"id": "gh", "name": "Github", "mimeType": "application/vnd.google-apps.folder"},
                        ]
                    },
                    {"files": [_file("repo.py")]},
                ]
            ),
            expect_cal=1,
            expect_mail=1,
            expect_drive=1,
        )
        self.assertEqual(result["modules"]["drive"]["got"], 1)
        self.assertEqual(result["modules"]["drive"]["tone"], "ok")

    def test_drive_walks_into_subfolders(self):
        nested = FakeDrive(
            [
                {
                    "files": [
                        _file("top.txt"),
                        {"id": "sub", "name": "sub", "mimeType": "application/vnd.google-apps.folder"},
                    ]
                },
                {"files": [_file("inner.txt")]},
            ]
        )
        result = _run(_calendar(1), _gmail(1), nested, expect_cal=1, expect_mail=1, expect_drive=2)
        self.assertEqual(result["modules"]["drive"]["got"], 2)

    def test_calendar_follows_page_tokens(self):
        paged = FakeCalendar(
            [
                {"items": [{"id": "a"}, {"id": "b"}], "nextPageToken": "p2"},
                {"items": [{"id": "c"}]},
            ]
        )
        result = _run(paged, _gmail(1), _drive([]), expect_cal=3, expect_mail=1, expect_drive=0)
        self.assertEqual(result["modules"]["calendar"]["got"], 3)
        self.assertEqual(result["modules"]["calendar"]["tone"], "ok")

    def test_missing_label_means_zero_not_crash(self):
        gmail = FakeGmail([{"id": "x", "name": "SOMETHING-ELSE"}], [{"messages": []}])
        result = _run(_calendar(1), gmail, _drive([]), expect_cal=1, expect_mail=5, expect_drive=0)
        self.assertEqual(result["modules"]["gmail"]["got"], 0)
        self.assertEqual(result["modules"]["gmail"]["tone"], "err")

    def test_ineligible_drive_files_are_reported(self):
        result = _run(
            _calendar(1),
            _gmail(1),
            _drive([_file("a.txt")]),
            expect_cal=1,
            expect_mail=1,
            expect_drive=1,
            ineligible=3,
        )
        self.assertEqual(result["modules"]["drive"]["ineligible"], 3)
        self.assertEqual(result["modules"]["drive"]["tone"], "ok")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import materialize.provision.executors as ex_mod
from materialize.provision.executors import JobExecutor
from materialize.provision.store import Job, JobStore

RAW = b"hello world"
MD5 = hashlib.md5(RAW).hexdigest()


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _Files:
    def __init__(self, drive):
        self.d = drive

    def list(self, **kw):
        return _Req({"files": self.d.files_list, "nextPageToken": None})

    def update(self, fileId=None, body=None, media_body=None, addParents=None, removeParents=None, fields=None):
        self.d.updates.append(
            {"fileId": fileId, "body": body, "media": media_body is not None,
             "addParents": addParents, "removeParents": removeParents}
        )
        return _Req({"id": fileId})

    def create(self, **kw):
        self.d.creates.append(kw)
        return _Req({"id": "NEWID"})


class FakeDrive:
    def __init__(self, files_list):
        self.files_list = files_list
        self.updates: list = []
        self.creates: list = []

    def files(self):
        return _Files(self)


def _file_entry(fid, name, parents, md5=MD5, size=len(RAW), folder=False):
    return {
        "id": fid, "name": name, "parents": parents, "size": str(size),
        "md5Checksum": "" if folder else md5,
        "mimeType": "application/vnd.google-apps.folder" if folder else "text/plain",
    }


class DriveReconcileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tmp.name) / "jobs.sqlite")
        self._orig_build = ex_mod.build_service
        self._orig_read = ex_mod.read_cached_bytes
        ex_mod.read_cached_bytes = lambda env, rel: RAW

    def tearDown(self):
        ex_mod.build_service = self._orig_build
        ex_mod.read_cached_bytes = self._orig_read
        self.store.close()
        self.tmp.cleanup()

    def _exec(self, drive):
        ex_mod.build_service = lambda name, ver, creds: drive
        return JobExecutor(
            creds_for=lambda e: object(),
            builder=None,
            store=self.store,
            log=lambda _m: None,
            attachment_index=lambda env: {},
        )

    def _job(self, gid, parent="folderA", name="report.txt"):
        return Job(
            job_id="j1", account_id="a@ex.com", persona_id="P", environment_id="P",
            service="drive", action="upload", synthetic_id="s1", source_type="generated",
            google_object_id=gid,
            payload={"filename": name, "parent": parent, "rel": "docs/report.txt", "mime": "text/plain"},
            extra={"mode": "delta"},
        )

    def test_renamed_file_is_patched_not_duplicated(self):
        drive = FakeDrive([_file_entry("F1", "agent-renamed.txt", ["folderA"])])  # same content, new name
        res = self._exec(drive)._drive(self._job("F1"), object())
        self.assertEqual(res["id"], "F1")
        self.assertEqual(len(drive.creates), 0)  # no duplicate
        self.assertEqual(len(drive.updates), 1)
        self.assertEqual(drive.updates[0]["body"], {"name": "report.txt"})  # renamed back
        self.assertFalse(drive.updates[0]["media"])  # content unchanged

    def test_moved_file_is_patched_back(self):
        drive = FakeDrive([_file_entry("F1", "report.txt", ["WRONGFOLDER"])])  # moved
        self._exec(drive)._drive(self._job("F1"), object())
        self.assertEqual(len(drive.creates), 0)
        self.assertEqual(drive.updates[0]["addParents"], "folderA")
        self.assertEqual(drive.updates[0]["removeParents"], "WRONGFOLDER")

    def test_content_edit_replaced_in_place(self):
        drive = FakeDrive([_file_entry("F1", "report.txt", ["folderA"], md5="deadbeef")])  # edited bytes
        self._exec(drive)._drive(self._job("F1"), object())
        self.assertEqual(len(drive.creates), 0)
        self.assertTrue(any(u["media"] for u in drive.updates))  # content replaced in place

    def test_deleted_file_is_restored(self):
        drive = FakeDrive([])  # id F1 gone, nothing at the location
        res = self._exec(drive)._drive(self._job("F1"), object())
        self.assertEqual(res["id"], "NEWID")  # re-uploaded
        self.assertEqual(len(drive.creates), 1)

    def test_unchanged_file_is_skipped(self):
        drive = FakeDrive([_file_entry("F1", "report.txt", ["folderA"])])  # identical
        res = self._exec(drive)._drive(self._job("F1"), object())
        self.assertTrue(res.get("skipped"))
        self.assertEqual(len(drive.updates), 0)
        self.assertEqual(len(drive.creates), 0)

    def test_native_md5less_file_is_restored_in_place(self):
        # Agent converted the seeded binary to a Google-native type -> live md5 is empty,
        # so content can't be verified. Must restore the seeded bytes in place (same id),
        # not skip it and leave a drifted environment.
        drive = FakeDrive([_file_entry("F1", "report.txt", ["folderA"], md5="")])
        res = self._exec(drive)._drive(self._job("F1"), object())
        self.assertEqual(res["id"], "F1")
        self.assertEqual(len(drive.creates), 0)  # no duplicate
        self.assertTrue(any(u["media"] for u in drive.updates))  # bytes restored in place
        self.assertTrue(res.get("updated"))


if __name__ == "__main__":
    unittest.main()

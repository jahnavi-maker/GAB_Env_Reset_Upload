from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from materialize.fs_cache import (
    DRIVE_CACHE_ROOT,
    ensure_persona_drive_cache,
    file_index_from_cache,
    materialize_persona_filesystem,
)


class FsCacheTests(unittest.TestCase):
    def test_materialize_and_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            persona = "Test_persona_cache"
            svc = Path(tmp) / persona / "services" / "filesystem"
            svc.mkdir(parents=True)
            payload = {
                "files": [
                    {
                        "path": "docs/readme.txt",
                        "filename": "readme.txt",
                        "content": "aGVsbG8=",
                        "encoding": "base64",
                    }
                ]
            }
            src = svc / "data.json"
            src.write_text(json.dumps(payload), encoding="utf-8")
            old_root = DRIVE_CACHE_ROOT
            try:
                import materialize.fs_cache as mod

                mod.DRIVE_CACHE_ROOT = Path(tmp) / "cache"
                out = materialize_persona_filesystem(persona, print, source_path=src)
                self.assertIsNotNone(out)
                self.assertTrue((out / "files" / "docs" / "readme.txt").read_bytes() == b"hello")
                again = ensure_persona_drive_cache(persona, print, source_path=src)
                self.assertEqual(again, out)
                index = file_index_from_cache(persona, {"readme.txt"})
                self.assertEqual(index.get("readme.txt"), b"hello")
            finally:
                mod.DRIVE_CACHE_ROOT = old_root


if __name__ == "__main__":
    unittest.main()

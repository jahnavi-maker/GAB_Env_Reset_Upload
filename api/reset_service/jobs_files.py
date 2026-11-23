"""Where a job's screenshot lives: the picture of what the worker's browser showed when a job needed a person.

One small interface (put/get by job id) so the storage can change without touching the routes. LocalFileStore writes to a folder on this
machine (set JOB_FILES_DIR to a persistent path in production). Swap in S3 or Supabase Storage later by implementing the same two methods.
The newest upload for a job replaces the older one; the file name is built from the job id only, never from anything the caller sends.
"""
from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path
from typing import Optional

_JOB_ID = re.compile(r"^[0-9a-fA-F-]{36}$")
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class FileStore:
    max_bytes: int

    def put(self, job_id: str, data: bytes) -> None:
        raise NotImplementedError

    def get(self, job_id: str) -> Optional[bytes]:
        raise NotImplementedError


class LocalFileStore(FileStore):
    def __init__(self, root: str, max_bytes: int = 2_000_000) -> None:
        self.root = Path(root)
        self.max_bytes = max_bytes

    def _path(self, job_id: str) -> Optional[Path]:
        return self.root / f"{job_id}.png" if _JOB_ID.match(job_id or "") else None

    def put(self, job_id: str, data: bytes) -> None:
        path = self._path(job_id)
        if path is None:
            raise ValueError("not a job id")
        self.root.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.root, suffix=".tmp")   # write whole, then rename: a reader never sees half a file
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    def get(self, job_id: str) -> Optional[bytes]:
        path = self._path(job_id)
        if path is None or not path.is_file():
            return None
        return path.read_bytes()

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

JOB_HEADERS = (
    "Job ID",
    "Requested At",
    "Requester Email",
    "Account ID",
    "Account Email",
    "Persona",
    "Mode",
    "Status",
    "Phase",
    "Progress",
    "Detail",
    "Started At",
    "Completed At",
    "Updated At",
    "Result JSON",
    "Error",
    "Client Nonce",
    "Worker ID",
)
OPERATOR_HEADERS = ("Email", "Name", "Active", "Role")
WORKER_HEADERS = (
    "Worker ID",
    "Heartbeat At",
    "State",
    "Current Job ID",
    "Detail",
    "Host",
    "Version",
)
JOBS_SHEET = "Reset Jobs [DO NOT EDIT]"
OPERATORS_SHEET = "Operators"
WORKER_SHEET = "Worker Status"


class QueueError(RuntimeError):
    pass


@dataclass
class ResetJob:
    row_number: int
    values: dict[str, Any]

    def __getitem__(self, key: str) -> Any:
        return self.values[key]


class GwsSheetsClient:
    def __init__(
        self,
        *,
        spreadsheet_id: str,
        gws_bin: str | Path = "~/bin/gws",
        config_dir: str | Path = "~/.config/gws-deccan-backup",
        attempts: int = 4,
    ) -> None:
        self.spreadsheet_id = spreadsheet_id
        self.gws_bin = str(Path(gws_bin).expanduser())
        self.config_dir = str(Path(config_dir).expanduser())
        self.attempts = attempts

    def _call(self, args: list[str]) -> dict[str, Any]:
        env = os.environ.copy()
        env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = self.config_dir
        env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
        lock_digest = hashlib.sha256(self.config_dir.encode("utf-8")).hexdigest()[:16]
        lock_path = Path(tempfile.gettempdir()) / f"gab-gws-cli-{lock_digest}.lock"
        with lock_path.open("a+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                last_error = ""
                for attempt in range(1, self.attempts + 1):
                    proc = subprocess.run(
                        [self.gws_bin, *args],
                        text=True,
                        capture_output=True,
                        env=env,
                        timeout=120,
                    )
                    if proc.returncode == 0:
                        try:
                            return json.loads(proc.stdout or "{}")
                        except json.JSONDecodeError as exc:
                            raise QueueError(f"gws returned malformed JSON: {exc}") from exc
                    last_error = (proc.stderr or proc.stdout or "gws request failed").strip()
                    if attempt < self.attempts:
                        time.sleep(min(8, 2 ** (attempt - 1)))
                raise QueueError(last_error[-2000:])
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def get_values(self, range_name: str) -> list[list[Any]]:
        result = self._call(
            [
                "sheets",
                "spreadsheets",
                "values",
                "get",
                "--params",
                json.dumps(
                    {
                        "spreadsheetId": self.spreadsheet_id,
                        "range": range_name,
                        "valueRenderOption": "UNFORMATTED_VALUE",
                    }
                ),
            ]
        )
        return result.get("values", [])

    def update_values(self, range_name: str, values: list[list[Any]]) -> dict[str, Any]:
        return self._call(
            [
                "sheets",
                "spreadsheets",
                "values",
                "update",
                "--params",
                json.dumps(
                    {
                        "spreadsheetId": self.spreadsheet_id,
                        "range": range_name,
                        "valueInputOption": "RAW",
                    }
                ),
                "--json",
                json.dumps({"range": range_name, "majorDimension": "ROWS", "values": values}),
            ]
        )


def _range(sheet: str, cells: str) -> str:
    return f"'{sheet}'!{cells}"


def _clean(value: Any, limit: int = 45000) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


class SheetResetQueue:
    def __init__(self, client: GwsSheetsClient) -> None:
        self.client = client

    def _read_table(self, sheet: str, headers: tuple[str, ...]) -> list[list[Any]]:
        rows = self.client.get_values(_range(sheet, f"A:{column_letter(len(headers))}"))
        if not rows:
            raise QueueError(f"{sheet} is empty")
        actual = tuple(str(value) for value in rows[0])
        if actual != headers:
            raise QueueError(f"{sheet} schema mismatch: {actual!r}")
        return rows

    def list_jobs(self) -> list[ResetJob]:
        rows = self._read_table(JOBS_SHEET, JOB_HEADERS)
        jobs: list[ResetJob] = []
        for row_number, raw in enumerate(rows[1:], start=2):
            row = list(raw) + [""] * (len(JOB_HEADERS) - len(raw))
            values = {header: row[index] for index, header in enumerate(JOB_HEADERS)}
            if _clean(values["Job ID"]):
                jobs.append(ResetJob(row_number=row_number, values=values))
        return jobs

    def active_operators(self) -> set[str]:
        rows = self._read_table(OPERATORS_SHEET, OPERATOR_HEADERS)
        return {
            _clean(row[0]).casefold()
            for row in rows[1:]
            if len(row) >= 3 and str(row[2]).strip().upper() == "TRUE"
        }

    def find_job(self, job_id: str) -> ResetJob:
        for job in self.list_jobs():
            if _clean(job["Job ID"]) == job_id:
                return job
        raise QueueError(f"job not found: {job_id}")

    def update_job(self, job_id: str, **changes: Any) -> ResetJob:
        job = self.find_job(job_id)
        unknown = set(changes) - set(JOB_HEADERS)
        if unknown:
            raise QueueError(f"unknown job columns: {sorted(unknown)}")
        updated = dict(job.values)
        updated.update({key: _clean(value) for key, value in changes.items()})
        values = [updated[header] for header in JOB_HEADERS]
        end = column_letter(len(JOB_HEADERS))
        self.client.update_values(_range(JOBS_SHEET, f"A{job.row_number}:{end}{job.row_number}"), [values])
        return ResetJob(row_number=job.row_number, values=updated)

    def recover_stale_running(self, *, now: str, stale_after_seconds: int = 900) -> list[str]:
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        recovered = []
        for job in self.list_jobs():
            if _clean(job["Status"]).upper() != "RUNNING":
                continue
            raw_updated = _clean(job["Updated At"] or job["Started At"])
            try:
                updated = datetime.fromisoformat(raw_updated.replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except ValueError:
                updated = datetime.fromtimestamp(0, tz=timezone.utc)
            if (current - updated).total_seconds() < stale_after_seconds:
                continue
            job_id = _clean(job["Job ID"])
            self.update_job(
                job_id,
                **{
                    "Status": "FAILED",
                    "Phase": "FAILED",
                    "Detail": "The prior worker stopped before reporting completion. Request a fresh full reset.",
                    "Completed At": now,
                    "Updated At": now,
                    "Error": "Worker heartbeat/process ended before the reset completed.",
                },
            )
            recovered.append(job_id)
        return recovered

    def claim_next(
        self,
        *,
        worker_id: str,
        now: str,
        retry_after_seconds: int = 1800,
        max_concurrent: int = 3,
    ) -> ResetJob | None:
        current = datetime.fromisoformat(now.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        jobs = self.list_jobs()
        running = [job for job in jobs if _clean(job["Status"]).upper() == "RUNNING"]
        if len(running) >= max(1, max_concurrent):
            return None
        busy_accounts = {
            _clean(job["Account Email"]).casefold() or _clean(job["Account ID"]).casefold()
            for job in running
        }
        queued = []
        cooling_accounts: set[str] = set()
        for job in jobs:
            status = _clean(job["Status"]).upper()
            if status == "QUEUED":
                queued.append(job)
                continue
            if status != "RETRY_WAIT":
                continue
            try:
                updated = datetime.fromisoformat(_clean(job["Updated At"]).replace("Z", "+00:00"))
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=timezone.utc)
            except ValueError:
                updated = datetime.fromtimestamp(0, tz=timezone.utc)
            if (current - updated).total_seconds() >= retry_after_seconds:
                queued.append(job)
            else:
                cooling_accounts.add(
                    _clean(job["Account Email"]).casefold()
                    or _clean(job["Account ID"]).casefold()
                )
        if not queued:
            return None
        job = next(
            (
                item for item in queued
                if (
                    _clean(item["Account Email"]).casefold()
                    or _clean(item["Account ID"]).casefold()
                )
                not in busy_accounts | cooling_accounts
            ),
            None,
        )
        if job is None:
            return None
        return self.update_job(
            _clean(job["Job ID"]),
            **{
                "Status": "RUNNING",
                "Phase": "AUTHENTICATING",
                "Progress": "3",
                "Detail": "Reset worker claimed this job.",
                "Started At": now,
                "Updated At": now,
                "Worker ID": worker_id,
            },
        )

    def update_worker(
        self,
        *,
        worker_id: str,
        heartbeat_at: str,
        state: str,
        current_job_id: str,
        detail: str,
        host: str,
        version: str,
    ) -> None:
        values = [[worker_id, heartbeat_at, state, current_job_id, _clean(detail, 2000), host, version]]
        self.client.update_values(_range(WORKER_SHEET, "A2:G2"), values)


def column_letter(number: int) -> str:
    if number < 1:
        raise ValueError("column number must be positive")
    result = ""
    value = number
    while value:
        value, remainder = divmod(value - 1, 26)
        result = chr(65 + remainder) + result
    return result


def row_for_headers(headers: Iterable[str], values: dict[str, Any]) -> list[Any]:
    return [values.get(header, "") for header in headers]

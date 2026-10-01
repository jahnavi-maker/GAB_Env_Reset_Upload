from __future__ import annotations

import os
from dataclasses import dataclass

from materialize.runstate import clamp_int


def _float_env(name: str, default: float, lo: float, hi: float) -> float:
    raw = os.environ.get(name)
    try:
        value = float(raw) if raw is not None else default
    except (TypeError, ValueError):
        value = default
    return max(lo, min(hi, value))


@dataclass(frozen=True)
class ProvisionConfig:
    drive_workers: int
    calendar_workers: int
    gmail_workers: int
    generate_workers: int
    checksum_workers: int
    drive_rate: float
    calendar_rate: float
    calendar_account_rate: float
    gmail_rate: float
    generate_rate: float
    drive_burst: float
    calendar_burst: float
    gmail_burst: float
    max_retries: int
    stale_processing_s: float
    max_file_bytes: int
    adaptive: bool


def load_config() -> ProvisionConfig:
    return ProvisionConfig(
        drive_workers=clamp_int(os.environ.get("DRIVE_WORKERS", "10"), 10, 1, 64),
        calendar_workers=clamp_int(os.environ.get("CALENDAR_WORKERS", "5"), 5, 1, 32),
        gmail_workers=clamp_int(os.environ.get("GMAIL_WORKERS", "10"), 10, 1, 64),
        generate_workers=clamp_int(os.environ.get("GENERATE_WORKERS", "2"), 2, 1, 8),
        checksum_workers=clamp_int(os.environ.get("CHECKSUM_WORKERS", "2"), 2, 0, 8),
        drive_rate=_float_env("DRIVE_RATE", 8.0, 0.2, 80.0),
        calendar_rate=_float_env("CALENDAR_RATE", 0.8, 0.05, 20.0),
        calendar_account_rate=_float_env("CALENDAR_ACCOUNT_RATE", 0.8, 0.05, 10.0),
        gmail_rate=_float_env("GMAIL_RATE", 8.0, 0.2, 80.0),
        generate_rate=_float_env("GENERATE_RATE", 20.0, 1.0, 200.0),
        drive_burst=_float_env("DRIVE_BURST", 16.0, 1.0, 80.0),
        calendar_burst=_float_env("CALENDAR_BURST", 2.0, 1.0, 20.0),
        gmail_burst=_float_env("GMAIL_BURST", 16.0, 1.0, 80.0),
        max_retries=clamp_int(os.environ.get("PROVISION_MAX_RETRIES", "8"), 8, 1, 16),
        stale_processing_s=_float_env("PROVISION_STALE_S", 300.0, 5.0, 3600.0),
        max_file_bytes=clamp_int(
            os.environ.get("GAB_MAX_FILE_BYTES", str(40 * 1024 * 1024)),
            40 * 1024 * 1024,
            1024,
            200 * 1024 * 1024,
        ),
        adaptive=os.environ.get("PROVISION_ADAPTIVE", "1").strip().lower() not in ("0", "false", "no"),
    )

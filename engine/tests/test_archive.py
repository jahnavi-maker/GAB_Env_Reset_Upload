from pathlib import Path

import pytest

from gab_seeder.archive import ArchiveError, classify_content, decode_content, parse_datetime, safe_relpath


def test_decode_base64_binary():
    record = {"path": "x.bin", "content": "AAEC/w==", "size": 4}
    assert classify_content(record) == "base64"
    assert decode_content(record) == b"\x00\x01\x02\xff"


def test_decode_plain_text():
    record = {"path": "x.md", "content": "hello\n", "size": 6}
    assert classify_content(record) == "text"
    assert decode_content(record) == b"hello\n"


def test_size_mismatch_fails():
    with pytest.raises(ArchiveError, match="size mismatch"):
        decode_content({"path": "x.txt", "content": "hello", "size": 4})


def test_mixed_timestamp_formats():
    from decimal import Decimal

    assert parse_datetime(0).isoformat() == "1970-01-01T00:00:00+00:00"
    assert parse_datetime(Decimal("0.0")).isoformat() == "1970-01-01T00:00:00+00:00"
    assert parse_datetime("2026-08-24T15:00:00Z").isoformat() == "2026-08-24T15:00:00+00:00"


def test_unsafe_paths_rejected():
    with pytest.raises(ArchiveError):
        safe_relpath("../../secret")

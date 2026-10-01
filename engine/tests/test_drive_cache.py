"""The Drive cache must be fingerprint- and byte-identical to reading the zip.

If it isn't, delta / reseed / reset could misfire, so these tests are the safety
net for the opt-in GAB_DRIVE_CACHE path.
"""
import base64
import json
import zipfile
from pathlib import Path

from gab_seeder import drive as drive_mod
from gab_seeder import drive_cache
from gab_seeder.archive import EnvironmentArchive, decode_content, safe_relpath

BIN = b"\x00\x01\x02 binary \xfe\xff" * 200


def make_archive(path: Path) -> EnvironmentArchive:
    filesystem = {
        "directories": ["Docs"],
        "files": [
            {"path": "Docs/a.txt", "mime_type": "text/plain", "size": 5,
             "content": base64.b64encode(b"hello").decode("ascii")},
            {"path": "Docs/bin.dat", "mime_type": "application/octet-stream",
             "size": len(BIN), "content": base64.b64encode(BIN).decode("ascii")},
            {"path": "notes.md", "size": 5, "content": "plain"},  # text (non-base64)
        ],
    }
    root = "PKJA_UltraEvals_Environments_/Persona/services"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{root}/filesystem/data.json", json.dumps(filesystem))
    return EnvironmentArchive(path)


_MANIFEST = {"seed_tag": "t", "drive": {"files": {}}}


def _fingerprints(archive):
    desired = drive_mod._drive_file_desired(archive=archive, persona="Persona", manifest=_MANIFEST)
    return {p: (d["content_sha"], d["md5Checksum"], d["size"], d["mimeType"])
            for p, d in desired.items()}


def test_cache_fingerprints_match_direct(tmp_path, monkeypatch):
    arc = make_archive(tmp_path / "env.zip")

    monkeypatch.delenv("GAB_DRIVE_CACHE", raising=False)
    direct = _fingerprints(arc)

    monkeypatch.setenv("GAB_DRIVE_CACHE", "1")
    monkeypatch.setenv("GAB_DRIVE_CACHE_ROOT", str(tmp_path / "cache"))
    cached = _fingerprints(arc)

    assert set(direct) == {"Docs/a.txt", "Docs/bin.dat", "notes.md"}
    assert cached == direct  # sha256, md5, size, mimeType identical for every file
    assert (tmp_path / "cache").exists()


def test_cache_bytes_match_direct(tmp_path, monkeypatch):
    arc = make_archive(tmp_path / "env.zip")
    monkeypatch.setenv("GAB_DRIVE_CACHE", "1")
    monkeypatch.setenv("GAB_DRIVE_CACHE_ROOT", str(tmp_path / "cache"))

    drive_cache.ensure(arc, "Persona")
    cached_bytes = {r["path"]: decode_content(r) for r in drive_cache.iter_cached("Persona")}
    direct_bytes = {safe_relpath(str(r["path"])): decode_content(r) for r in arc.iter_files("Persona")}
    assert cached_bytes == direct_bytes


def test_cache_rebuilds_when_archive_changes(tmp_path, monkeypatch):
    monkeypatch.setenv("GAB_DRIVE_CACHE", "1")
    monkeypatch.setenv("GAB_DRIVE_CACHE_ROOT", str(tmp_path / "cache"))
    p = tmp_path / "env.zip"

    arc1 = make_archive(p)
    drive_cache.ensure(arc1, "Persona")
    first = {r["path"] for r in drive_cache.iter_cached("Persona")}
    assert "notes.md" in first

    # Rewrite the archive with a different file set -> fingerprint changes -> rebuild.
    filesystem = {"files": [{"path": "only.txt", "size": 2, "content": base64.b64encode(b"hi").decode("ascii")}]}
    root = "PKJA_UltraEvals_Environments_/Persona/services"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr(f"{root}/filesystem/data.json", json.dumps(filesystem))
    arc2 = EnvironmentArchive(p)
    drive_cache.ensure(arc2, "Persona")
    second = {r["path"] for r in drive_cache.iter_cached("Persona")}
    assert second == {"only.txt"}

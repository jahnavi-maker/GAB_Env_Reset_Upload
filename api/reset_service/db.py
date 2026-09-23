"""Session store: Supabase (PostgREST) with a local-JSON fallback for dev.

Both backends expose the same async interface:

    create(record)                 -> insert a queued session
    update(reset_session_id, dict) -> patch fields (status/timestamps/error)
    get(reset_session_id)          -> fetch one record or None

The record shape is a plain dict matching the ``reset_sessions`` table in
``schema.sql``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx

from .config import settings

log = logging.getLogger("reset_service.db")


class Store:
    """Abstract async session store. SupabaseStore (prod) and LocalJsonStore (dev)
    implement it; see the module docstring for the create/update/get/query contract."""

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def update(self, reset_session_id: str, fields: dict[str, Any]) -> None:
        raise NotImplementedError

    async def get(self, reset_session_id: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class SupabaseStore(Store):
    """Talks to Supabase via its PostgREST endpoint (no SDK dependency)."""

    def __init__(self) -> None:
        base = settings.supabase_url.rstrip("/")
        self._url = f"{base}/rest/v1/{settings.supabase_table}"
        self._client = httpx.AsyncClient(
            timeout=15.0,
            headers={
                "apikey": settings.supabase_key,
                "Authorization": f"Bearer {settings.supabase_key}",
                "Content-Type": "application/json",
            },
        )

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(
            self._url, json=record, headers={"Prefer": "return=representation"}
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else record

    async def update(self, reset_session_id: str, fields: dict[str, Any]) -> None:
        r = await self._client.patch(
            self._url,
            params={"reset_session_id": f"eq.{reset_session_id}"},
            json=fields,
        )
        r.raise_for_status()

    async def get(self, reset_session_id: str) -> Optional[dict[str, Any]]:
        r = await self._client.get(
            self._url,
            params={"reset_session_id": f"eq.{reset_session_id}", "limit": "1"},
        )
        # A malformed id (e.g. not a UUID) makes PostgREST return 400; it can't match
        # any row, so treat it as "not found" (404) rather than a 500.
        if r.status_code == 400:
            return None
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def query(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        base = settings.supabase_url.rstrip("/")
        r = await self._client.get(f"{base}/rest/v1/{table}", params=params)
        r.raise_for_status()
        return r.json()

    async def patch_table(self, table: str, params: dict[str, str], fields: dict[str, Any]) -> None:
        base = settings.supabase_url.rstrip("/")
        r = await self._client.patch(f"{base}/rest/v1/{table}", params=params, json=fields)
        r.raise_for_status()

    async def aclose(self) -> None:
        await self._client.aclose()


class LocalJsonStore(Store):
    """File-backed store used when Supabase env vars are unset (dev only)."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        self._lock = asyncio.Lock()
        if not self._path.exists():
            self._path.write_text("{}", encoding="utf-8")

    def _read(self) -> dict[str, Any]:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, data: dict[str, Any]) -> None:
        self._path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            data = self._read()
            data[record["reset_session_id"]] = record
            self._write(data)
        return record

    async def update(self, reset_session_id: str, fields: dict[str, Any]) -> None:
        async with self._lock:
            data = self._read()
            if reset_session_id in data:
                data[reset_session_id].update(fields)
                self._write(data)

    async def get(self, reset_session_id: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            return self._read().get(reset_session_id)

    async def query(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        # Dev fallback: only session rows live locally; no gab_accounts table.
        if table == settings.supabase_table:
            async with self._lock:
                return list(self._read().values())
        return []

    async def patch_table(self, table: str, params: dict[str, str], fields: dict[str, Any]) -> None:
        # Dev fallback: no gab_accounts locally; no-op.
        return None


def make_store() -> Store:
    if settings.use_supabase:
        log.info("session store: Supabase (%s)", settings.supabase_table)
        return SupabaseStore()
    log.warning(
        "SUPABASE_URL/SUPABASE_KEY not set -> using local JSON store at %s (dev only)",
        settings.local_store_path,
    )
    return LocalJsonStore(settings.local_store_path)

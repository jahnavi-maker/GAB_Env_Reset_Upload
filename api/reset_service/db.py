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
from datetime import datetime, timezone
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

    # --- freelancers allow-list (email-only access check for the reset page) ---
    async def upsert_freelancer(self, email: str, name: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def get_freelancer(self, email: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def list_freelancers(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def delete_freelancer(self, email: str) -> None:
        raise NotImplementedError

    async def upsert_account(
        self, email: str, persona: str, password: str | None = None
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_account(self, email: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def list_accounts(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def upsert_login(self, email: str, name: str | None = None) -> dict[str, Any]:
        raise NotImplementedError

    async def get_login(self, email: str) -> Optional[dict[str, Any]]:
        raise NotImplementedError

    async def list_logins(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def delete_login(self, email: str) -> None:
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


def _norm_email(email: str) -> str:
    return (email or "").strip().lower()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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
        self._fallback = None

    def _local(self) -> "LocalJsonStore":
        if self._fallback is None:
            self._fallback = LocalJsonStore(settings.local_store_path)
        return self._fallback

    @staticmethod
    def _write_denied(r: httpx.Response) -> bool:
        return r.status_code in (401, 403)

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        r = await self._client.post(
            self._url, json=record, headers={"Prefer": "return=representation"}
        )
        if self._write_denied(r):
            log.warning("Supabase insert denied (%s); session stored locally", r.status_code)
            return await self._local().create(record)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else record

    async def update(self, reset_session_id: str, fields: dict[str, Any]) -> None:
        r = await self._client.patch(
            self._url,
            params={"reset_session_id": f"eq.{reset_session_id}"},
            json=fields,
        )
        if self._write_denied(r):
            await self._local().update(reset_session_id, fields)
            return
        r.raise_for_status()
        # Insert may have fallen back to local JSON (publishable key). Always
        # keep that copy current so the status page is not stuck on queued.
        await self._local().update(reset_session_id, fields)

    async def get(self, reset_session_id: str) -> Optional[dict[str, Any]]:
        r = await self._client.get(
            self._url,
            params={"reset_session_id": f"eq.{reset_session_id}", "limit": "1"},
        )
        # A malformed id (e.g. not a UUID) makes PostgREST return 400; it can't match
        # any row, so treat it as "not found" (404) rather than a 500.
        if r.status_code == 400:
            return await self._local().get(reset_session_id)
        r.raise_for_status()
        rows = r.json()
        if rows:
            return rows[0]
        return await self._local().get(reset_session_id)

    async def query(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        base = settings.supabase_url.rstrip("/")
        r = await self._client.get(f"{base}/rest/v1/{table}", params=params)
        r.raise_for_status()
        return r.json()

    async def patch_table(self, table: str, params: dict[str, str], fields: dict[str, Any]) -> None:
        base = settings.supabase_url.rstrip("/")
        r = await self._client.patch(f"{base}/rest/v1/{table}", params=params, json=fields)
        r.raise_for_status()

    def _fl_url(self) -> str:
        return f"{settings.supabase_url.rstrip('/')}/rest/v1/{settings.freelancers_table}"

    async def upsert_freelancer(self, email: str, name: str | None = None) -> dict[str, Any]:
        rec: dict[str, Any] = {"email": _norm_email(email)}
        if name is not None:
            rec["name"] = name
        r = await self._client.post(
            self._fl_url(),
            json=rec,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else rec

    async def get_freelancer(self, email: str) -> Optional[dict[str, Any]]:
        r = await self._client.get(
            self._fl_url(), params={"email": f"eq.{_norm_email(email)}", "limit": "1"}
        )
        if r.status_code == 400:
            return None
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def list_freelancers(self) -> list[dict[str, Any]]:
        r = await self._client.get(
            self._fl_url(),
            params={"select": "email,name,active,created_at", "order": "created_at.desc"},
        )
        r.raise_for_status()
        return r.json()

    async def delete_freelancer(self, email: str) -> None:
        r = await self._client.delete(self._fl_url(), params={"email": f"eq.{_norm_email(email)}"})
        r.raise_for_status()

    def _acct_url(self) -> str:
        return f"{settings.supabase_url.rstrip('/')}/rest/v1/{settings.accounts_table}"

    async def upsert_account(
        self, email: str, persona: str, password: str | None = None
    ) -> dict[str, Any]:
        email = _norm_email(email)
        existing = await self.get_account(email)
        rec: dict[str, Any] = {
            "email": email,
            "persona": persona,
            "status": "active",
        }
        if password is not None:
            rec["password"] = password
        # last_reset_persona is set only after a successful seed/reset, not at register.
        r = await self._client.post(
            self._acct_url(),
            json=rec,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
            params={"on_conflict": "email"},
        )
        if self._write_denied(r):
            log.warning("Supabase gab_accounts write denied (%s); keeping local copy", r.status_code)
            return await self._local().upsert_account(email, persona, password)
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else rec

    async def get_account(self, email: str) -> Optional[dict[str, Any]]:
        r = await self._client.get(
            self._acct_url(),
            params={"email": f"eq.{_norm_email(email)}", "limit": "1"},
        )
        if r.status_code == 400:
            return None
        r.raise_for_status()
        rows = r.json()
        return rows[0] if rows else None

    async def list_accounts(self) -> list[dict[str, Any]]:
        r = await self._client.get(
            self._acct_url(),
            params={
                "select": "email,persona,last_reset_persona,authorized,status,last_reset_at,created_at",
                "order": "created_at.desc",
            },
        )
        r.raise_for_status()
        return r.json()

    # Login allow-list is the existing freelancers table (same as /ui/freelancer/verify).
    async def upsert_login(self, email: str, name: str | None = None) -> dict[str, Any]:
        return await self.upsert_freelancer(email, name)

    async def get_login(self, email: str) -> Optional[dict[str, Any]]:
        return await self.get_freelancer(email)

    async def list_logins(self) -> list[dict[str, Any]]:
        return await self.list_freelancers()

    async def delete_login(self, email: str) -> None:
        await self.delete_freelancer(email)

    async def aclose(self) -> None:
        await self._client.aclose()


class SplitLoginStore(Store):
    """Accounts/sessions/freelancers on ``primary``; login emails on ``logins``.

    Used while login allow-list is still local JSON and the rest of the app
    already talks to the existing Supabase project.
    """

    def __init__(self, primary: Store, logins: Store) -> None:
        self._primary = primary
        self._logins = logins

    async def create(self, record: dict[str, Any]) -> dict[str, Any]:
        return await self._primary.create(record)

    async def update(self, reset_session_id: str, fields: dict[str, Any]) -> None:
        await self._primary.update(reset_session_id, fields)

    async def get(self, reset_session_id: str) -> Optional[dict[str, Any]]:
        return await self._primary.get(reset_session_id)

    async def query(self, table: str, params: dict[str, str]) -> list[dict[str, Any]]:
        return await self._primary.query(table, params)

    async def patch_table(self, table: str, params: dict[str, str], fields: dict[str, Any]) -> None:
        await self._primary.patch_table(table, params, fields)

    async def upsert_freelancer(self, email: str, name: str | None = None) -> dict[str, Any]:
        return await self._logins.upsert_freelancer(email, name)

    async def get_freelancer(self, email: str) -> Optional[dict[str, Any]]:
        return await self._logins.get_freelancer(email)

    async def list_freelancers(self) -> list[dict[str, Any]]:
        return await self._logins.list_freelancers()

    async def delete_freelancer(self, email: str) -> None:
        await self._logins.delete_freelancer(email)

    async def upsert_account(
        self, email: str, persona: str, password: str | None = None
    ) -> dict[str, Any]:
        return await self._primary.upsert_account(email, persona, password)

    async def get_account(self, email: str) -> Optional[dict[str, Any]]:
        return await self._primary.get_account(email)

    async def list_accounts(self) -> list[dict[str, Any]]:
        return await self._primary.list_accounts()

    async def upsert_login(self, email: str, name: str | None = None) -> dict[str, Any]:
        return await self._logins.upsert_login(email, name)

    async def get_login(self, email: str) -> Optional[dict[str, Any]]:
        return await self._logins.get_login(email)

    async def list_logins(self) -> list[dict[str, Any]]:
        return await self._logins.list_logins()

    async def delete_login(self, email: str) -> None:
        await self._logins.delete_login(email)

    async def aclose(self) -> None:
        await self._logins.aclose()
        await self._primary.aclose()


class LocalJsonStore(Store):
    """File-backed store used when Supabase env vars are unset (dev only)."""

    def __init__(self, path: str) -> None:
        self._path = Path(path)
        # Freelancers live in a sibling file so they don't mix with session rows.
        self._fl_path = self._path.with_name(self._path.stem + ".freelancers.json")
        self._acct_path = self._path.with_name(self._path.stem + ".accounts.json")
        self._login_path = self._path.with_name(self._path.stem + ".logins.json")
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
        if table == settings.supabase_table:
            async with self._lock:
                return list(self._read().values())
        if table == settings.accounts_table:
            async with self._lock:
                rows = list(self._read_acct().values())
            want = params.get("email") or ""
            if want.startswith("eq."):
                want = want[3:].strip().lower()
                rows = [r for r in rows if r.get("email") == want]
            return rows
        return []

    async def patch_table(self, table: str, params: dict[str, str], fields: dict[str, Any]) -> None:
        if table != settings.accounts_table:
            return None
        want = params.get("email") or ""
        if want.startswith("eq."):
            want = want[3:].strip().lower()
        async with self._lock:
            data = self._read_acct()
            if want in data:
                data[want].update(fields)
                self._write_acct(data)
        return None

    def _read_fl(self) -> dict[str, Any]:
        try:
            return json.loads(self._fl_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_fl(self, data: dict[str, Any]) -> None:
        self._fl_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    async def upsert_freelancer(self, email: str, name: str | None = None) -> dict[str, Any]:
        async with self._lock:
            data = self._read_fl()
            key = _norm_email(email)
            rec = data.get(key) or {"email": key, "active": True, "created_at": _now_iso()}
            if name is not None:
                rec["name"] = name
            rec.setdefault("active", True)
            data[key] = rec
            self._write_fl(data)
        return rec

    async def get_freelancer(self, email: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            return self._read_fl().get(_norm_email(email))

    async def list_freelancers(self) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._read_fl().values())

    async def delete_freelancer(self, email: str) -> None:
        async with self._lock:
            data = self._read_fl()
            if data.pop(_norm_email(email), None) is not None:
                self._write_fl(data)

    def _read_acct(self) -> dict[str, Any]:
        try:
            return json.loads(self._acct_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_acct(self, data: dict[str, Any]) -> None:
        self._acct_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    async def upsert_account(
        self, email: str, persona: str, password: str | None = None
    ) -> dict[str, Any]:
        async with self._lock:
            data = self._read_acct()
            key = _norm_email(email)
            rec = data.get(key) or {
                "email": key,
                "authorized": False,
                "status": "active",
                "created_at": _now_iso(),
            }
            rec["persona"] = persona
            if password is not None:
                rec["password"] = password
            # last_reset_persona stays empty until a real seed/reset completes.
            rec["updated_at"] = _now_iso()
            data[key] = rec
            self._write_acct(data)
        return rec

    async def get_account(self, email: str) -> Optional[dict[str, Any]]:
        async with self._lock:
            return self._read_acct().get(_norm_email(email))

    async def list_accounts(self) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._read_acct().values())

    def _read_logins(self) -> dict[str, Any]:
        try:
            return json.loads(self._login_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_logins(self, data: dict[str, Any]) -> None:
        self._login_path.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")

    async def upsert_login(self, email: str, name: str | None = None) -> dict[str, Any]:
        return await self.upsert_freelancer(email, name)

    async def get_login(self, email: str) -> Optional[dict[str, Any]]:
        return await self.get_freelancer(email)

    async def list_logins(self) -> list[dict[str, Any]]:
        return await self.list_freelancers()

    async def delete_login(self, email: str) -> None:
        await self.delete_freelancer(email)


def _login_json_path(store: LocalJsonStore) -> str:
    return str(store._login_path)


def make_store() -> Store:
    if settings.use_supabase:
        log.info(
            "session store: Supabase (%s); login store: %s",
            settings.supabase_table,
            settings.freelancers_table,
        )
        return SupabaseStore()
    log.warning(
        "SUPABASE_URL/SUPABASE_KEY not set -> using local JSON store at %s (dev only)",
        settings.local_store_path,
    )
    return LocalJsonStore(settings.local_store_path)

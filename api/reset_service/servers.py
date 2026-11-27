"""Several GAB servers sharing one Supabase project.

Each server has a name (GAB_SERVER_ID, e.g. "A" or "B"). Every reset_sessions row it creates carries that name in
``server_id``, and the start-up auto-resume and the stuck-session reaper only touch rows that belong to it. Without
this, restarting one server would re-launch (or mark failed) the other server's running seeds and resets.

The one-operation-per-account rule stays global on purpose: the database's partial unique index still stops two
servers from working on the same account at the same time.

With GAB_SERVER_ID unset everything behaves exactly as before (no stamp, every row is this server's).
"""
from __future__ import annotations

import re
from typing import Any

_NAME = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


def check_name(server_id: str) -> str:
    """The server name goes into a PostgREST filter, so only plain names are allowed."""
    if server_id and not _NAME.match(server_id):
        raise ValueError(f"GAB_SERVER_ID must be 1-32 letters, digits, '-' or '_' (got {server_id!r})")
    return server_id


def owner_filter(settings: Any) -> dict[str, str]:
    """PostgREST params that limit a reset_sessions query/patch to the rows this server owns."""
    sid = check_name(getattr(settings, "server_id", "") or "")
    if not sid:
        return {}  # single-server mode: every row is ours
    if getattr(settings, "server_claims_unowned", False):
        return {"or": f"(server_id.eq.{sid},server_id.is.null)"}
    return {"server_id": f"eq.{sid}"}


def stamp(record: dict[str, Any], settings: Any) -> dict[str, Any]:
    """A copy of a new reset_sessions row with this server's name added (unchanged when no name is set)."""
    sid = check_name(getattr(settings, "server_id", "") or "")
    if not sid or record.get("server_id"):
        return record
    return {**record, "server_id": sid}

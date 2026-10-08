"""Client IP allow-list for the platform reset APIs + helpers for the audit log.

The platform (Bearer) endpoints under /api/environment/* and /api/reset-link may
only be called from whitelisted client servers (cosmo + configurable test IPs).
The freelancer-facing reset page is NOT IP-gated (it is opened from the freelancer's
own browser); it is protected by the short-lived, single-use, signed reset token.
"""
from __future__ import annotations

import ipaddress
import socket
import time
from typing import Any, Iterable

from .config import settings

# Platform API paths that require a whitelisted client IP (when enforcement is on).
PROTECTED_PREFIXES = ("/api/environment/", "/api/reset-link")
# Reset-flow paths whose every request is audit-logged (platform + freelancer).
AUDITED_PREFIXES = ("/api/environment/", "/api/reset-link", "/ui/task", "/reset")


def is_protected(path: str) -> bool:
    return any(path.startswith(p) for p in PROTECTED_PREFIXES)


def is_audited(path: str) -> bool:
    return any(path.startswith(p) for p in AUDITED_PREFIXES)


def client_ip(request) -> str:
    """Real client IP. Behind nginx+ALB the socket peer is the proxy, so prefer the
    left-most X-Forwarded-For hop (the original client) when proxies are trusted."""
    if settings.trust_forwarded_for:
        xff = request.headers.get("x-forwarded-for")
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
    client = getattr(request, "client", None)
    return client.host if client else ""


def _networks(values: Iterable[str]):
    nets = []
    for v in values:
        v = (v or "").strip()
        if not v:
            continue
        try:
            nets.append(ipaddress.ip_network(v, strict=False))
        except ValueError:
            continue
    return nets


def _is_ip_or_cidr(v: str) -> bool:
    try:
        ipaddress.ip_network((v or "").strip(), strict=False)
        return True
    except ValueError:
        return False


_host_cache: dict[str, tuple[float, set[str]]] = {}
_HOST_TTL_S = 300.0


def _resolve_host(host: str) -> set[str]:
    """Resolve a whitelist hostname (e.g. cosmo.deccanexperts.ai) to its IPs, cached.

    Note: a server's *egress* IP can differ from its DNS A record (NAT/LB). If cosmo
    calls from a different source IP, add that exact IP (visible in the audit log) too.
    """
    now = time.monotonic()
    hit = _host_cache.get(host)
    if hit and now - hit[0] < _HOST_TTL_S:
        return hit[1]
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(host, None):
            ips.add(info[4][0])
    except OSError:
        pass
    _host_cache[host] = (now, ips)
    return ips


def ip_allowed(ip: str, whitelist_cidrs: Iterable[str], static_cidrs: Iterable[str]) -> bool:
    """True if ip falls inside any static/DB-whitelisted address, CIDR, or hostname."""
    ip = (ip or "").strip()
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    entries = [e for e in (list(static_cidrs) + list(whitelist_cidrs)) if (e or "").strip()]
    for net in _networks(entries):
        if addr.version == net.version and addr in net:
            return True
    for e in entries:  # hostname entries -> resolve and match
        if not _is_ip_or_cidr(e) and ip in _resolve_host(e.strip()):
            return True
    return False


def static_allow() -> list[str]:
    return [s for s in settings.client_whitelist_allow.split(",") if s.strip()]


# Short cache so the whitelist isn't fetched from Supabase on every request.
_cache: dict[str, Any] = {"at": 0.0, "cidrs": []}
_CACHE_TTL_S = 30.0


async def allowed_cidrs(store) -> list[str]:
    now = time.monotonic()
    if now - _cache["at"] < _CACHE_TTL_S and _cache["at"] > 0:
        return _cache["cidrs"]
    try:
        rows = await store.list_whitelist()
        cidrs = [str(r.get("cidr")) for r in rows if r.get("cidr") and r.get("active", True)]
        _cache["cidrs"] = cidrs
        _cache["at"] = now
    except Exception:
        # Keep the last good list on a transient lookup error (fail-safe, not fail-open-fresh).
        pass
    return _cache["cidrs"]


def session_id_from_path(path: str) -> str | None:
    """Pull a reset_session_id out of a path like /api/environment/reset/<id>."""
    for marker in ("/api/environment/reset/", "/reset/status/", "/ui/reset/"):
        if marker in path:
            tail = path.split(marker, 1)[1].strip("/")
            return tail.split("/")[0] or None
    return None

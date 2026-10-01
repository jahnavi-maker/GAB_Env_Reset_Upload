from __future__ import annotations

import base64
import hashlib
import re
from typing import Any

_B64_RE = re.compile(r"^[A-Za-z0-9+/=\s]+$")


def decode_seed_bytes(item: dict[str, Any]) -> bytes:
    content = item.get("content") or ""
    if isinstance(content, bytes):
        raw = content
    else:
        text = str(content)
        labeled = str(item.get("encoding") or "").lower() == "base64"
        looks = bool(text) and bool(_B64_RE.match(text)) and " " not in text.strip()[:80]
        raw = None
        if labeled or looks:
            try:
                raw = base64.b64decode(text, validate=False)
            except (ValueError, TypeError):
                raw = None
        if raw is None:
            raw = text.encode("utf-8")

    expected = str(item.get("sha256") or "").strip().lower()
    if expected:
        got = hashlib.sha256(raw).hexdigest()
        if got != expected and isinstance(content, str):
            alt_tries: list[bytes] = []
            try:
                alt_tries.append(base64.b64decode(content, validate=False))
            except (ValueError, TypeError):
                pass
            alt_tries.append(content.encode("utf-8"))
            for alt in alt_tries:
                if hashlib.sha256(alt).hexdigest() == expected:
                    return alt
    return raw

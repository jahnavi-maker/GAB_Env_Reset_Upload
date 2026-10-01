from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

_ADDR = re.compile(r"([A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,})", re.I)


def _swap_addr(value: str, persona: str, target: str) -> str:
    if not value or not persona:
        return value
    pattern = re.compile(re.escape(persona), re.I)
    return pattern.sub(target, value)


def _swap_list(values: Any, persona: str, target: str) -> list[str]:
    if isinstance(values, str):
        values = [values]
    if not isinstance(values, list):
        return []
    out = []
    for item in values:
        if isinstance(item, str):
            out.append(_swap_addr(item, persona, target))
        else:
            out.append(item)
    return out


def persona_mailbox(gmail_data: dict[str, Any] | None) -> str:
    if not gmail_data:
        return ""
    listed = str(gmail_data.get("user_email") or "").strip().lower()
    if listed:
        return listed
    counts: dict[str, int] = {}
    for item in gmail_data.get("emails") or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("folder") or "").upper() == "SENT":
            sender = str(item.get("sender") or "")
            found = _ADDR.search(sender)
            if found:
                addr = found.group(1).lower()
                counts[addr] = counts.get(addr, 0) + 1
    if not counts:
        return ""
    return max(counts, key=counts.get)


def rewrite_identity(
    *,
    gmail_data: dict[str, Any] | None,
    calendar_data: dict[str, Any] | None,
    target_email: str,
    log: Callable[[str], None],
) -> None:
    target = (target_email or "").strip()
    persona = persona_mailbox(gmail_data)
    if not target or not persona or persona == target.lower():
        return
    log(f"Rewriting persona mailbox {persona} → {target}")
    if gmail_data:
        gmail_data["user_email"] = target
        for item in gmail_data.get("emails") or []:
            if not isinstance(item, dict):
                continue
            if item.get("sender"):
                item["sender"] = _swap_addr(str(item["sender"]), persona, target)
            item["recipients"] = _swap_list(item.get("recipients"), persona, target)
            item["cc"] = _swap_list(item.get("cc"), persona, target)
            if item.get("content"):
                item["content"] = _swap_addr(str(item["content"]), persona, target)
    if calendar_data:
        for item in calendar_data.get("events") or []:
            if not isinstance(item, dict):
                continue
            item["attendees"] = _swap_list(item.get("attendees"), persona, target)
            if item.get("description"):
                item["description"] = _swap_addr(str(item["description"]), persona, target)

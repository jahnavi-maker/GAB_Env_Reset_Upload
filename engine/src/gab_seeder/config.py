from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SCOPES = [
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]


class ConfigError(RuntimeError):
    pass


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path).expanduser().resolve()
    data = json.loads(path.read_text(encoding="utf-8"))
    for required in ["archive", "client_secret", "token_dir", "state_dir", "accounts"]:
        if required not in data:
            raise ConfigError(f"missing config key: {required}")
    return data


def account_for_persona(config: dict[str, Any], persona: str) -> dict[str, Any]:
    try:
        account = config["accounts"][persona]
    except KeyError as exc:
        raise ConfigError(f"no account configured for persona {persona!r}") from exc
    if not account.get("email"):
        raise ConfigError(f"persona {persona!r} has no account email")
    return account


def missing_attachment_policy_for_persona(config: dict[str, Any], persona: str) -> str:
    """Return the configured archive-fidelity policy for one persona."""
    policies = config.get("missing_attachment_policies", {})
    return str(policies.get(persona, config.get("missing_attachment_policy", "error")))


def state_path(config: dict[str, Any], account_email: str, persona: str) -> Path:
    safe_email = account_email.replace("@", "_at_").replace("/", "_")
    return Path(config["state_dir"]).expanduser().resolve() / safe_email / f"{persona}.manifest.json"

"""Shared test fixture: a minimal persona ENV_ROOT.

The real persona tree (``PKJA_UltraEvals_Environments_`` / ``GAB_PERSONA_ROOT``) is not
checked into the repo, so persona matching + source resolution can't run against it in a
plain clone. These helpers build a tiny self-contained persona tree in a temp dir and
point ``materialize.runstate.ENV_ROOT`` at it for the duration of a test, so persona-
dependent tests pass deterministically anywhere.
"""
from __future__ import annotations

import json
from pathlib import Path

# Persona folders the persona-dependent tests expect to MATCH (assert persona_dir set).
DEFAULT_PERSONAS = ("Student", "Applied_ML_and_data_scientist", "Backend_software_engineer")

# runstate.persona_file maps gmail -> services/email/data.json (note the dir name), plus
# calendar -> services/calendar/... and filesystem -> services/filesystem/...
_SERVICE_FILES = {
    "calendar": {"events": []},
    "email": {"emails": []},
    "filesystem": {"files": []},
}


def build_env_root(base: Path, personas: tuple[str, ...] = DEFAULT_PERSONAS) -> Path:
    """Create ``<base>/PKJA_UltraEvals_Environments_/<persona>/services/...`` and return it."""
    root = base / "PKJA_UltraEvals_Environments_"
    for name in personas:
        svc = root / name / "services"
        for sub, payload in _SERVICE_FILES.items():
            d = svc / sub
            d.mkdir(parents=True, exist_ok=True)
            (d / "data.json").write_text(json.dumps(payload), encoding="utf-8")
    return root


class EnvRootMixin:
    """unittest mixin: point runstate.ENV_ROOT at a temp persona tree for each test.

    Call ``self._install_env_root(base)`` from setUp and ``self._restore_env_root()`` from
    tearDown. Safe to combine with a class that also patches runstate.RUNS.
    """

    def _install_env_root(self, base: Path, personas: tuple[str, ...] = DEFAULT_PERSONAS) -> None:
        import materialize.runstate as rs

        self._rs_mod = rs
        self._old_env_root = rs.ENV_ROOT
        rs.ENV_ROOT = build_env_root(base, personas)

    def _restore_env_root(self) -> None:
        self._rs_mod.ENV_ROOT = self._old_env_root

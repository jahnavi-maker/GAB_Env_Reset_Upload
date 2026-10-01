import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "reset_environment.py"


def load_script():
    spec = importlib.util.spec_from_file_location("reset_environment_script", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def fixture_config():
    return {
        "accounts": {
            "Student": {
                "email": "test-account-410@example.com",
            }
        }
    }


def test_reset_wrapper_previews_delta_by_default(monkeypatch, tmp_path, capsys):
    module = load_script()
    calls = []
    monkeypatch.setattr(module, "load_config", lambda _path: fixture_config())
    monkeypatch.setattr(
        module,
        "reconcile_persona",
        lambda *args, **kwargs: calls.append(kwargs) or {"dry_run": True},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "test-account-410", "--config", str(tmp_path / "config.json")],
    )

    assert module.main() == 0
    assert calls == [
        {
            "persona": "Student",
            "services": {"gmail", "drive", "calendar"},
            "dry_run": True,
        }
    ]
    assert "--execute" in capsys.readouterr().out


def test_reset_wrapper_executes_delta_without_full_wipe(monkeypatch, tmp_path):
    module = load_script()
    calls = []
    monkeypatch.setattr(module, "load_config", lambda _path: fixture_config())
    monkeypatch.setattr("builtins.input", lambda _prompt: "test-account-410@example.com")
    monkeypatch.setattr(
        module,
        "reconcile_persona",
        lambda *args, **kwargs: calls.append(("delta", kwargs))
        or {"verify": {"ok": True}},
    )
    monkeypatch.setattr(
        module,
        "reset_persona",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("default execute must not wipe")),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [str(SCRIPT), "test-account-410", "--config", str(tmp_path / "config.json"), "--execute"],
    )

    assert module.main() == 0
    assert calls == [
        (
            "delta",
            {
                "persona": "Student",
                "services": {"gmail", "drive", "calendar"},
                "dry_run": False,
            },
        )
    ]


def test_reset_wrapper_full_reset_is_explicit(monkeypatch, tmp_path):
    module = load_script()
    calls = []
    monkeypatch.setattr(module, "load_config", lambda _path: fixture_config())
    monkeypatch.setattr("builtins.input", lambda _prompt: "test-account-410@example.com")
    monkeypatch.setattr(
        module,
        "reconcile_persona",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("full reset must not call delta")),
    )
    monkeypatch.setattr(
        module,
        "reset_persona",
        lambda *args, **kwargs: calls.append(("reset", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "seed_persona",
        lambda *args, **kwargs: calls.append(("seed", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        module,
        "verify_persona",
        lambda *args, **kwargs: calls.append(("verify", kwargs)) or {"ok": True},
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            str(SCRIPT),
            "test-account-410",
            "--config",
            str(tmp_path / "config.json"),
            "--execute",
            "--full-reset",
        ],
    )

    assert module.main() == 0
    assert [name for name, _kwargs in calls] == ["reset", "seed", "verify"]

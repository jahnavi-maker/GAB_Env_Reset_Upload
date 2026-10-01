#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def call_gws(args: list[str], *, config_dir: str, attempts: int = 4) -> dict[str, Any]:
    env = os.environ.copy()
    env["GOOGLE_WORKSPACE_CLI_CONFIG_DIR"] = str(Path(config_dir).expanduser())
    env["GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND"] = "file"
    last = ""
    for attempt in range(1, attempts + 1):
        proc = subprocess.run(
            [str(Path("~/bin/gws").expanduser()), *args],
            text=True,
            capture_output=True,
            env=env,
            timeout=180,
        )
        if proc.returncode == 0:
            return json.loads(proc.stdout or "{}")
        last = (proc.stderr or proc.stdout).strip()
        if attempt < attempts:
            time.sleep(min(8, 2 ** (attempt - 1)))
    raise RuntimeError(last[-3000:])


def local_files() -> list[dict[str, str]]:
    app = ROOT / "apps-script"
    return [
        {"name": "Code", "type": "SERVER_JS", "source": (app / "Code.gs").read_text(encoding="utf-8")},
        {"name": "Index", "type": "HTML", "source": (app / "Index.html").read_text(encoding="utf-8")},
        {"name": "appsscript", "type": "JSON", "source": (app / "appsscript.json").read_text(encoding="utf-8")},
    ]


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Deploy the GAB reset console Apps Script web app.")
    parser.add_argument("--control-config", default=str(ROOT / "reset_control.json"))
    parser.add_argument("--gws-config-dir", default="~/.config/gws-deccan-backup")
    args = parser.parse_args()

    control_path = Path(args.control_config).expanduser().resolve()
    control = json.loads(control_path.read_text(encoding="utf-8"))
    files = local_files()
    script_id = str(control.get("script_id") or "")
    if not script_id:
        project = call_gws(
            [
                "script", "projects", "create", "--json",
                json.dumps({"title": "GAB Environment Reset Console", "parentId": control["spreadsheet_id"]}),
            ],
            config_dir=args.gws_config_dir,
        )
        script_id = str(project["scriptId"])

    call_gws(
        [
            "script", "projects", "updateContent",
            "--params", json.dumps({"scriptId": script_id}),
            "--json", json.dumps({"files": files}),
        ],
        config_dir=args.gws_config_dir,
    )
    version = call_gws(
        [
            "script", "projects", "versions", "create",
            "--params", json.dumps({"scriptId": script_id}),
            "--json", json.dumps({"description": "Domain-restricted reset queue with live worker progress"}),
        ],
        config_dir=args.gws_config_dir,
    )
    version_number = int(version["versionNumber"])
    deployment_id = str(control.get("deployment_id") or "")
    deployment_config = {
        "scriptId": script_id,
        "versionNumber": version_number,
        "manifestFileName": "appsscript",
        "description": "Production — GAB environment reset console",
    }
    if deployment_id:
        deployment = call_gws(
            [
                "script", "projects", "deployments", "update",
                "--params", json.dumps({"scriptId": script_id, "deploymentId": deployment_id}),
                "--json", json.dumps({"deploymentConfig": deployment_config}),
            ],
            config_dir=args.gws_config_dir,
        )
    else:
        deployment = call_gws(
            [
                "script", "projects", "deployments", "create",
                "--params", json.dumps({"scriptId": script_id}),
                "--json", json.dumps(deployment_config),
            ],
            config_dir=args.gws_config_dir,
        )
        deployment_id = str(deployment["deploymentId"])

    remote = call_gws(
        ["script", "projects", "getContent", "--params", json.dumps({"scriptId": script_id})],
        config_dir=args.gws_config_dir,
    )
    remote_sources = {item["name"]: item.get("source", "") for item in remote.get("files", [])}
    expected = {item["name"]: digest(item["source"]) for item in files}
    actual = {name: digest(remote_sources.get(name, "")) for name in expected}
    if expected != actual:
        raise RuntimeError("deployed Apps Script source readback did not match local source")

    deployed = call_gws(
        [
            "script", "projects", "deployments", "get",
            "--params", json.dumps({"scriptId": script_id, "deploymentId": deployment_id}),
        ],
        config_dir=args.gws_config_dir,
    )
    web_url = ""
    for entry in deployed.get("entryPoints", []):
        if entry.get("entryPointType") == "WEB_APP":
            web_url = str((entry.get("webApp") or {}).get("url") or "")
    if not web_url:
        web_url = f"https://script.google.com/a/macros/deccan.ai/s/{deployment_id}/exec"

    control.update(
        {
            "script_id": script_id,
            "deployment_id": deployment_id,
            "version": version_number,
            "web_app_url": web_url,
        }
    )
    control_path.write_text(json.dumps(control, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"script_id": script_id, "deployment_id": deployment_id, "version": version_number, "web_app_url": web_url, "source_verified": True}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

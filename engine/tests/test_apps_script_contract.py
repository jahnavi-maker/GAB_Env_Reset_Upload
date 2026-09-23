import json
from pathlib import Path

from gab_seeder.reset_queue import JOB_HEADERS, WORKER_HEADERS

ROOT = Path(__file__).resolve().parents[1]


def test_apps_script_is_domain_restricted_and_executes_as_owner():
    manifest = json.loads((ROOT / "apps-script" / "appsscript.json").read_text())
    assert manifest["webapp"] == {"access": "DOMAIN", "executeAs": "USER_DEPLOYING"}
    assert "https://www.googleapis.com/auth/spreadsheets" in manifest["oauthScopes"]
    assert "https://www.googleapis.com/auth/userinfo.email" in manifest["oauthScopes"]


def test_apps_script_allows_any_deccan_user_and_requires_explicit_confirmation():
    code = (ROOT / "apps-script" / "Code.gs").read_text()
    assert "function createResetRequest(payload)" in code
    assert "const requester = requireDeccanUser_();" in code
    assert "function requireDeccanUser_()" in code
    assert "email.endsWith('@' + CONFIG.allowedDomain)" in code
    assert "operatorsSheet" not in code
    assert "operator allowlist" not in code
    assert "confirmation !== account.email.toLowerCase()" in code
    assert "'DELTA', 'QUEUED'" in code
    assert "'RETRY_WAIT'" in code
    assert "Opening" not in code
    for header in (*JOB_HEADERS, *WORKER_HEADERS):
        assert f"'{header}'" in code


def test_ui_has_no_link_trigger_and_requires_final_click():
    html = (ROOT / "apps-script" / "Index.html").read_text()
    assert "Restore changed items" in html
    assert "Type the full account email" in html
    assert "createResetRequest" in html
    assert "Inspect and restore only changed Gmail, Drive, and Calendar items" in html
    assert "Different accounts may run concurrently; the same account waits its turn." in html
    assert "Prior-run evidence has been saved." in html
    assert "initial baseline setup" not in html
    assert "Clear Calendar" not in html
    assert "Restore Calendar" in html
    assert "primary-calendar events" not in html
    assert "?reset=" not in html
    assert "window.location" not in html


def test_scheduler_starts_three_portable_worker_slots():
    script = (ROOT / "scripts" / "gab_reset_worker_tick.sh").read_text()
    assert "for _slot in 1 2 3" in script
    assert "--max-concurrent 3" in script
    assert "wait \"$pid\"" in script
    assert "flock" not in script


def test_deployment_metadata_template_has_required_keys():
    control = json.loads((ROOT / "reset_control.example.json").read_text())
    assert set(control) == {"spreadsheet_id", "spreadsheet_url", "script_id", "deployment_id", "web_app_url", "version"}
    assert control["version"] == 0

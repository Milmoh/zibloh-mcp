import json
import os
import stat
from pathlib import Path

import httpx
import pytest
import respx

from zibloh import config, ops
from zibloh.client import ApiError, Client

from robot_fixtures import GOOD_MAIN

BASE = "https://api.test/api/v1"
KEY = "zbl_" + "k" * 40
ROBOT = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def client():
    c = Client(config.Profile(name="t", base_url=BASE, api_key=KEY), client_name="mcp")
    c.tool = "robots_deploy"
    yield c
    c.close()


def _robot_dir(tmp_path):
    (tmp_path / "main.py").write_text(GOOD_MAIN)
    return tmp_path


@respx.mock
def test_requests_carry_audit_headers_and_errors_keep_detail(client):
    route = respx.get(f"{BASE}/self-serve/whoami").mock(return_value=httpx.Response(
        403, json={"detail": "La API Key no tiene los scopes requeridos: ['robots:read']"}, headers={"x-request-id": "req-1"}))
    with pytest.raises(ApiError) as exc:
        ops.whoami(client)
    sent = route.calls.last.request.headers
    assert sent["authorization"] == f"Bearer {KEY}"
    assert sent["x-zibloh-client"] == "mcp" and sent["x-zibloh-tool"] == "robots_deploy"
    assert sent["x-zibloh-session"].startswith("zs-") and sent["x-request-id"].startswith("zc-")
    assert exc.value.status == 403 and "robots:read" in str(exc.value) and "req-1" in str(exc.value)


@respx.mock
def test_deploy_uploads_without_the_api_key_and_reports_the_hash(client, tmp_path):
    respx.get(f"{BASE}/automate/robots/{ROBOT}").mock(return_value=httpx.Response(200, json={
        "robot_id": ROBOT, "name": "R", "execution_tipology": "cloud"}))
    respx.post(f"{BASE}/automate/robots/{ROBOT}/upload-url").mock(return_value=httpx.Response(200, json={
        "upload": {"method": "PUT", "url": "https://s3.test/put?sig=1", "headers": {"Content-Type": "application/zip"}},
        "file": {"file_id": "f1", "file_path": "company/c/robot/r/f1.zip"}}))
    put = respx.put("https://s3.test/put?sig=1").mock(return_value=httpx.Response(200))
    complete = respx.post(f"{BASE}/automate/robots/{ROBOT}/upload/complete").mock(return_value=httpx.Response(200, json={
        "robot_name": "R", "file_hash": "f1"}))

    result = ops.robots_deploy(client, ROBOT, _robot_dir(tmp_path))

    assert "authorization" not in put.calls.last.request.headers
    assert put.calls.last.request.headers["content-type"] == "application/zip"
    body = json.loads(complete.calls.last.request.content)
    assert body["sha256"] == result["sha256"] and body["file_size_bytes"] == result["size"]
    assert result["file_id"] == "f1"


def test_deploy_refuses_invalid_packages(client, tmp_path):
    (tmp_path / "main.py").write_text(GOOD_MAIN)
    (tmp_path / ".env").write_text("X=1")
    with respx.mock:
        respx.get(f"{BASE}/automate/robots/{ROBOT}").mock(return_value=httpx.Response(200, json={"robot_id": ROBOT}))
        with pytest.raises(ops.OpError, match="credentials file"):
            ops.robots_deploy(client, ROBOT, tmp_path)


@respx.mock
def test_target_cloud_unpins_launcher_first(client):
    respx.get(f"{BASE}/launcher/execution-types").mock(return_value=httpx.Response(200, json=[
        {"execution_type_id": "et-cloud", "tipology": "cloud", "name": "Cloud"},
        {"execution_type_id": "et-win", "tipology": "windows", "name": "Windows"}]))
    launcher = respx.patch(f"{BASE}/automate/robots/{ROBOT}/launcher").mock(return_value=httpx.Response(200, json={}))
    et = respx.patch(f"{BASE}/automate/robots/{ROBOT}/execution-type").mock(return_value=httpx.Response(200, json={}))

    ops.robots_target(client, ROBOT, cloud=True)

    assert json.loads(launcher.calls.last.request.content) == {"launcher_id": None}
    assert json.loads(et.calls.last.request.content) == {"execution_type_id": "et-cloud"}
    with pytest.raises(ops.OpError):
        ops.robots_target(client, ROBOT, cloud=True, launcher_id="x")


@respx.mock
def test_options_define_skips_existing_names(client, tmp_path):
    respx.get(f"{BASE}/automate/robots/options/{ROBOT}").mock(return_value=httpx.Response(200, json=[{"name": "timezone"}]))
    create = respx.post(f"{BASE}/automate/robots/options/{ROBOT}").mock(return_value=httpx.Response(201, json={
        "robot_options": [{"name": "test_mode"}]}))
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"opciones": [
        {"name": "timezone", "option_type": "schedule", "data_type": "string", "default": "UTC"},
        {"name": "test_mode", "option_type": "custom", "data_type": "boolean", "default": "true"}]}))

    result = ops.options_define_from_config(client, ROBOT, cfg)

    sent = json.loads(create.calls.last.request.content)["robot_options"]
    assert [o["name"] for o in sent] == ["test_mode"]
    assert result == {"created": ["test_mode"], "skipped_existing": ["timezone"]}


@respx.mock
def test_bind_keeps_other_bindings(client):
    respx.get(f"{BASE}/company-secrets").mock(return_value=httpx.Response(200, json=[
        {"company_secret_id": "s-new", "name": "ERP_PASSWORD", "kind": "env"}]))
    respx.get(f"{BASE}/robots/{ROBOT}/secrets").mock(return_value=httpx.Response(200, json=[
        {"company_secret_id": "s-old", "secret_name": "SMTP", "expose_as": "env", "target_env_name": "SMTP",
         "mount_filename": None, "is_required": True}]))
    put = respx.put(f"{BASE}/robots/{ROBOT}/secrets").mock(return_value=httpx.Response(200, json=[]))

    ops.robot_secrets_bind(client, ROBOT, "ERP_PASSWORD")

    sent = json.loads(put.calls.last.request.content)
    assert [b["company_secret_id"] for b in sent] == ["s-old", "s-new"]
    assert sent[1]["expose_as"] == "env" and sent[1]["target_env_name"] == "ERP_PASSWORD"


@respx.mock
def test_wait_polls_until_terminal_then_reads_the_log(client, monkeypatch):
    monkeypatch.setattr(ops.time, "sleep", lambda _: None)
    statuses = iter(["queued", "running", "success"])
    respx.get(f"{BASE}/robot-execution/executions/e1/detail").mock(side_effect=lambda req: httpx.Response(200, json=(
        lambda s: {"execution_id": "e1", "status": s, "is_terminal": s == "success", "log_message": s})(next(statuses))))
    logs = iter([httpx.Response(404, json={"detail": "Execution artifact not found."}),
                 httpx.Response(200, json={"content": "last line\n", "truncated": False})])
    respx.get(f"{BASE}/robot-execution/executions/e1/logs").mock(side_effect=lambda req: next(logs))

    result = ops.wait_for_execution(client, "e1", tail=10)

    assert result["execution"]["status"] == "success" and not result["timed_out"]
    assert result["log"]["content"] == "last line\n"


def test_credentials_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for var in ("ZIBLOH_API_KEY", "ZIBLOH_BASE_URL", "ZIBLOH_ENV", "ZIBLOH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    config.save_profile("default", KEY, "dev", None)

    mode = stat.S_IMODE(os.stat(config.credentials_path()).st_mode)
    assert mode == 0o600
    profile = config.load_profile()
    assert profile.base_url == config.ENVIRONMENTS["dev"] and profile.api_key == KEY

    monkeypatch.setenv("ZIBLOH_ENV", "prod")
    assert config.load_profile().base_url == config.ENVIRONMENTS["prod"]
    assert "api_key" not in json.dumps(config.list_profiles())

import json

import httpx
import pytest
import respx

from zibloh import browser_login, config

BASE = "https://devapp.zibloh.test/api/v1"


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for var in ("ZIBLOH_API_KEY", "ZIBLOH_BASE_URL", "ZIBLOH_ENV", "ZIBLOH_PROFILE", "ZIBLOH_WEB_URL"):
        monkeypatch.delenv(var, raising=False)


def _device(route):
    route.mock(return_value=httpx.Response(200, json={
        "device_code": "dev-secret", "user_code": "BCDF-GHJK", "verification_path": "/cli-login",
        "expires_in": 600, "interval": 5,
    }))


def test_web_url_is_the_api_origin_unless_overridden(monkeypatch):
    assert browser_login.web_url("https://app.zibloh.com/api/v1") == "https://app.zibloh.com"
    monkeypatch.setenv("ZIBLOH_WEB_URL", "http://localhost:5173/")
    assert browser_login.web_url("http://localhost:8000/api/v1") == "http://localhost:5173"


@respx.mock
def test_start_poll_and_save(monkeypatch):
    device = respx.post(f"{BASE}/cli-auth/device")
    _device(device)
    answers = iter([
        httpx.Response(400, json={"error": "authorization_pending"}),
        httpx.Response(400, json={"error": "slow_down"}),
        httpx.Response(200, json={"api_key": "zbl_new", "api_key_id": "k1", "api_key_name": "CLI · box",
                                  "company_id": "c1", "company_name": "Acme", "scopes": ["robots:*"]}),
    ])
    token = respx.post(f"{BASE}/cli-auth/token").mock(side_effect=lambda request: next(answers))

    pending = browser_login.start(BASE, None, client="mcp")
    assert pending.verification_url == "https://devapp.zibloh.test/cli-login?code=BCDF-GHJK"
    body = json.loads(device.calls[0].request.content)
    assert body["client"] == "mcp" and body["client_name"]

    sleeps = []
    result = browser_login.wait(pending, "default", sleep=sleeps.append)
    assert "api_key" not in result and result["company_name"] == "Acme"
    assert sleeps == [5, 10]  # slow_down adds 5 s
    assert all(json.loads(c.request.content) == {"device_code": "dev-secret"} for c in token.calls)
    # Never sent with an Authorization header.
    assert all("authorization" not in c.request.headers for c in token.calls)

    profile = config.load_profile("default")
    assert profile.api_key == "zbl_new" and profile.base_url == BASE


@respx.mock
@pytest.mark.parametrize("error, message", [("access_denied", "rejected"), ("expired_token", "expired")])
def test_terminal_errors(error, message):
    _device(respx.post(f"{BASE}/cli-auth/device"))
    respx.post(f"{BASE}/cli-auth/token").mock(return_value=httpx.Response(400, json={"error": error}))
    pending = browser_login.start(BASE, None)
    with pytest.raises(browser_login.LoginFailed, match=message):
        browser_login.wait(pending, "default", sleep=lambda _s: None)
    with pytest.raises(config.ConfigError):
        config.load_profile("default")


@respx.mock
def test_mcp_auth_login_runs_in_the_background(monkeypatch):
    from zibloh import mcp_server

    monkeypatch.setattr(config, "ENVIRONMENTS", {**config.ENVIRONMENTS, "dev": BASE})
    monkeypatch.setattr(browser_login, "open_browser", lambda url: False)
    _device(respx.post(f"{BASE}/cli-auth/device"))
    respx.post(f"{BASE}/cli-auth/token").mock(return_value=httpx.Response(200, json={
        "api_key": "zbl_mcp", "api_key_id": "k1", "api_key_name": "Claude Code · box",
        "company_id": "c1", "company_name": "Acme", "scopes": ["robots:*", "mcp:use"]}))

    started = mcp_server.auth_login(env="dev")
    assert started["user_code"] == "BCDF-GHJK" and "dev-secret" not in json.dumps(started)
    status = mcp_server.auth_status(wait_seconds=10)
    assert status["state"] == "logged_in" and status["company_name"] == "Acme"
    assert "api_key" not in status and "device_code" not in status
    assert config.load_profile().api_key == "zbl_mcp"


def test_prompt_hook_blocks_api_keys():
    import subprocess
    from pathlib import Path

    hooks = json.loads((Path(__file__).parents[1] / "hooks" / "hooks.json").read_text())
    command = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]

    def run(prompt):
        return subprocess.run(["bash", "-c", command], input=json.dumps({"prompt": prompt}),
                              capture_output=True, text=True)

    blocked = run("my key is zbl_" + "A1b2C3d4E5" * 4)
    assert blocked.returncode == 2 and "Revoke" in blocked.stderr
    assert run("deploy with zbl_ keys? no").returncode == 0

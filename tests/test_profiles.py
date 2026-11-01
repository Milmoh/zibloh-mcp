"""One person, several companies: one profile per company login, switched without a new login."""
import json

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from zibloh import browser_login, cli, config, mcp_server

BASE = "https://devapp.zibloh.test/api/v1"
SECRET = "_SECRETPART0123456789"  # real keys are long; only their first 12 characters may be shown


@pytest.fixture(autouse=True)
def isolated_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for var in ("ZIBLOH_API_KEY", "ZIBLOH_BASE_URL", "ZIBLOH_ENV", "ZIBLOH_PROFILE", "ZIBLOH_WEB_URL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(config, "ENVIRONMENTS", {**config.ENVIRONMENTS, "dev": BASE})


def _login(company_id, company_name, key, env="dev", profile=None):
    """A full browser login against the mocked backend, approved for one company."""
    with respx.mock:
        respx.post(f"{BASE}/cli-auth/device").mock(return_value=httpx.Response(200, json={
            "device_code": "dc", "user_code": "BCDF-GHJK", "verification_path": "/cli-login",
            "expires_in": 600, "interval": 5}))
        respx.post(f"{BASE}/cli-auth/token").mock(return_value=httpx.Response(200, json={
            "api_key": key + SECRET, "api_key_id": "k", "api_key_name": "CLI · box",
            "company_id": company_id, "company_name": company_name, "scopes": ["robots:*"]}))
        pending = browser_login.start(BASE, env)
        return browser_login.wait(pending, profile, sleep=lambda _s: None)


def test_each_company_gets_its_own_profile_and_the_last_login_is_the_default():
    assert _login("c-acme", "Acme Logística S.L.", "zbl_acme")["profile"] == "acme-logistica-s-l-dev"
    assert _login("c-beta", "Beta", "zbl_beta")["profile"] == "beta-dev"

    profiles = config.list_profiles()
    assert set(profiles["profiles"]) == {"acme-logistica-s-l-dev", "beta-dev"}
    assert profiles["default_profile"] == "beta-dev"
    assert profiles["profiles"]["acme-logistica-s-l-dev"]["company_id"] == "c-acme"
    assert SECRET not in json.dumps(profiles)
    assert config.load_profile().api_key == "zbl_beta" + SECRET
    assert config.load_profile("acme-logistica-s-l-dev").api_key == "zbl_acme" + SECRET


def test_logging_into_the_same_company_again_replaces_its_key():
    _login("c-acme", "Acme", "zbl_old")
    config.save_profile("renamed", "zbl_other", "dev", None, company_id="c-other", company_name="Other")
    assert _login("c-acme", "Acme (renamed in the web)", "zbl_new")["profile"] == "acme-dev"
    assert config.load_profile("acme-dev").api_key == "zbl_new" + SECRET
    assert len(config.list_profiles()["profiles"]) == 2


def test_two_companies_with_the_same_name_do_not_collide():
    _login("c-1111111111", "Acme", "zbl_1")
    assert _login("c-2222222222", "Acme", "zbl_2")["profile"] == "acme-dev-c-222222"
    assert config.load_profile("acme-dev").api_key == "zbl_1" + SECRET


def test_an_explicit_profile_name_still_wins():
    assert _login("c-acme", "Acme", "zbl_acme", profile="work")["profile"] == "work"
    assert config.load_profile("work").api_key == "zbl_acme" + SECRET


def test_use_profile_switches_the_default_and_rejects_unknown_names():
    _login("c-acme", "Acme", "zbl_acme")
    _login("c-beta", "Beta", "zbl_beta")
    config.use_profile("acme-dev")
    assert config.load_profile().api_key == "zbl_acme" + SECRET
    with pytest.raises(config.ConfigError, match="acme-dev, beta-dev"):
        config.use_profile("nope")


def test_legacy_profiles_without_company_still_load(tmp_path):
    config.save_profile("default", "zbl_legacy", "dev", None)
    assert config.load_profile().api_key == "zbl_legacy"
    assert config.list_profiles()["profiles"]["default"]["company_name"] is None


def test_mcp_profiles_tools(monkeypatch):
    _login("c-acme", "Acme", "zbl_acme")
    _login("c-beta", "Beta", "zbl_beta")
    listed = mcp_server.profiles_list()
    assert listed["active_profile"] == "beta-dev" and SECRET not in json.dumps(listed)

    switched = mcp_server.profiles_use("acme-dev")
    assert switched["company_name"] == "Acme" and "warning" not in switched
    assert config.load_profile().api_key == "zbl_acme" + SECRET
    with pytest.raises(ToolError, match="No profile"):
        mcp_server.profiles_use("nope")

    monkeypatch.setenv("ZIBLOH_PROFILE", "beta-dev")
    assert "ZIBLOH_PROFILE" in mcp_server.profiles_use("acme-dev")["warning"]


def test_cli_profiles_list_and_use():
    _login("c-acme", "Acme", "zbl_acme")
    _login("c-beta", "Beta", "zbl_beta")
    runner = CliRunner()

    out = runner.invoke(cli.app, ["profiles", "list"])
    assert out.exit_code == 0 and "* beta-dev" in out.output and SECRET not in out.output

    out = runner.invoke(cli.app, ["profiles", "use", "acme-dev", "--json"])
    assert out.exit_code == 0 and json.loads(out.output)["company_name"] == "Acme"
    assert config.load_profile().api_key == "zbl_acme" + SECRET

    out = runner.invoke(cli.app, ["profiles", "use", "nope"])
    assert out.exit_code == 1

    out = runner.invoke(cli.app, ["logout"])
    assert out.exit_code == 0 and "acme-dev" in out.output
    assert set(config.list_profiles()["profiles"]) == {"beta-dev"}

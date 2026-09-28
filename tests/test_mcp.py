import asyncio

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from zibloh import mcp_server


def _tool(name):
    tools = asyncio.run(mcp_server.server.list_tools())
    return next(t for t in tools if t.name == name)


def test_tool_names_follow_module_action():
    names = {t.name for t in asyncio.run(mcp_server.server.list_tools())}
    assert {"whoami", "robots_deploy", "robots_run", "secrets_create", "robots_secrets_bind"} <= names
    assert not any("reveal" in n for n in names)


def test_secrets_create_has_no_value_parameter():
    params = set(_tool("secrets_create").input_schema["properties"])
    assert params == {"name", "from_env", "from_file", "kind", "description"}
    assert set(_tool("secrets_rotate").input_schema["properties"]) == {"name", "from_env", "from_file"}


def test_secret_value_needs_exactly_one_source(monkeypatch, tmp_path):
    with pytest.raises(ToolError, match="Literal values are not accepted"):
        mcp_server._secret_value(None, None)
    monkeypatch.setenv("ERP_PASSWORD_SRC", "s3cret")
    assert mcp_server._secret_value("ERP_PASSWORD_SRC", None) == b"s3cret"
    f = tmp_path / "cert.pem"
    f.write_bytes(b"pem")
    assert mcp_server._secret_value(None, str(f)) == b"pem"
    with pytest.raises(ToolError):
        mcp_server._secret_value("ERP_PASSWORD_SRC", str(f))


def test_destructive_tools_are_annotated():
    for name in ("robots_delete", "secrets_delete", "robots_executions_cancel"):
        assert _tool(name).annotations.destructive_hint is True
    assert _tool("robots_list").annotations.read_only_hint is True


def test_every_tool_returns_structured_output():
    """Bare `list`/`dict` annotations give no output schema: lists arrive as one text block per item."""
    for tool in asyncio.run(mcp_server.server.list_tools()):
        assert tool.output_schema is not None, tool.name

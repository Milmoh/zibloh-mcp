# CLAUDE.md

`zibloh` CLI, MCP server and Claude Code plugin for Zibloh self-serve robots and the Warehouse.
It talks to the Zibloh API (`/self-serve`, `/cli-auth`, robots, executions, secrets,
`/warehouse`) with a company API key.

- `src/zibloh/client.py` is the only HTTP code. It adds the audit headers (`X-Zibloh-Client`,
  `-Tool`, `-Session`, `X-Request-Id`); presigned uploads go through `Client.upload()`, which never
  sends the API key.
- `src/zibloh/ops.py` holds the robot operations and `warehouse.py` the Warehouse ones (types and
  fields ↔ `json_schema` as the web app writes it, documents, files, flows, approvals, links);
  `cli.py` (Typer) and `mcp_server.py` (mcp 2.x `MCPServer`) are thin front ends over them. Add a
  feature there, then expose it in both, named `zibloh <module> <action>` / tool
  `<module>_<action>` (`zibloh warehouse docs list` / `warehouse_docs_list`), with `--json` on
  every command. Warehouse requests also send the key as `X-API-Key` (older backends read only
  that there). Approval decisions are the person's, so tools never decide on their own.
- Login is by browser (`browser_login.py`, device flow against `/cli-auth`): `zibloh login` and the
  MCP tools `auth_login`/`auth_status`. The key goes backend → `credentials.json`, never to the
  model or the screen. `--with-key` is only for CI. A key is for ONE company; each login is its
  own profile named after the company (`config.profile_name_for`), and `zibloh profiles use` /
  MCP `profiles_use` switch the default, so changing company never needs a new login.
  `hooks/hooks.json` blocks prompts with `zbl_…`.
- Secret values never reach the model: MCP secret tools take only `from_env`/`from_file`; there is
  no reveal anywhere. Keep it that way.
- `src/zibloh/templates/` mirrors the official robot template; `tests/test_packaging.py` checks that
  the templates pass `validate` with no warnings.
- Plugin: `.claude-plugin/plugin.json` (MCP server via `uvx --from ${CLAUDE_PLUGIN_ROOT}`),
  `.claude-plugin/marketplace.json`, `skills/*/SKILL.md`.

Tests: `uv venv && uv pip install -e . pytest respx && .venv/bin/pytest`.

# CLAUDE.md

`zibloh` CLI, MCP server and Claude Code plugin for Zibloh self-serve robots. It talks to the
Zibloh self-serve API (`/self-serve`, `/cli-auth`, robots, executions, secrets) with a company
API key.

- `src/zibloh/client.py` is the only HTTP code. It adds the audit headers (`X-Zibloh-Client`,
  `-Tool`, `-Session`, `X-Request-Id`); presigned uploads go through `Client.upload()`, which never
  sends the API key.
- `src/zibloh/ops.py` holds every operation; `cli.py` (Typer) and `mcp_server.py` (mcp 2.x
  `MCPServer`) are thin front ends over it. Add a feature to `ops.py`, then expose it in both,
  named `zibloh <module> <action>` / tool `<module>_<action>`, with `--json` on every command.
- Login is by browser (`browser_login.py`, device flow against `/cli-auth`): `zibloh login` and the
  MCP tools `auth_login`/`auth_status`. The key goes backend → `credentials.json`, never to the
  model or the screen. `--with-key` is only for CI. `hooks/hooks.json` blocks prompts with `zbl_…`.
- Secret values never reach the model: MCP secret tools take only `from_env`/`from_file`; there is
  no reveal anywhere. Keep it that way.
- `src/zibloh/templates/` mirrors the official robot template; `tests/test_packaging.py` checks that
  the templates pass `validate` with no warnings.
- Plugin: `.claude-plugin/plugin.json` (MCP server via `uvx --from ${CLAUDE_PLUGIN_ROOT}`),
  `.claude-plugin/marketplace.json`, `skills/*/SKILL.md`.

Tests: `uv venv && uv pip install -e . pytest respx && .venv/bin/pytest`.

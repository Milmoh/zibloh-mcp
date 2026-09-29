# Zibloh CLI and Claude Code plugin

Write, deploy, configure, run and debug [Zibloh](https://zibloh.com) robots from the terminal or
from Claude Code. The same package is:

- the `zibloh` CLI (`zibloh <module> <action>`, every command takes `--json`);
- an MCP server (`zibloh mcp`, stdio) with the same operations as `<module>_<action>` tools;
- a Claude Code plugin: the MCP server, three skills (`zibloh-setup`, `zibloh-workflow`,
  `zibloh-robot-authoring`) and a hook that keeps API keys out of prompts.

## Before you start

1. **Features.** Your company needs the **Autoservicio** (`self_serve`) and **API Keys**
   features enabled, plus **Secrets de robot** if your robots need credentials. Ask your Zibloh
   contact.
2. **A company admin to approve the login.** Logging in opens the web app and a company admin
   clicks *Autorizar* (see [Log in](#log-in)). Nobody creates or copies an API key by hand.
3. **Network access to `app.zibloh.com`.** Everything the CLI and the MCP server do, including
   uploading robot code, goes to `https://app.zibloh.com` (`https://devapp.zibloh.com` for
   Milmoh's dev platform). If Claude runs with a network allowlist, that domain must be on it:

   - **Claude Desktop / claude.ai** (code execution): an organization Owner adds
     `app.zibloh.com` in *Organization settings → Capabilities → Code execution*, under the
     additional allowed domains. Members of a Team/Enterprise organization cannot change it
     themselves. Without it the session fails with `403` and `blocked-by-allowlist` from
     Claude's proxy, before the request reaches Zibloh.
   - **Claude Code with the sandbox enabled**: add it to `sandbox.network.allowedDomains` in
     `~/.claude/settings.json` (or the project's `.claude/settings.json`):

     ```json
     { "sandbox": { "network": { "allowedDomains": ["app.zibloh.com"] } } }
     ```

   - **Company firewall / proxy**: allow outbound HTTPS to `app.zibloh.com`.

   Nothing else is needed: no `amazonaws.com` domains.

## Install

The repository is private for now: you need read access to `Milmoh/zibloh-mcp` and git
credentials for GitHub (e.g. `gh auth setup-git`).

Claude Code plugin:

```
/plugin marketplace add Milmoh/zibloh-mcp
/plugin install zibloh@zibloh
```

CLI only (needs [uv](https://docs.astral.sh/uv/)):

```bash
uv tool install git+ssh://git@github.com/Milmoh/zibloh-mcp   # SSH key on GitHub
# or, with HTTPS git credentials (gh auth setup-git):
uv tool install git+https://github.com/Milmoh/zibloh-mcp
```

## Log in

From Claude Code, just ask ("log me in to Zibloh"): Claude calls the `auth_login` tool. From a
terminal:

```bash
zibloh login            # --env dev for Milmoh's dev platform
zibloh whoami
```

Either way your browser opens at `app.zibloh.com/cli-login?code=XXXX-XXXX` (if it does not, open
the URL shown). Log in, check that the code matches the one in your terminal or in Claude Code,
choose what it may do (*Completo*, *Ejecución* or *Lectura*, and *MCP* for Claude Code) and
click **Autorizar**. The CLI then receives an API key for this computer, named
`Claude Code · <computer>` or `CLI · <computer>`, and stores it in
`~/.config/zibloh/credentials.json` (mode 0600). The key never appears on screen or in the chat;
to cut access, revoke it in *Configuración de empresa → API Keys*. Approving needs a company
admin.

For CI and scripts that already have a key: `zibloh login --with-key < keyfile` (or the hidden
prompt). `ZIBLOH_API_KEY`, `ZIBLOH_BASE_URL`, `ZIBLOH_ENV` and `ZIBLOH_PROFILE` override the
stored login; `--profile` switches between saved logins.

The plugin also stops any prompt that contains a Zibloh API key (`zbl_…`) before Claude reads
it. If that happens, revoke the key.

## The loop

```bash
zibloh launchers list                                  # where can it run
zibloh robots init ./my-robot                          # scaffold from the template
# ... write main.py ...
zibloh robots validate ./my-robot --target cloud
zibloh robots create --name "Facturas proveedor"       # once; note the robot_id
zibloh robots target <robot_id> --cloud                # or --launcher <launcher_id>
zibloh secrets create ERP_PASSWORD                     # hidden prompt
zibloh robots secrets bind <robot_id> ERP_PASSWORD
zibloh robots deploy <robot_id> ./my-robot
zibloh robots options define <robot_id> --from-config ./my-robot/config.json
zibloh robots options set <robot_id> test_mode=true
zibloh robots run <robot_id> --wait
zibloh robots executions logs <execution_id>
```

Schedules (`cron`, `auto_execute`) are set in the web app for now.

## What it will not do

- Show secret values: there is no reveal command, and the MCP tools only take secrets from an
  environment variable or a file, never as text.
- Create companies, enable features, create launcher licences or manage users: those stay in
  the web app.

## Audit

Every call carries `X-Zibloh-Client` (`cli`/`mcp`), `X-Zibloh-Tool` (the command or tool),
`X-Zibloh-Session` (one per process, or `ZIBLOH_SESSION`) and `X-Request-Id`. Zibloh stores one
audit row per call made with an API key.

## Development

```bash
uv venv && uv pip install -e . pytest respx
.venv/bin/pytest
```

`src/zibloh/templates/` is a copy of `main.py`, `main_browser_template.py`, `config.json` and
`requirements.txt` from the `zibloh-robot-template` repo; keep them in sync.

# Zibloh CLI and Claude Code plugin

Write, deploy, configure, run and debug [Zibloh](https://zibloh.com) robots from the terminal or
from Claude Code. The same package is:

- the `zibloh` CLI (`zibloh <module> <action>`, every command takes `--json`);
- an MCP server (`zibloh mcp`, stdio) with the same operations as `<module>_<action>` tools;
- a Claude Code plugin: the MCP server plus three skills (`zibloh-setup`, `zibloh-workflow`,
  `zibloh-robot-authoring`).

## Before you start

Your company needs, from Zibloh:

1. The **Autoservicio** (`self_serve`) and **API Keys** features enabled, plus **Secrets de robot**
   if your robots need credentials. Ask your Zibloh contact.
2. An API key: a company admin creates it once in the web app, *Configuración de empresa →
   API Keys → Crear key*, with the **Robots** permission (level *Completo* to deploy, *Ejecución*
   to run, *Lectura* to only look) and, to use it from Claude Code or another assistant, **MCP**.
   The key is shown once. Without **MCP** the key works with the CLI and scripts but the MCP
   tools are refused.

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
uv tool install git+https://github.com/Milmoh/zibloh-mcp
```

Then, **in your own terminal** (the key never goes through Claude):

```bash
zibloh login            # prompts for the key; --env dev for the dev environment
zibloh whoami
```

The key is stored in `~/.config/zibloh/credentials.json` (mode 0600). `ZIBLOH_API_KEY`,
`ZIBLOH_BASE_URL`, `ZIBLOH_ENV` and `ZIBLOH_PROFILE` override it; `--profile` switches between
saved keys.

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
- Create companies, enable features, create the first API key, create launcher licences or
  manage users: those stay in the web app.

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

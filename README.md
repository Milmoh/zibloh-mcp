# Zibloh CLI and Claude Code plugin

Write, deploy, configure, run and debug [Zibloh](https://zibloh.com) robots, and work with the
Zibloh Warehouse (document types, documents and their files, approval flows and approvals), from
the terminal or from Claude Code. The same package is:

- the `zibloh` CLI (`zibloh <module> <action>`, every command takes `--json`);
- an MCP server (`zibloh mcp`, stdio) with the same operations as `<module>_<action>` tools;
- a Claude Code plugin: the MCP server, four skills (`zibloh-setup`, `zibloh-workflow`,
  `zibloh-robot-authoring`, `zibloh-warehouse`) and a hook that keeps API keys out of prompts.

## Before you start

1. **Features.** Your company needs the **API Keys** feature, plus **Autoservicio**
   (`self_serve`) for robots (and **Secrets de robot** if they need credentials) or **Warehouse**
   for documents (and **Warehouse Aprobaciones** for approval flows). Ask your Zibloh contact.
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

   Robots need nothing else. Warehouse files are uploaded to and downloaded from storage
   directly, so for them also allow `zibloh-prod-warehouse-storage.s3.amazonaws.com` and
   `zibloh-prod-warehouse-storage.s3.eu-west-3.amazonaws.com`.

## Install

Claude Code plugin:

```
/plugin marketplace add Milmoh/zibloh-mcp
/plugin install zibloh@zibloh
```

CLI only (Python 3.11+; with [uv](https://docs.astral.sh/uv/) or pipx):

```bash
uv tool install zibloh      # or: pipx install zibloh
zibloh version
```

Upgrade with `uv tool upgrade zibloh`.

## Log in

From Claude Code, just ask ("log me in to Zibloh"): Claude calls the `auth_login` tool. From a
terminal:

```bash
zibloh login            # --env dev for Milmoh's dev platform
zibloh whoami
```

Either way your browser opens at `app.zibloh.com/cli-login?code=XXXX-XXXX` (if it does not, open
the URL shown). Log in, check that the code matches the one in your terminal or in Claude Code
and that the page shows the right company (switch it there if you belong to several), choose
what it may do with robots (*Completo*, *Ejecución*, *Lectura* or *Sin acceso*) and with the
Warehouse (*Completo*, *Documentos*, *Lectura* or *Sin acceso*), tick *MCP* for Claude Code and
click **Autorizar**. The CLI then receives an API key for this computer, named
`Claude Code · <computer>` or `CLI · <computer>`, and stores it in
`~/.config/zibloh/credentials.json` (mode 0600). The key never appears on screen or in the chat;
to cut access, revoke it in *Configuración de empresa → API Keys*. Approving needs a company
admin.

For CI and scripts that already have a key: `zibloh login --with-key < keyfile` (or the hidden
prompt). `ZIBLOH_API_KEY`, `ZIBLOH_BASE_URL`, `ZIBLOH_ENV` and `ZIBLOH_PROFILE` override the
stored login.

### Several companies

A key belongs to one company. If you work for several, log in once per company: each login is
saved as its own profile, named after the company (`-dev` on the dev platform), and becomes the
default. Then switch without logging in again:

```bash
zibloh profiles list        # * marks the active one
zibloh profiles use acme    # the CLI and Claude Code use it from now on
zibloh whoami --profile beta  # one command against another company
```

From Claude Code, ask it to switch company: it calls `profiles_list` / `profiles_use`.

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

## Warehouse

```bash
zibloh warehouse types create Facturas --path Contabilidad/Facturas \
  --field "Número:text:required" --field "Importe:number" --field "Fecha:date" --primary-key Número
zibloh warehouse fields add Facturas --field "Proveedor:text"
zibloh warehouse docs create Facturas --file ./F-001.pdf --set Número=F-001 --content '{"Importe": 1450}'
zibloh warehouse docs list Facturas --status missing_data
zibloh warehouse docs download <document_id> ./descargas/
zibloh warehouse flows create "Aprobación facturas" Facturas --steps @steps.json
zibloh warehouse approvals start <document_id>
zibloh warehouse approvals inbox                       # waiting for you
zibloh warehouse approvals decide <document_id> approve
zibloh warehouse docs export Facturas ./ --files       # ZIP with the files
```

A document type can be given by id, name or path. `--fields`, `--content`, `--steps` and
`--filters` take JSON or `@file`. Approval decisions are recorded as the person who approved
the login, with that person's permissions. In Claude Code, ask for it in words; the
`zibloh-warehouse` skill drives the `warehouse_*` tools.

## What it will not do

- Show secret values: there is no reveal command, and the MCP tools only take secrets from an
  environment variable or a file, never as text.
- Create companies, enable features, create launcher licences or manage users: those stay in
  the web app.

## Audit

Every call carries `X-Zibloh-Client` (`cli`/`mcp`), `X-Zibloh-Tool` (the command or tool),
`X-Zibloh-Session` (one per process, or `ZIBLOH_SESSION`) and `X-Request-Id`. Zibloh stores one
audit row per call made with an API key (Warehouse calls too). Documents also keep their own
history (`zibloh warehouse docs get <id> --history`).

## Development

```bash
uv venv && uv pip install -e . pytest respx
.venv/bin/pytest
```

Releases: bump the version in `pyproject.toml`, `src/zibloh/__init__.py` and
`.claude-plugin/plugin.json`, merge, then push a `vX.Y.Z` tag; `.github/workflows/publish.yml`
publishes to PyPI with trusted publishing (environment `pypi`, no stored token).

`src/zibloh/templates/` is a copy of `main.py`, `main_browser_template.py`, `config.json` and
`requirements.txt` from the `zibloh-robot-template` repo; keep them in sync.

## License

Apache-2.0. See [LICENSE](https://github.com/Milmoh/zibloh-mcp/blob/main/LICENSE).

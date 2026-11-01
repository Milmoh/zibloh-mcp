---
name: zibloh-setup
description: Connect Claude Code to a Zibloh company account (robots and Warehouse). Use when the user wants to start using Zibloh from Claude, log in or out, switch between dev and prod or between profiles, check which company/API key is active (`zibloh whoami`), or when any zibloh command or MCP tool fails with 401/403, "not logged in", a missing scope, self_serve/robot_secrets disabled, or `blocked-by-allowlist` (Claude's network allowlist).
---

# Zibloh setup

Goal: the `zibloh` CLI (and its MCP tools) authenticated against the right company and
environment, with no API key ever shown to the user or passing through Claude's context.

## 1. Prerequisites (outside Claude)

Claude cannot do these. Tell the user who has to do them:

1. **Feature flags** (a Zibloh superadmin, i.e. Milmoh staff): `api_keys` always; `self_serve`
   for robots (plus `robot_secrets` if robots use platform secrets); `warehouse` for the
   Warehouse (plus `warehouse_approvals` for approval flows).
2. **A company admin** to approve the login in the browser (step 2). If the user is not an
   admin, an admin can open the same link and approve it.
3. **Network access to `app.zibloh.com`** (`devapp.zibloh.com` for dev). If a call fails with
   `403` and `blocked-by-allowlist`, that is Claude's own network allowlist, not Zibloh:
   - Claude Desktop / claude.ai: an organization Owner adds `app.zibloh.com` in
     *Organization settings → Capabilities → Code execution* (additional allowed domains).
   - Claude Code with the sandbox on: add it to `sandbox.network.allowedDomains` in
     `~/.claude/settings.json`.
   Do not try to route around the allowlist. Robots need no other domain (their uploads also
   go to `app.zibloh.com`). Warehouse files go straight to storage: also allow
   `zibloh-prod-warehouse-storage.s3.amazonaws.com` and
   `zibloh-prod-warehouse-storage.s3.eu-west-3.amazonaws.com` (`-dev-` on dev).

Also outside Claude: creating a company, managing users or sharing robots, installing a launcher
on a server and creating its licence (an admin with Autoservicio downloads the installer from
Configuración de empresa → Launchers).

## 2. Log in (browser approval)

Call the `auth_login` MCP tool (`env: "prod"`; `"dev"` only for Milmoh's test platform). It returns
a `verification_url` and a `user_code` and tries to open the browser. Tell the user, in one short
message:

> Open <verification_url> (it may already be open), log in, check that it shows the code
> **<user_code>** and the right **company** (change it there if you belong to several), choose
> the access level and click **Autorizar**.

Then call `auth_status` with `wait_seconds: 120` (repeat while it says `pending`; the code lasts
10 minutes). `logged_in` means the key is saved in `~/.config/zibloh/credentials.json` as the
profile it names (one per company) and every tool uses it. `failed` says why (rejected, expired): start again with `auth_login`.

The page asks two levels; suggest what the user's task needs (and *MCP* ticked, or the MCP
tools are refused):
- **Robots**: *Completo* to build and deploy, *Ejecución* to only run, *Lectura* to only look,
  *Sin acceso* for Warehouse-only use.
- **Warehouse**: *Completo* to set up types, fields, folders and approval flows, *Documentos* to
  upload, edit and approve documents, *Lectura* to only look and download, *Sin acceso*.

Without the MCP tools (CLI only), run `zibloh login --env prod` yourself: it prints the same URL
and code and waits until the user approves (up to 10 minutes; use a long timeout, or ask the user
to run `! zibloh login` so they see the URL).

- Never ask for an API key, never suggest `--key` or `--with-key` in a conversation, and never
  write a key into a file. `--with-key` is for CI with a key from a secret store.
- If the user pastes a key anyway (the plugin's hook blocks messages containing `zbl_…`, but it
  can arrive in other ways), tell them to revoke it in Configuración de empresa → API Keys, and
  log in with the browser instead.
- If the CLI is not installed yet: `uv tool install zibloh` (or `pipx install zibloh`), from PyPI.

## 3. Check

Run `zibloh whoami --json` yourself. It shows company, key name, scopes, `capabilities`, and
whether `self_serve`, `robot_secrets`, `warehouse` and `warehouse_approvals` are enabled. Confirm with the user that the company is
the expected one before creating or deploying anything. If `robot_secrets` is off, secrets
commands will fail: stop and ask for the flag (step 1) rather than working around it.

## Companies, environments and profiles

- An API key belongs to ONE company, but a person can belong to several. Each login is saved as
  its own profile, named after the company (`acme`, `acme-dev` on dev) and made the default;
  other companies' profiles are kept.
- To switch company: `profiles_list`, then `profiles_use` (CLI: `zibloh profiles list`,
  `zibloh profiles use NAME`). No new login. Only call `auth_login` for a company that has no
  profile yet (or whose key was revoked). After switching, say which company is now active.
- Select per command with `--profile NAME` or per shell with `ZIBLOH_PROFILE=NAME` (it beats the
  default, so `profiles_use` warns when it is set).
- Environment variables override the stored login: `ZIBLOH_API_KEY`, `ZIBLOH_BASE_URL`,
  `ZIBLOH_PROFILE`. Useful in CI; never write their values into files you create or commit.
- Before any write in prod (deploy, run, secrets), state which profile/env is active.
- `zibloh logout [--profile NAME]` removes the stored key of that profile (default: the active one) (revoke it in the web app too
  if the computer is being retired).

## Troubleshooting

Always read the `detail` field of the error (`--json` output) and quote it to the user.

| Symptom | Cause | Fix |
|---|---|---|
| Not logged in | No stored login for this profile | `auth_login` (step 2) |
| 401 | Key revoked or expired; wrong env (dev login on prod) | Check env/profile; if still 401, `auth_login` again |
| 403 missing scope | The level approved is too low for the action (e.g. Lectura trying to deploy) | `auth_login` again and approve a higher level |
| 403 "no permite el uso por MCP" | Approved without **MCP** (`whoami` shows `mcp_allowed: false`) | `auth_login` again with MCP ticked, or use the CLI directly |
| 403 on approving in the browser | The approver is not a company admin, or `api_keys` (or `self_serve`/`warehouse` for the level chosen) is off | A company admin approves; ask Zibloh for the flags |
| `403` + `blocked-by-allowlist` | Claude's network allowlist, not Zibloh | Step 1.3 |
| 403 self_serve / feature disabled | Flag off for the company | Ask a Zibloh superadmin to enable `self_serve` / `api_keys` / `robot_secrets` |
| 404 on a robot or launcher id | Resource belongs to another company or profile | `zibloh whoami`; if it is the wrong company, `profiles_use` the right one |
| Connection error | Wrong `--base-url` / `ZIBLOH_BASE_URL`, network, firewall | Unset the override or fix the URL; allow `app.zibloh.com` |

Once `whoami` is correct, continue with the `zibloh-workflow` skill (robots) or `zibloh-warehouse` (documents).

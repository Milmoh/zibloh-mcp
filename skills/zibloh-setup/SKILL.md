---
name: zibloh-setup
description: Connect Claude Code to a Zibloh company account. Use when the user wants to start using Zibloh from Claude, log in or out, switch between dev and prod or between profiles, check which company/API key is active (`zibloh whoami`), or when any zibloh command or MCP tool fails with 401/403, "not logged in", a missing scope, or self_serve/robot_secrets disabled.
---

# Zibloh setup

Goal: the `zibloh` CLI (and its MCP tools) authenticated against the right company and
environment, with the API key never passing through Claude's context.

## 1. Prerequisites (outside Claude)

Claude cannot do these. Tell the user who has to do them:

1. **Feature flags** (a Zibloh superadmin, i.e. Milmoh staff): enable `self_serve` and `api_keys`
   for the company, plus `robot_secrets` if robots will use platform secrets.
2. **API key** (a company admin, once, in the web app): Configuración de empresa → API Keys →
   create a key with **Robots** (level *Completo* to build and deploy, *Ejecución* to only run,
   *Lectura* to only look) plus **MCP** (required for the MCP tools). The key is shown only once; the admin
   keeps it somewhere safe (password manager) and never pastes it into the chat.

Also outside Claude: creating a company, managing users or sharing robots, installing a launcher
on a server and creating its licence (an admin with Autoservicio downloads the installer from
Configuración de empresa → Launchers).

## 2. Log in (the user runs it, not Claude)

Ask the user to run this **in their own terminal** (a separate window, not through Claude), so
the key goes into a hidden prompt instead of the conversation:

```
zibloh login --env prod
```

Not with the `!` prefix: `!` commands have no interactive terminal, so the hidden prompt cannot
read the key. The login is saved to `~/.config/zibloh/credentials.json`, which the CLI and the
MCP tools in this session then use. If the CLI is not installed yet:
`uv tool install git+ssh://git@github.com/Milmoh/zibloh-mcp` (private repo: needs GitHub access).

- `--env dev|prod` selects the environment (dev is Milmoh's test platform; customers use prod).
- `--base-url URL` for a non-standard backend. `--profile NAME` stores the login under a name.
- The key can also come from stdin (`zibloh login --env prod < keyfile`); delete the file after.
- Never suggest `--key <value>`: it lands in shell history and, if Claude runs it, in the
  transcript. Never ask the user to paste the key in the chat. If they do anyway, tell them to
  revoke it in the web app and create a new one.

## 3. Check

Run `zibloh whoami --json` yourself. It shows company, key name, scopes, `capabilities`, and
whether `self_serve` and `robot_secrets` are enabled. Confirm with the user that the company is
the expected one before creating or deploying anything. If `robot_secrets` is off, secrets
commands will fail: stop and ask for the flag (step 1) rather than working around it.

## Environments and profiles

- One login per profile. Use profiles to keep dev and prod apart, e.g.
  `zibloh login --env dev --profile dev` and `zibloh login --env prod --profile prod`.
- Select per command with `--profile NAME` or per shell with `ZIBLOH_PROFILE=NAME`.
- Environment variables override the stored login: `ZIBLOH_API_KEY`, `ZIBLOH_BASE_URL`,
  `ZIBLOH_PROFILE`. Useful in CI; never write their values into files you create or commit.
- Before any write in prod (deploy, run, secrets), state which profile/env is active.
- `zibloh logout` removes the stored key for the current profile.

## Troubleshooting

Always read the `detail` field of the error (`--json` output) and quote it to the user.

| Symptom | Cause | Fix |
|---|---|---|
| 401 | Key revoked, expired or mistyped; wrong env (dev key on prod) | Check `--env`/profile; if still 401, admin creates a new key, user runs `zibloh login` again in their terminal |
| 403 missing scope | The key's Robots level is too low for the action (e.g. Lectura trying to deploy) | Admin creates a key with a higher Robots level |
| 403 "no permite el uso por MCP" | Key has no **MCP** permission (`whoami` shows `mcp_allowed: false`) | Admin creates a key with MCP, or use the CLI directly |
| 403 self_serve / feature disabled | Flag off for the company | Ask a Zibloh superadmin to enable `self_serve` / `api_keys` / `robot_secrets` |
| 404 on a robot or launcher id | Resource belongs to another company or profile | `zibloh whoami`, then `zibloh robots list` / `launchers list` |
| Connection error | Wrong `--base-url` / `ZIBLOH_BASE_URL`, network | Unset the override or fix the URL |

Once `whoami` is correct, continue with the `zibloh-workflow` skill.

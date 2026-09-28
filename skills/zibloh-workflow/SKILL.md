---
name: zibloh-workflow
description: End-to-end loop for building and operating a Zibloh RPA robot from Claude Code with the zibloh CLI or MCP tools (robots_*, secrets_*, launchers_*). Use when the user wants to create, deploy, configure, run, debug or iterate on a Zibloh robot, choose where it runs (cloud or a launcher), manage its options, metrics or secrets, read execution logs, or cancel an execution.
---

# Zibloh workflow

Every command accepts `--json`; use it and parse the result. The MCP server exposes the same
operations as `<module>_<action>` tools (`robots_deploy`, `robots_run`, `secrets_create`...).
If a command fails with 401/403 or you are not logged in, switch to the `zibloh-setup` skill.
To write the robot code itself, use the `zibloh-robot-authoring` skill.

## The loop

1. **Who and where.** `zibloh whoami --json`: confirm company, env/profile, capabilities, and
   whether `robot_secrets` is enabled.
2. **Choose a runner.** `zibloh launchers list --json` returns the company launchers (id, name,
   platform, status, execution type) and the execution types, including cloud. Pick with the user:
   - **cloud**: 10 min max, no pip install (image libraries or vendored code only), no access to
     the customer's network.
   - **Windows launcher**: customer server, 30 min default, venv from `requirements.txt`, GUI
     and local network available.
   - **Linux launcher**: customer server, Docker, 600 s default cap, `requirements.txt`
     installed every run, nothing persists between runs.
   An offline launcher leaves executions `queued`: check `status` before choosing it.
3. **Create once.** Look for an existing robot first (`zibloh robots list`). Otherwise
   `zibloh robots create --name "..." --description "..."`, then
   `zibloh robots target <id> --cloud` or `--launcher <launcher_id>`. Reuse this id for every
   redeploy; do not create a new robot per iteration.
4. **Scaffold.** `zibloh robots init <dir> --template basic` (or `browser` for Playwright).
5. **Write the code** with the `zibloh-robot-authoring` skill. Declare every configurable value
   in `config.json` `opciones`; secrets never go there.
6. **Validate.** `zibloh robots validate <dir> --target cloud|onprem`. Fix every error. Treat
   warnings seriously: a cloud import that is not in the image nor vendored will fail at run
   time; unpinned requirements on-prem break on the next release of the library.
7. **Secrets.** For each credential the robot reads:
   - Check what exists: `zibloh secrets list` (names only; there is no reveal).
   - Create missing ones. For values the user must type, ask them to run
     `zibloh secrets create NAME` **in their own terminal** (hidden prompt; the `!` prefix has no
     terminal for it). For values already in an env var or file on
     their machine: `zibloh secrets create NAME --from-env VAR` or `--from-file PATH`
     (`--file` for a file secret such as a service-account JSON). `rotate` takes the same inputs.
     The MCP `secrets_create`/`secrets_rotate` tools accept only `from_env`/`from_file`.
   - Bind to the robot: `zibloh robots secrets bind <id> NAME --as env --env-name ERP_TOKEN --required`
     or `--as file --env-name GOOGLE_SA_JSON --filename sa.json`. Bind file secrets as `file`.
     The `--env-name` must match what the code reads. Check with `zibloh robots secrets list <id>`.
8. **Deploy.** `zibloh robots deploy <id> <dir>` validates, zips, uploads and activates.
9. **Options.** `zibloh robots options define <id> --from-config <dir>/config.json` declares
   the options (rerun when you add or rename one). Set values with
   `zibloh robots options set <id> TEST_MODE=true MAX_ITEMS=5`, check with `options list <id>`.
   Set `max_execution_time` (minutes) within the runner's limit. Schedules (`cron`,
   `auto_execute`) are set by the user in the web app. Optionally record value with
   `zibloh robots metrics set <id> ...` (minutes / euros saved per run).
10. **Run.** `zibloh robots run <id> --wait --timeout 900 --tail 50` launches, waits for a
    terminal status and prints the log tail. Without `--wait`, poll
    `zibloh robots executions get <exec_id>`.
11. **Read the result.** `zibloh robots executions logs <exec_id> --tail 200`,
    `executions list <id>` for history. Status goes `queued` → `assigned` → `running` →
    `success|failure|cancelled`.
12. **Fix and redeploy** the same id (steps 5-10) until it succeeds. Then, with the user's
    explicit OK, set `TEST_MODE=false` and run against real data.

## Guardrails

- **Test first.** First runs use `TEST_MODE=true` (and the email redirect option) and a small
  batch. Ask before any run that touches real data, writes to external systems or sends email.
- **Secrets never pass through you.** Never echo, print, log, commit or paste secret values; never
  put them in `config.json`, options, `robot.env.json`, code, command lines or commit messages.
  Have the user type values in their own terminal, or use `--from-env`/`--from-file`. Do not `cat` files
  that contain credentials.
- **Prod awareness.** Say which profile/env you are about to modify before deploy, run or secret
  changes in prod.
- **Runaway executions.** If a run exceeds its expected duration, loops or spams output, cancel
  it: `zibloh robots executions cancel <exec_id>`. Do not start a new run while one is stuck.
- **Errors.** On any 4xx, read and report the `detail` field instead of retrying blindly. 403 on
  secrets usually means `robot_secrets` is off; 409/422 usually means invalid state or input.
- **Deleting** a robot or secret is irreversible: confirm with the user first.

## Reading a failed run

- `failure` with a timeout message: the runner limit was hit (cloud 10 min; lower
  `max_execution_time` or split the work; process in batches with persisted progress).
- `ModuleNotFoundError` on cloud: the library is not in the image; vendor it (pure Python) or
  move the robot to a launcher.
- Missing secret env var: binding absent, wrong `--env-name`, or `load_secrets()` not called.
- `success` expected but got `failure`: check the final JSON line and exit code; on cloud, words
  like `error`, `exception`, `not found` in logs can flag failure when the last line is not an
  explicit success.
- Stays `queued`: the target launcher is offline or the robot targets the wrong execution type.

## Claude cannot

Create companies or enable feature flags (superadmin), create the first API key (company admin,
web app), install a launcher or create its licence, reveal secret values, manage users or share
robots, or edit schedules from the CLI (web app for now). Say so and tell the user who can.

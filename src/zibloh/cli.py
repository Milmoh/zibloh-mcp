"""`zibloh <module> <action>`: the command line front end. Every command takes `--json`."""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

import typer

from zibloh import __version__, config, ops
from zibloh.client import ApiError, Client

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Build, deploy and run Zibloh robots.")
robots_app = typer.Typer(no_args_is_help=True, help="Robots: create, deploy, configure, run.")
options_app = typer.Typer(no_args_is_help=True, help="Robot options (configuration).")
metrics_app = typer.Typer(no_args_is_help=True, help="Time and money saved per run.")
executions_app = typer.Typer(no_args_is_help=True, help="Executions: status, logs, cancel.")
robot_secrets_app = typer.Typer(no_args_is_help=True, help="Secrets bound to a robot.")
secrets_app = typer.Typer(no_args_is_help=True, help="Company secrets (values are never shown).")
launchers_app = typer.Typer(no_args_is_help=True, help="Company launchers.")
app.add_typer(robots_app, name="robots")
app.add_typer(launchers_app, name="launchers")
app.add_typer(secrets_app, name="secrets")
robots_app.add_typer(options_app, name="options")
robots_app.add_typer(metrics_app, name="metrics")
robots_app.add_typer(executions_app, name="executions")
robots_app.add_typer(robot_secrets_app, name="secrets")

JsonOpt = typer.Option(False, "--json", help="Machine-readable output.")
ProfileOpt = typer.Option(None, "--profile", help="Profile to use (default: the saved default).")


# ── plumbing ──────────────────────────────────────────────────────────────────

def _client(ctx: typer.Context, profile: Optional[str]) -> Client:
    c = Client(config.load_profile(profile), client_name="cli")
    c.tool = " ".join(ctx.command_path.split()[1:])
    return c


def _err(message: str, json_out: bool) -> None:
    if json_out:
        typer.echo(json.dumps({"error": message}))
    else:
        typer.secho(f"error: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _run(ctx: typer.Context, profile: Optional[str], json_out: bool, fn: Callable[[Client], Any],
         human: Optional[Callable[[Any], None]] = None) -> Any:
    try:
        c = _client(ctx, profile)
        try:
            result = fn(c)
        finally:
            c.close()
    except (ApiError, ops.OpError, config.ConfigError) as exc:
        _err(str(exc), json_out)
    _emit(result, json_out, human)
    return result


def _emit(result: Any, json_out: bool, human: Optional[Callable[[Any], None]] = None) -> None:
    if json_out:
        typer.echo(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    elif human is not None:
        human(result)
    elif isinstance(result, (dict, list)):
        typer.echo(json.dumps(result, indent=2, default=str, ensure_ascii=False))
    elif result is not None:
        typer.echo(str(result))


def _table(rows: list[dict], columns: list[str]) -> None:
    if not rows:
        typer.echo("(none)")
        return
    cells = [[("" if r.get(c) is None else str(r.get(c))) for c in columns] for r in rows]
    widths = [max(len(c), *(len(row[i]) for row in cells)) for i, c in enumerate(columns)]
    typer.echo("  ".join(c.upper().ljust(w) for c, w in zip(columns, widths)))
    for row in cells:
        typer.echo("  ".join(v.ljust(w) for v, w in zip(row, widths)))


def _progress(json_out: bool) -> Callable[[str], None]:
    return (lambda _msg: None) if json_out else (lambda msg: typer.secho(msg, fg=typer.colors.BRIGHT_BLACK, err=True))


def _read_secret(from_env: Optional[str], from_file: Optional[Path], stdin: bool, prompt: str) -> bytes:
    sources = [s for s in (from_env, from_file, stdin or None) if s]
    if len(sources) > 1:
        raise ops.OpError("Use only one of --from-env, --from-file, --stdin")
    if from_env:
        if from_env not in os.environ:
            raise ops.OpError(f"Environment variable {from_env} is not set")
        return os.environ[from_env].encode("utf-8")
    if from_file:
        return from_file.expanduser().read_bytes()
    if stdin:
        return sys.stdin.buffer.read()
    if not sys.stdin.isatty():
        raise ops.OpError("No value given. Run this in your own terminal (hidden prompt), or pass --from-env/--from-file/--stdin")
    value = getpass.getpass(prompt)
    if not value:
        raise ops.OpError("Empty value")
    return value.encode("utf-8")


def _parse_assignments(pairs: List[str]) -> dict:
    values = {}
    for pair in pairs:
        if "=" not in pair:
            raise ops.OpError(f"Expected NAME=VALUE, got '{pair}'")
        name, value = pair.split("=", 1)
        values[name.strip()] = value
    return values


# ── auth ──────────────────────────────────────────────────────────────────────

@app.command()
def login(
    ctx: typer.Context,
    env: str = typer.Option(config.DEFAULT_ENV, "--env", help="prod or dev."),
    base_url: Optional[str] = typer.Option(None, "--base-url", help="Custom API base URL (overrides --env)."),
    profile: str = typer.Option(config.DEFAULT_PROFILE, "--profile"),
    key: Optional[str] = typer.Option(None, "--key", help="Discouraged: ends up in shell history. Prefer the prompt or stdin."),
    json_out: bool = JsonOpt,
):
    """Save an API key (Configuración de empresa → API Keys, permission "Robots", plus "MCP" for assistants)."""
    try:
        if key is None:
            key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Zibloh API key: ").strip()
        if not key.startswith("zbl_"):
            raise ops.OpError("That does not look like a Zibloh API key (zbl_…)")
        candidate = config.Profile(name=profile, base_url=config.resolve_base_url(env, base_url), api_key=key, env=env)
        c = Client(candidate, client_name="cli")
        c.tool = "login"
        try:
            me = ops.whoami(c)
        finally:
            c.close()
        config.save_profile(profile, key, env, base_url)
    except (ApiError, ops.OpError, config.ConfigError) as exc:
        _err(str(exc), json_out)
    _emit(me, json_out, lambda m: typer.echo(
        f"Logged in to {m['company_name']} ({m['base_url']}) as key '{m.get('api_key_name')}', profile '{profile}'."
        + ("" if m.get("self_serve") else "\nWarning: self-service is not enabled for this company; ask Zibloh to enable it.")
        + ("" if m.get("mcp_allowed") else "\nNote: this key has no MCP permission; the MCP tools (Claude Code plugin) will be refused.")
    ))


@app.command()
def logout(profile: str = typer.Option(config.DEFAULT_PROFILE, "--profile"), json_out: bool = JsonOpt):
    """Forget the saved key of a profile (revoke it in the web app if it leaked)."""
    removed = config.delete_profile(profile)
    _emit({"profile": profile, "removed": removed}, json_out,
          lambda r: typer.echo(f"Removed profile '{profile}'." if r["removed"] else f"No profile '{profile}'."))


@app.command()
def whoami(ctx: typer.Context, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Company, key, scopes and what this key can do."""
    def human(m):
        typer.echo(f"company:      {m['company_name']} ({m['company_id']})")
        typer.echo(f"api:          {m['base_url']} (profile {m['profile']})")
        typer.echo(f"key:          {m.get('api_key_name')} [{', '.join(m.get('scopes') or [])}]")
        typer.echo(f"self-service: {'on' if m['self_serve'] else 'OFF'}   robot secrets: {'on' if m['robot_secrets'] else 'OFF'}   mcp: {'allowed' if m.get('mcp_allowed') else 'NOT allowed'}")
        typer.echo(f"can:          {', '.join(m['capabilities']) or '(nothing)'}")
    _run(ctx, profile, json_out, ops.whoami, human)


@app.command()
def version():
    """Print the CLI version."""
    typer.echo(__version__)


@app.command()
def mcp():
    """Run the MCP server on stdio (used by the Claude Code plugin)."""
    from zibloh.mcp_server import run

    run()


# ── launchers ─────────────────────────────────────────────────────────────────

@launchers_app.command("list")
def launchers_list(ctx: typer.Context, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Launchers of the company and the execution types (cloud included)."""
    def human(r):
        _table(r["launchers"], ["launcher_id", "launcher_type", "platform", "status", "execution_type_name", "last_seen_at"])
        typer.echo("")
        _table(r["execution_types"], ["execution_type_id", "tipology", "name"])
    _run(ctx, profile, json_out, ops.launchers_list, human)


# ── robots ────────────────────────────────────────────────────────────────────

@robots_app.command("list")
def robots_list(ctx: typer.Context, search: Optional[str] = typer.Option(None, "--search"),
                profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Robots of the company."""
    _run(ctx, profile, json_out, lambda c: ops.robots_list(c, search),
         lambda rows: _table(rows, ["robot_id", "name", "status", "execution", "target", "launcher_name", "last_exec"]))


@robots_app.command("get")
def robots_get(ctx: typer.Context, robot_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """A robot with its options."""
    _run(ctx, profile, json_out, lambda c: ops.robots_get(c, robot_id))


@robots_app.command("create")
def robots_create(ctx: typer.Context, name: str = typer.Option(..., "--name"),
                  description: Optional[str] = typer.Option(None, "--description"),
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Create an empty robot (status pending_executable until the first deploy)."""
    _run(ctx, profile, json_out, lambda c: ops.robots_create(c, name, description))


@robots_app.command("delete")
def robots_delete(ctx: typer.Context, robot_id: str, yes: bool = typer.Option(False, "--yes", help="Do not ask."),
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Delete a robot."""
    if not yes and sys.stdin.isatty():
        typer.confirm(f"Delete robot {robot_id}?", abort=True)
    _run(ctx, profile, json_out, lambda c: ops.robots_delete(c, robot_id))


@robots_app.command("target")
def robots_target(ctx: typer.Context, robot_id: str,
                  cloud: bool = typer.Option(False, "--cloud", help="Run on the Zibloh cloud runner."),
                  launcher: Optional[str] = typer.Option(None, "--launcher", help="Run on this launcher id."),
                  execution_type: Optional[str] = typer.Option(None, "--execution-type", help="Cloud execution type id, if there are several."),
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Choose where the robot runs."""
    _run(ctx, profile, json_out, lambda c: ops.robots_target(c, robot_id, cloud=cloud, launcher_id=launcher, execution_type_id=execution_type))


@robots_app.command("init")
def robots_init(directory: Path, template: str = typer.Option("basic", "--template", help="basic or browser."),
                force: bool = typer.Option(False, "--force"), json_out: bool = JsonOpt):
    """Scaffold a robot from the official template."""
    try:
        result = ops.robots_init(directory, template, force)
    except ops.OpError as exc:
        _err(str(exc), json_out)
    _emit(result, json_out, lambda r: typer.echo(f"Created {r['template']} robot in {r['directory']}: " + ", ".join(Path(f).name for f in r["files"])))


def _print_report(r: dict) -> None:
    for e in r["errors"]:
        typer.secho(f"error: {e}", fg=typer.colors.RED)
    for w in r["warnings"]:
        typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)
    typer.echo(f"{'OK' if r['ok'] else 'FAILED'}: {r['files']} files, {r['total_bytes'] / 1e6:.1f} MB")


@robots_app.command("validate")
def robots_validate(directory: Path, target: Optional[str] = typer.Option(None, "--target", help="cloud or onprem (default: warnings for both)."),
                    json_out: bool = JsonOpt):
    """Check a robot directory before deploying."""
    if target not in (None, "cloud", "onprem"):
        _err("--target must be cloud or onprem", json_out)
    result = ops.robots_validate(directory, target)
    _emit(result, json_out, _print_report)
    if not result["ok"]:
        raise typer.Exit(1)


@robots_app.command("deploy")
def robots_deploy(ctx: typer.Context, robot_id: str, directory: Path, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Validate, zip and upload the robot code (activates the robot)."""
    def human(r):
        for w in r["warnings"]:
            typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)
        typer.echo(f"Deployed {r['robot_name']} ({r['files']} files, {r['size'] / 1e6:.1f} MB, sha256 {r['sha256'][:12]}…)")
    _run(ctx, profile, json_out, lambda c: ops.robots_deploy(c, robot_id, directory, _progress(json_out)), human)


@robots_app.command("run")
def robots_run(ctx: typer.Context, robot_id: str,
               wait: bool = typer.Option(False, "--wait", help="Wait for the end and print the log tail."),
               timeout: float = typer.Option(900, "--timeout", help="Seconds to wait with --wait."),
               tail: int = typer.Option(50, "--tail", help="Log lines to print with --wait."),
               profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Launch a robot."""
    def fn(c):
        launched = ops.robots_run(c, robot_id)
        if not wait:
            return launched
        result = ops.wait_for_execution(c, launched["robot_execution_id"], timeout=timeout, tail=tail, progress=_progress(json_out))
        result["launch"] = launched
        return result

    def human(r):
        if "execution" not in r:
            typer.echo(f"Launched execution {r['robot_execution_id']} ({r.get('launch_mode')}, {r.get('status')})")
            return
        _print_execution_result(r)
    result = _run(ctx, profile, json_out, fn, human)
    if wait and (result.get("timed_out") or result["execution"]["status"] != "success"):
        raise typer.Exit(1)


def _print_execution_result(r: dict) -> None:
    ex = r["execution"]
    if r.get("log") and r["log"].get("content"):
        typer.echo(r["log"]["content"].rstrip("\n"))
        typer.echo("")
    elif not r.get("timed_out"):
        typer.secho("(log not uploaded yet: try `zibloh robots executions logs` in a moment)", fg=typer.colors.BRIGHT_BLACK)
    state = "timed out waiting" if r.get("timed_out") else ex["status"]
    color = typer.colors.GREEN if ex["status"] == "success" else typer.colors.RED
    typer.secho(f"{state}: {ex.get('log_message') or ''}  [{ex.get('processed_count_detail') or ex.get('processed_count')}]  execution {ex['execution_id']}", fg=color)


# ── robot options / metrics ───────────────────────────────────────────────────

@options_app.command("list")
def options_list(ctx: typer.Context, robot_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Options of a robot."""
    _run(ctx, profile, json_out, lambda c: ops.options_list(c, robot_id),
         lambda rows: _table(rows, ["name", "option_type", "data_type", "value", "default"]))


@options_app.command("define")
def options_define(ctx: typer.Context, robot_id: str,
                   from_config: Path = typer.Option(..., "--from-config", help="config.json with an `opciones` list."),
                   profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Declare the robot's options (existing ones are kept as they are)."""
    _run(ctx, profile, json_out, lambda c: ops.options_define_from_config(c, robot_id, from_config))


@options_app.command("set")
def options_set(ctx: typer.Context, robot_id: str, assignments: List[str] = typer.Argument(..., help="NAME=VALUE ..."),
                profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Set values of existing options."""
    _run(ctx, profile, json_out, lambda c: ops.options_set(c, robot_id, _parse_assignments(assignments)))


@metrics_app.command("get")
def metrics_get(ctx: typer.Context, robot_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Saved-time/money metrics of a robot."""
    _run(ctx, profile, json_out, lambda c: ops.metrics_get(c, robot_id),
         lambda rows: _table(rows, ["metric_type", "name", "value", "default"]))


@metrics_app.command("set")
def metrics_set(ctx: typer.Context, robot_id: str,
                minutes: Optional[float] = typer.Option(None, "--minutes", help="Minutes saved."),
                euros: Optional[float] = typer.Option(None, "--euros", help="Euros saved."),
                scope: str = typer.Option("execution", "--scope", help="execution (per run) or process (per processed unit)."),
                profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Set minutes/euros saved per run or per processed unit."""
    _run(ctx, profile, json_out, lambda c: ops.metrics_set(c, robot_id, minutes, euros, scope))


# ── executions ────────────────────────────────────────────────────────────────

@executions_app.command("list")
def executions_list(ctx: typer.Context, robot_id: str, limit: int = typer.Option(20, "--limit"),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Recent executions of a robot."""
    _run(ctx, profile, json_out, lambda c: ops.executions_list(c, robot_id, limit),
         lambda rows: _table(rows, ["execution_id", "status", "started_at", "ended_at", "processed_count"]))


@executions_app.command("get")
def executions_get(ctx: typer.Context, execution_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Status and status history of an execution."""
    _run(ctx, profile, json_out, lambda c: ops.execution_get(c, execution_id))


@executions_app.command("logs")
def executions_logs(ctx: typer.Context, execution_id: str, tail: Optional[int] = typer.Option(None, "--tail"),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Log of an execution (the runner uploads it when the run ends)."""
    def fn(c):
        log = ops.execution_logs(c, execution_id, tail)
        if log is None:
            raise ops.OpError("The log is not uploaded yet (the runner uploads it when the execution ends)")
        return log
    _run(ctx, profile, json_out, fn, lambda r: typer.echo(r["content"].rstrip("\n")))


@executions_app.command("cancel")
def executions_cancel(ctx: typer.Context, execution_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Cancel a queued execution or ask a running one to stop."""
    _run(ctx, profile, json_out, lambda c: ops.execution_cancel(c, execution_id))


# ── secrets ───────────────────────────────────────────────────────────────────

SECRET_SOURCES = "Value from --from-env VAR, --from-file PATH, --stdin, or a hidden prompt."


@secrets_app.command("list")
def secrets_list(ctx: typer.Context, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Company secrets (names and kinds only)."""
    _run(ctx, profile, json_out, ops.secrets_list,
         lambda rows: _table(rows, ["name", "kind", "is_active", "description", "company_secret_id"]))


@secrets_app.command("create", help=f"Create a secret. {SECRET_SOURCES}")
def secrets_create(ctx: typer.Context, name: str,
                   file: bool = typer.Option(False, "--file", help="File secret (mounted as a file) instead of text."),
                   description: Optional[str] = typer.Option(None, "--description"),
                   from_env: Optional[str] = typer.Option(None, "--from-env"),
                   from_file: Optional[Path] = typer.Option(None, "--from-file"),
                   stdin: bool = typer.Option(False, "--stdin"),
                   profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    try:
        value = _read_secret(from_env, from_file, stdin, f"Value for {name}: ")
    except (ops.OpError, OSError) as exc:
        _err(str(exc), json_out)
    _run(ctx, profile, json_out, lambda c: ops.secrets_create(c, name, value, kind="file" if file else "env", description=description),
         lambda r: typer.echo(f"Created {r['kind']} secret {r['name']}. Bind it with `zibloh robots secrets bind <robot_id> {r['name']}`."))


@secrets_app.command("rotate", help=f"Replace a secret's value. {SECRET_SOURCES}")
def secrets_rotate(ctx: typer.Context, name: str,
                   from_env: Optional[str] = typer.Option(None, "--from-env"),
                   from_file: Optional[Path] = typer.Option(None, "--from-file"),
                   stdin: bool = typer.Option(False, "--stdin"),
                   profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    try:
        value = _read_secret(from_env, from_file, stdin, f"New value for {name}: ")
    except (ops.OpError, OSError) as exc:
        _err(str(exc), json_out)
    _run(ctx, profile, json_out, lambda c: ops.secrets_rotate(c, name, value),
         lambda r: typer.echo(f"Rotated {r['name']}."))


@secrets_app.command("delete")
def secrets_delete(ctx: typer.Context, name: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Deactivate a secret (unbind it from every robot first)."""
    _run(ctx, profile, json_out, lambda c: ops.secrets_delete(c, name))


@robot_secrets_app.command("list")
def robot_secrets_list(ctx: typer.Context, robot_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Secrets bound to a robot and how they are exposed."""
    _run(ctx, profile, json_out, lambda c: ops.robot_secrets_list(c, robot_id),
         lambda rows: _table(rows, ["secret_name", "expose_as", "target_env_name", "mount_filename", "is_required"]))


@robot_secrets_app.command("bind")
def robot_secrets_bind(ctx: typer.Context, robot_id: str, name: str,
                       expose_as: Optional[str] = typer.Option(None, "--as", help="env or file (default: from the secret kind)."),
                       env_name: Optional[str] = typer.Option(None, "--env-name", help="Env var the robot reads (default: the secret name)."),
                       filename: Optional[str] = typer.Option(None, "--filename", help="File name for file secrets."),
                       required: bool = typer.Option(False, "--required"),
                       profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Give a robot access to a secret."""
    _run(ctx, profile, json_out, lambda c: ops.robot_secrets_bind(c, robot_id, name, expose_as=expose_as, env_name=env_name, filename=filename, required=required),
         lambda rows: _table(rows, ["secret_name", "expose_as", "target_env_name", "mount_filename", "is_required"]))


@robot_secrets_app.command("unbind")
def robot_secrets_unbind(ctx: typer.Context, robot_id: str, name: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Remove a robot's access to a secret."""
    _run(ctx, profile, json_out, lambda c: ops.robot_secrets_unbind(c, robot_id, name),
         lambda rows: _table(rows, ["secret_name", "expose_as", "target_env_name", "mount_filename", "is_required"]))


def main() -> None:
    app()

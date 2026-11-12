"""`zibloh <module> <action>`: the command line front end. Every command takes `--json`."""
from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path
from typing import Any, Callable, List, Optional

import typer

from zibloh import __version__, browser_login, config, ops, warehouse
from zibloh.client import ApiError, Client

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Build, deploy and run Zibloh robots.")
robots_app = typer.Typer(no_args_is_help=True, help="Robots: create, deploy, configure, run.")
options_app = typer.Typer(no_args_is_help=True, help="Robot options (configuration).")
metrics_app = typer.Typer(no_args_is_help=True, help="Time and money saved per run (read-only; set in the Backoffice).")
executions_app = typer.Typer(no_args_is_help=True, help="Executions: status, logs, cancel.")
robot_secrets_app = typer.Typer(no_args_is_help=True, help="Secrets bound to a robot.")
secrets_app = typer.Typer(no_args_is_help=True, help="Company secrets (values are never shown).")
launchers_app = typer.Typer(no_args_is_help=True, help="Company launchers.")
profiles_app = typer.Typer(no_args_is_help=True, help="Saved logins, one per company: list and switch.")
warehouse_app = typer.Typer(no_args_is_help=True, help="Warehouse: document types, documents, approvals.")
wh_types_app = typer.Typer(no_args_is_help=True, help="Document types (lists).")
wh_fields_app = typer.Typer(no_args_is_help=True, help="Fields of a document type.")
wh_folders_app = typer.Typer(no_args_is_help=True, help="Folders.")
wh_docs_app = typer.Typer(no_args_is_help=True, help="Documents and their files.")
wh_flows_app = typer.Typer(no_args_is_help=True, help="Approval flows.")
wh_steps_app = typer.Typer(no_args_is_help=True, help="Steps of an approval flow.")
wh_approvers_app = typer.Typer(no_args_is_help=True, help="Approvers of a step.")
wh_approvals_app = typer.Typer(no_args_is_help=True, help="Approvals of documents.")
wh_links_app = typer.Typer(no_args_is_help=True, help="Linked documents (matches).")
app.add_typer(profiles_app, name="profiles")
app.add_typer(robots_app, name="robots")
app.add_typer(launchers_app, name="launchers")
app.add_typer(secrets_app, name="secrets")
robots_app.add_typer(options_app, name="options")
robots_app.add_typer(metrics_app, name="metrics")
robots_app.add_typer(executions_app, name="executions")
robots_app.add_typer(robot_secrets_app, name="secrets")
app.add_typer(warehouse_app, name="warehouse")
for _sub, _name in ((wh_types_app, "types"), (wh_fields_app, "fields"), (wh_folders_app, "folders"),
                    (wh_docs_app, "docs"), (wh_flows_app, "flows"), (wh_steps_app, "steps"),
                    (wh_approvers_app, "approvers"), (wh_approvals_app, "approvals"), (wh_links_app, "links")):
    warehouse_app.add_typer(_sub, name=_name)

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
    profile: Optional[str] = typer.Option(None, "--profile", help="Profile name (default: named after the company)."),
    with_key: bool = typer.Option(False, "--with-key", help="Paste an existing API key (hidden prompt or stdin) instead of the browser. For CI and scripts."),
    key: Optional[str] = typer.Option(None, "--key", help="Discouraged: ends up in shell history. Prefer --with-key."),
    no_browser: bool = typer.Option(False, "--no-browser", help="Only print the URL; do not try to open a browser."),
    json_out: bool = JsonOpt,
):
    """Log in through the browser: approve this computer in the Zibloh web app, no key to copy.

    Each company gets its own profile, so log in once per company and switch with `zibloh profiles use`."""
    resolved_base = None
    try:
        resolved_base = config.resolve_base_url(env, base_url)
        if key is not None or with_key:
            me = _login_with_key(key, env, base_url, resolved_base, profile)
        else:
            me = _login_with_browser(env if not base_url else None, resolved_base, profile, no_browser, json_out)
    except (ApiError, ops.OpError, config.ConfigError, browser_login.LoginFailed) as exc:
        _err(str(exc), json_out)
    _emit(me, json_out, lambda m: typer.echo(
        f"Logged in to {m['company_name']} ({m['base_url']}) as key '{m.get('api_key_name')}', profile '{m['profile']}' (now the default)."
        + ("" if m.get("self_serve") else "\nWarning: self-service is not enabled for this company; ask Zibloh to enable it.")
        + ("" if m.get("mcp_allowed") else "\nNote: this key has no MCP permission; the MCP tools (Claude Code plugin) will be refused.")
    ))


def _whoami_with(candidate: config.Profile) -> dict:
    c = Client(candidate, client_name="cli")
    c.tool = "login"
    try:
        return ops.whoami(c)
    finally:
        c.close()


def _login_with_key(key: Optional[str], env: str, base_url: Optional[str], resolved_base: str, profile: Optional[str]) -> dict:
    if key is None:
        key = sys.stdin.readline().strip() if not sys.stdin.isatty() else getpass.getpass("Zibloh API key: ").strip()
    if not key.startswith("zbl_"):
        raise ops.OpError("That does not look like a Zibloh API key (zbl_…)")
    me = _whoami_with(config.Profile(name=profile or "", base_url=resolved_base, api_key=key, env=env))
    name = profile or config.profile_name_for(me.get("company_id"), me.get("company_name"), env, base_url)
    config.save_profile(name, key, env, base_url, company_id=me.get("company_id"), company_name=me.get("company_name"))
    return {**me, "profile": name}


def _login_with_browser(env: Optional[str], resolved_base: str, profile: Optional[str], no_browser: bool, json_out: bool) -> dict:
    pending = browser_login.start(resolved_base, env, client="cli")
    opened = False if no_browser else browser_login.open_browser(pending.verification_url)
    say = typer.echo if not json_out else (lambda msg: typer.echo(msg, err=True))
    say(("Opened your browser at:" if opened else "Open this URL in your browser:") + f"\n\n    {pending.verification_url}\n")
    say(f"Check that the page shows the code {pending.user_code} and click Autorizar. Waiting…")
    result = browser_login.wait(pending, profile)
    return _whoami_with(config.load_profile(result["profile"]))


@app.command()
def logout(profile: Optional[str] = typer.Option(None, "--profile", help="Profile to forget (default: the default one)."),
           json_out: bool = JsonOpt):
    """Forget the saved key of a profile (revoke it in the web app if it leaked)."""
    try:
        profile = profile or config.list_profiles()["default_profile"]
    except config.ConfigError as exc:
        _err(str(exc), json_out)
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


@profiles_app.command("list")
def profiles_list(json_out: bool = JsonOpt):
    """Saved profiles (one per company login) and which one is the default."""
    def human(r):
        if not r["profiles"]:
            typer.echo("No profiles. Run `zibloh login`.")
        for name, p in r["profiles"].items():
            mark = "*" if name == r["active_profile"] else " "
            typer.echo(f"{mark} {name:<24} {p.get('company_name') or '?':<30} {p.get('env') or p.get('base_url')}")
    try:
        _emit(config.list_profiles(), json_out, human)
    except config.ConfigError as exc:
        _err(str(exc), json_out)


@profiles_app.command("use")
def profiles_use(profile: str, json_out: bool = JsonOpt):
    """Switch company: make PROFILE the default for the CLI and the MCP server. No new login."""
    try:
        config.use_profile(profile)
        result = {"default_profile": profile, **config.list_profiles()["profiles"][profile]}
    except config.ConfigError as exc:
        _err(str(exc), json_out)
    _emit(result, json_out, lambda r: typer.echo(f"Now using profile '{profile}' ({r.get('company_name') or r['base_url']})."
                                                 + (" ZIBLOH_PROFILE is set and still wins." if os.environ.get("ZIBLOH_PROFILE") else "")))


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
def robots_deploy(ctx: typer.Context, robot_id: str, directory: Path,
                  note: str = typer.Option(..., "--note", "-m", help="Why this version is uploaded (required; Know reads it)."),
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Validate, zip and upload the robot code (activates the robot). --note is required."""
    def human(r):
        for w in r["warnings"]:
            typer.secho(f"warning: {w}", fg=typer.colors.YELLOW)
        typer.echo(f"Deployed {r['robot_name']} ({r['files']} files, {r['size'] / 1e6:.1f} MB, sha256 {r['sha256'][:12]}…)")
    _run(ctx, profile, json_out, lambda c: ops.robots_deploy(c, robot_id, directory, note, _progress(json_out)), human)


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


# No `metrics set`: saved-time/money metrics are configured by Zibloh in the Backoffice.


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


# ── warehouse ─────────────────────────────────────────────────────────────────

def _json_arg(value: Optional[str], what: str) -> Any:
    """JSON given inline or as @path."""
    if value is None:
        return None
    try:
        text = Path(value[1:]).expanduser().read_text(encoding="utf-8") if value.startswith("@") else value
        return json.loads(text)
    except (OSError, ValueError) as exc:
        raise ops.OpError(f"{what}: invalid JSON ({exc})") from exc


def _field_specs(specs: List[str], fields_json: Optional[str]) -> list[dict]:
    """`--field "Name:type[:required]"` (repeatable) and/or `--fields JSON|@file` (tables with columns)."""
    fields = list(_json_arg(fields_json, "--fields") or [])
    for spec in specs:
        parts = [p.strip() for p in spec.split(":")]
        field = {"name": parts[0], "type": parts[1] if len(parts) > 1 and parts[1] else "text"}
        if len(parts) > 2:
            if parts[2] != "required":
                raise ops.OpError(f"--field {spec}: expected NAME[:TYPE][:required]")
            field["required"] = True
        fields.append(field)
    return fields


def _confirm(yes: bool, question: str) -> None:
    if not yes and sys.stdin.isatty():
        typer.confirm(question, abort=True)


FieldOpt = typer.Option([], "--field", help='NAME[:TYPE][:required]; TYPE: text, textarea, number, date, boolean, list, table.')
FieldsOpt = typer.Option(None, "--fields", help='JSON list (or @file) of {"name","type","required","columns"}.')
FiltersOpt = typer.Option(None, "--filters", help='JSON list (or @file) of column filters, see `docs list --help`.')
YesOpt = typer.Option(False, "--yes", help="Do not ask.")


def _types_human(r: dict) -> None:
    _table(r["types"], ["type_id", "path", "description"])


@wh_types_app.command("list")
def wh_types_list(ctx: typer.Context, folder: Optional[str] = typer.Option(None, "--folder"),
                  fields: bool = typer.Option(False, "--fields", help="Include each type's fields."),
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Document types and folders."""
    _run(ctx, profile, json_out, lambda c: warehouse.types_list(c, folder, fields), None if fields else _types_human)


@wh_types_app.command("get")
def wh_types_get(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE", help="Id, name or path."),
                 profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """A document type with its fields and approval flows."""
    _run(ctx, profile, json_out, lambda c: warehouse.types_get(c, type_ref))


@wh_types_app.command("create")
def wh_types_create(ctx: typer.Context, name: str,
                    path: Optional[str] = typer.Option(None, "--path", help='Folders + name, e.g. "Contabilidad/Facturas".'),
                    description: Optional[str] = typer.Option(None, "--description"),
                    field: List[str] = FieldOpt, fields: Optional[str] = FieldsOpt,
                    primary_key: List[str] = typer.Option([], "--primary-key", help="Field that identifies a document (repeatable)."),
                    status: List[str] = typer.Option([], "--status", help="Allowed custom status (repeatable)."),
                    file_type: Optional[str] = typer.Option(None, "--file-type", help="e.g. pdf"),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Create a document type."""
    _run(ctx, profile, json_out, lambda c: warehouse.types_create(
        c, name, path=path, description=description, fields=_field_specs(field, fields),
        primary_keys=primary_key or None, allowed_statuses=status or None, file_type=file_type))


@wh_types_app.command("update")
def wh_types_update(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"),
                    name: Optional[str] = typer.Option(None, "--name"),
                    description: Optional[str] = typer.Option(None, "--description"),
                    primary_key: List[str] = typer.Option([], "--primary-key"),
                    status: List[str] = typer.Option([], "--status"),
                    file_type: Optional[str] = typer.Option(None, "--file-type"),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Rename or reconfigure a document type (primary keys/statuses given replace the current ones)."""
    _run(ctx, profile, json_out, lambda c: warehouse.types_update(
        c, type_ref, name=name, description=description, primary_keys=primary_key or None,
        allowed_statuses=status or None, file_type=file_type))


@wh_types_app.command("archive")
def wh_types_archive(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"), yes: bool = YesOpt,
                     profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Archive a document type (documents are kept)."""
    _confirm(yes, f"Archive document type {type_ref}?")
    _run(ctx, profile, json_out, lambda c: warehouse.types_archive(c, type_ref))


@wh_fields_app.command("add")
def wh_fields_add(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"),
                  field: List[str] = FieldOpt, fields: Optional[str] = FieldsOpt,
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Add fields to a document type."""
    _run(ctx, profile, json_out, lambda c: warehouse.fields_add(c, type_ref, _field_specs(field, fields)))


@wh_fields_app.command("update")
def wh_fields_update(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"), field: str = typer.Argument(...),
                     new_name: Optional[str] = typer.Option(None, "--rename"),
                     field_type: Optional[str] = typer.Option(None, "--type"),
                     required: Optional[bool] = typer.Option(None, "--required/--optional"),
                     columns: Optional[str] = typer.Option(None, "--columns", help='JSON list (or @file) of {"name","type"} for tables.'),
                     migrate: bool = typer.Option(True, "--migrate/--no-migrate", help="On rename, move the value in every document."),
                     profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Rename a field, change its type or whether it is required."""
    _run(ctx, profile, json_out, lambda c: warehouse.fields_update(
        c, type_ref, field, new_name=new_name, type=field_type, required=required,
        columns=_json_arg(columns, "--columns"), migrate_documents=migrate))


@wh_fields_app.command("remove")
def wh_fields_remove(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"), field: str = typer.Argument(...),
                     yes: bool = YesOpt, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Remove a field from a document type."""
    _confirm(yes, f"Remove field {field} from {type_ref}?")
    _run(ctx, profile, json_out, lambda c: warehouse.fields_remove(c, type_ref, field))


@wh_folders_app.command("rename")
def wh_folders_rename(ctx: typer.Context, path: str, new_path: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Rename or move a folder with everything under it."""
    _run(ctx, profile, json_out, lambda c: warehouse.folders_rename(c, path, new_path))


@wh_folders_app.command("delete")
def wh_folders_delete(ctx: typer.Context, path: str, yes: bool = YesOpt, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """PERMANENTLY delete a folder with its types, documents, files, approvals and links."""
    _confirm(yes, f"Permanently delete folder {path} and everything in it?")
    _run(ctx, profile, json_out, lambda c: warehouse.folders_delete(c, path))


def _docs_human(r: dict) -> None:
    rows = [{**d, "approval": (d.get("approval") or {}).get("status")} for d in r["documents"]]
    _table(rows, ["document_id", "name", "status", "approval", "file_name", "created_time"])
    typer.echo(f"\n{len(rows)} of {r['total']}" + (" (more with --offset)" if r["has_more"] else ""))


@wh_docs_app.command("list")
def wh_docs_list(ctx: typer.Context, type_ref: Optional[str] = typer.Argument(None, metavar="[TYPE]"),
                 search: Optional[str] = typer.Option(None, "--search"),
                 status: Optional[str] = typer.Option(None, "--status", help="uploaded, missing_data, in_approval, approved, rejected, or a raw status."),
                 filters: Optional[str] = typer.Option(None, "--filters", help='JSON (or @file): [{"col": "content.Importe", "op": "range", "min": 100}, {"col": "...", "op": "in", "values": [...]}, {"col": "...", "op": "contains", "text": "..."}]'),
                 sort_by: Optional[str] = typer.Option(None, "--sort", help="created_time, name, created_by or content.<field>."),
                 desc: bool = typer.Option(False, "--desc"),
                 limit: int = typer.Option(50, "--limit"), offset: int = typer.Option(0, "--offset"),
                 archived: bool = typer.Option(False, "--archived", help="Include archived documents."),
                 profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Documents, a page at a time."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_list(
        c, type_ref, search=search, status=status, filters=_json_arg(filters, "--filters"), sort_by=sort_by,
        sort_dir="desc" if desc else "asc", limit=limit, offset=offset, include_archived=archived), _docs_human)


@wh_docs_app.command("get")
def wh_docs_get(ctx: typer.Context, document_id: str, history: bool = typer.Option(False, "--history"),
                profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """A document with its data (and its event history)."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_get(c, document_id, history))


@wh_docs_app.command("create")
def wh_docs_create(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"),
                   file: Optional[Path] = typer.Option(None, "--file", help="File to attach."),
                   name: Optional[str] = typer.Option(None, "--name"),
                   content: Optional[str] = typer.Option(None, "--content", help="JSON object (or @file) with the data."),
                   set_: List[str] = typer.Option([], "--set", help="FIELD=VALUE (text; repeatable)."),
                   status: Optional[str] = typer.Option(None, "--status"),
                   profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Create a document, optionally with its file."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_create(
        c, type_ref, name=name, content={**(_json_arg(content, "--content") or {}), **_parse_assignments(set_)},
        file=file.expanduser() if file else None, status=status))


@wh_docs_app.command("upload")
def wh_docs_upload(ctx: typer.Context, document_id: str, file: Path, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Attach (or replace) a document's file."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_upload(c, document_id, file.expanduser()))


@wh_docs_app.command("download")
def wh_docs_download(ctx: typer.Context, document_id: str, dest: str = typer.Argument(".", help="Directory (created if needed) or file path."),
                     profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Download a document's file."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_download(c, document_id, dest),
         lambda r: typer.echo(f"{r['path']} ({r['bytes']} bytes)"))


@wh_docs_app.command("update")
def wh_docs_update(ctx: typer.Context, document_id: str,
                   content: Optional[str] = typer.Option(None, "--content", help="JSON object (or @file) merged into the data."),
                   set_: List[str] = typer.Option([], "--set", help="FIELD=VALUE (text; repeatable)."),
                   replace: bool = typer.Option(False, "--replace", help="Replace the data instead of merging."),
                   name: Optional[str] = typer.Option(None, "--name"), status: Optional[str] = typer.Option(None, "--status"),
                   type_ref: Optional[str] = typer.Option(None, "--type", help="Move it to another document type."),
                   profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Edit a document's data, name, status or type."""
    def data():
        given = _json_arg(content, "--content")
        return {**(given or {}), **_parse_assignments(set_)} if set_ else given
    _run(ctx, profile, json_out, lambda c: warehouse.docs_update(
        c, document_id, content=data(), replace_content=replace, name=name, status=status, type_ref=type_ref))


@wh_docs_app.command("archive")
def wh_docs_archive(ctx: typer.Context, document_id: str, restore: bool = typer.Option(False, "--restore"),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Archive a document, or restore it."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_archive(c, document_id, restore))


@wh_docs_app.command("delete")
def wh_docs_delete(ctx: typer.Context, document_id: str, yes: bool = YesOpt, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Delete a document."""
    _confirm(yes, f"Delete document {document_id}?")
    _run(ctx, profile, json_out, lambda c: warehouse.docs_delete(c, document_id))


@wh_docs_app.command("export")
def wh_docs_export(ctx: typer.Context, type_ref: str = typer.Argument(..., metavar="TYPE"),
                   dest: str = typer.Argument(".", help="Directory (created if needed) or file path."),
                   files: bool = typer.Option(False, "--files", help="A ZIP with the files instead of a CSV with the data."),
                   search: Optional[str] = typer.Option(None, "--search"), status: Optional[str] = typer.Option(None, "--status"),
                   filters: Optional[str] = FiltersOpt, archived: bool = typer.Option(False, "--archived"),
                   profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Export a type's documents as CSV (data) or ZIP (files)."""
    _run(ctx, profile, json_out, lambda c: warehouse.docs_export(
        c, type_ref, dest, what="files" if files else "csv", search=search, status=status,
        filters=_json_arg(filters, "--filters"), include_archived=archived),
         lambda r: typer.echo(f"{r['path']} ({r['bytes']} bytes)"))


@wh_flows_app.command("list")
def wh_flows_list(ctx: typer.Context, type_ref: Optional[str] = typer.Argument(None, metavar="[TYPE]"),
                  inactive: bool = typer.Option(False, "--inactive", help="Include inactive flows."),
                  profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Approval flows."""
    _run(ctx, profile, json_out, lambda c: warehouse.flows_list(c, type_ref, inactive),
         lambda rows: _table(rows, ["flow_id", "name", "type", "is_active", "any_order"]))


@wh_flows_app.command("get")
def wh_flows_get(ctx: typer.Context, flow_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """A flow with its steps and approvers."""
    _run(ctx, profile, json_out, lambda c: warehouse.flows_get(c, flow_id))


@wh_flows_app.command("create")
def wh_flows_create(ctx: typer.Context, name: str, type_ref: str = typer.Argument(..., metavar="TYPE"),
                    steps: str = typer.Option(..., "--steps", help='JSON list (or @file) of {"name", "requirement": any|all|quorum, "quorum_count", "rule", "approvers": [emails]}.'),
                    any_order: bool = typer.Option(False, "--any-order"),
                    linked_type: List[str] = typer.Option([], "--linked-type", help="Type whose linked documents rules can use (repeatable)."),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Create an approval flow with its steps and approvers."""
    _run(ctx, profile, json_out, lambda c: warehouse.flows_create(
        c, name, type_ref, steps=_json_arg(steps, "--steps"), any_order=any_order, linked_types=linked_type or None))


@wh_flows_app.command("update")
def wh_flows_update(ctx: typer.Context, flow_id: str, name: Optional[str] = typer.Option(None, "--name"),
                    active: Optional[bool] = typer.Option(None, "--active/--inactive"),
                    any_order: Optional[bool] = typer.Option(None, "--any-order/--in-order"),
                    linked_type: List[str] = typer.Option([], "--linked-type"),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Rename, (de)activate or reconfigure a flow (deactivating cancels approvals in progress)."""
    _run(ctx, profile, json_out, lambda c: warehouse.flows_update(
        c, flow_id, name=name, active=active, any_order=any_order, linked_types=linked_type or None))


@wh_flows_app.command("archive")
def wh_flows_archive(ctx: typer.Context, flow_id: str, yes: bool = YesOpt, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Archive a flow (cancels its approvals in progress)."""
    _confirm(yes, f"Archive flow {flow_id}?")
    _run(ctx, profile, json_out, lambda c: warehouse.flows_archive(c, flow_id))


@wh_steps_app.command("add")
def wh_steps_add(ctx: typer.Context, flow_id: str, step: str = typer.Option(..., "--step", help="JSON object (or @file), same shape as in flows create."),
                 profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Add a step to a flow."""
    _run(ctx, profile, json_out, lambda c: warehouse.steps_add(c, flow_id, _json_arg(step, "--step")))


@wh_steps_app.command("update")
def wh_steps_update(ctx: typer.Context, step_id: str, changes: str = typer.Option(..., "--changes", help="JSON object (or @file)."),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Change a step (name, position, requirement, quorum_count, stage, auto_approve, rule)."""
    _run(ctx, profile, json_out, lambda c: warehouse.steps_update(c, step_id, _json_arg(changes, "--changes")))


@wh_steps_app.command("remove")
def wh_steps_remove(ctx: typer.Context, step_id: str, yes: bool = YesOpt, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Remove a step."""
    _confirm(yes, f"Remove step {step_id}?")
    _run(ctx, profile, json_out, lambda c: warehouse.steps_remove(c, step_id))


@wh_approvers_app.command("add")
def wh_approvers_add(ctx: typer.Context, step_id: str, who: str = typer.Argument(..., help="Email or user id."),
                     profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Make someone an approver of a step."""
    _run(ctx, profile, json_out, lambda c: warehouse.approvers_add(c, step_id, who))


@wh_approvers_app.command("remove")
def wh_approvers_remove(ctx: typer.Context, step_id: str, user_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Remove an approver from a step."""
    _run(ctx, profile, json_out, lambda c: warehouse.approvers_remove(c, step_id, user_id))


@wh_approvers_app.command("eligible")
def wh_approvers_eligible(ctx: typer.Context, type_ref: Optional[str] = typer.Argument(None, metavar="[TYPE]"),
                          profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """People who can approve documents of a type."""
    _run(ctx, profile, json_out, lambda c: warehouse.approvers_eligible(c, type_ref),
         lambda rows: _table(rows, ["user_id", "name"]))


@wh_approvals_app.command("inbox")
def wh_approvals_inbox(ctx: typer.Context, flow_id: Optional[str] = typer.Option(None, "--flow"),
                       bucket: str = typer.Option("my_turn", "--bucket", help="my_turn, pending_other or all."),
                       state: Optional[str] = typer.Option("pending", "--state", help="pending, approved or rejected."),
                       limit: int = typer.Option(50, "--limit"),
                       profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Documents waiting for your approval (or someone else's)."""
    def human(r):
        if not r["flows"]:
            typer.echo("(nothing)")
        for flow in r["flows"]:
            typer.echo(f"{flow['flow']} ({flow['total']})")
            _table(flow["documents"], ["document_id", "name", "status", "created_time"])
            typer.echo("")
    _run(ctx, profile, json_out, lambda c: warehouse.approvals_inbox(c, flow_id, bucket=bucket, state=state, limit=limit), human)


@wh_approvals_app.command("status")
def wh_approvals_status(ctx: typer.Context, document_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """A document's approval: steps, approvers and decisions."""
    _run(ctx, profile, json_out, lambda c: warehouse.approvals_status(c, document_id))


@wh_approvals_app.command("start")
def wh_approvals_start(ctx: typer.Context, document_id: str, flow_id: Optional[str] = typer.Option(None, "--flow"),
                       profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Send a document to approval."""
    _run(ctx, profile, json_out, lambda c: warehouse.approvals_start(c, document_id, flow_id))


@wh_approvals_app.command("decide")
def wh_approvals_decide(ctx: typer.Context, document_id: str,
                        decision: str = typer.Argument(..., help="approve, reject or request_changes."),
                        comment: Optional[str] = typer.Option(None, "--comment"),
                        step_id: Optional[str] = typer.Option(None, "--step"),
                        profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Approve, reject (with --comment) or request changes, as yourself."""
    _run(ctx, profile, json_out, lambda c: warehouse.approvals_decide(c, document_id, decision, comment, step_id))


@wh_approvals_app.command("force")
def wh_approvals_force(ctx: typer.Context, document_id: str, comment: Optional[str] = typer.Option(None, "--comment"),
                       yes: bool = YesOpt, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Approve every pending step at once (warehouse admins)."""
    _confirm(yes, f"Force-approve document {document_id}?")
    _run(ctx, profile, json_out, lambda c: warehouse.approvals_force(c, document_id, comment))


@wh_links_app.command("list")
def wh_links_list(ctx: typer.Context, document_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Documents linked to a primary document."""
    _run(ctx, profile, json_out, lambda c: warehouse.links_list(c, document_id))


@wh_links_app.command("create")
def wh_links_create(ctx: typer.Context, document_id: str, member_ids: List[str],
                    compare: List[str] = typer.Option([], "--compare", help="Field to compare (repeatable; default: primary keys)."),
                    profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Link documents to a primary one."""
    _run(ctx, profile, json_out, lambda c: warehouse.links_create(c, document_id, member_ids, compare or None))


@wh_links_app.command("remove")
def wh_links_remove(ctx: typer.Context, link_id: str, document_id: str, profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Unlink a document (the last one removes the link)."""
    _run(ctx, profile, json_out, lambda c: warehouse.links_remove(c, link_id, document_id))


@wh_links_app.command("mark")
def wh_links_mark(ctx: typer.Context, link_id: str, status: str = typer.Argument(..., help="correct, error or pending."),
                  note: Optional[str] = typer.Option(None, "--note"), profile: Optional[str] = ProfileOpt, json_out: bool = JsonOpt):
    """Resolve a link by hand."""
    _run(ctx, profile, json_out, lambda c: warehouse.links_mark(c, link_id, status, note))


def main() -> None:
    app()

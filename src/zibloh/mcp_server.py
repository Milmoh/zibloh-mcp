"""MCP server (stdio) for Claude Code: the same operations as the CLI, as `<module>_<action>` tools.

Secret values never pass through the model: `secrets_create`/`secrets_rotate` only take
`from_env` (a variable in this server's environment) or `from_file` (a path the user wrote),
and nothing returns a value.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from zibloh import __version__, config, ops
from zibloh.client import ApiError, Client

INSTRUCTIONS = """\
Zibloh robots: write, deploy, configure, run and debug RPA robots for the user's company.
Start with `whoami` to see the company and what this API key can do. Follow the
zibloh-workflow skill for the loop and zibloh-robot-authoring for the robot contract.
Never ask the user to paste a secret value into the chat: ask them to run
`zibloh secrets create NAME` in their own terminal, or use from_env/from_file.
Ask before running a robot against real data or one that sends emails; first runs use test_mode.
Paths are resolved against this server's working directory; prefer absolute paths."""

server = MCPServer(name="zibloh", version=__version__, instructions=INSTRUCTIONS)

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=True)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=True)
LOCAL = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)


def _call(tool: str, fn: Callable[[Client], Any]) -> Any:
    try:
        c = Client(config.load_profile(), client_name="mcp")
        c.tool = tool
        try:
            return fn(c)
        finally:
            c.close()
    except (ApiError, ops.OpError, config.ConfigError) as exc:
        raise ToolError(str(exc)) from exc


def _path(value: str) -> Path:
    return Path(value).expanduser().resolve()


def _local(fn: Callable[[], Any]) -> Any:
    try:
        return fn()
    except (ops.OpError, OSError) as exc:
        raise ToolError(str(exc)) from exc


def _secret_value(from_env: Optional[str], from_file: Optional[str]) -> bytes:
    if bool(from_env) == bool(from_file):
        raise ToolError("Pass exactly one of from_env or from_file. Literal values are not accepted: "
                        "ask the user to run `zibloh secrets create NAME` in their own terminal instead.")
    if from_env:
        if from_env not in os.environ:
            raise ToolError(f"{from_env} is not set in the MCP server's environment")
        return os.environ[from_env].encode("utf-8")
    try:
        return _path(from_file).read_bytes()
    except OSError as exc:
        raise ToolError(f"Cannot read {from_file}: {exc}") from exc


# ── identity and launchers ────────────────────────────────────────────────────

@server.tool(annotations=READ)
def whoami() -> dict[str, Any]:
    """Company, API key, scopes, enabled flags (self_serve, robot_secrets) and the capabilities this key has."""
    return _call("whoami", ops.whoami)


@server.tool(annotations=READ)
def launchers_list() -> dict[str, Any]:
    """The company's launchers (id, name, platform, status, execution type) and the execution types, including the cloud one."""
    return _call("launchers_list", ops.launchers_list)


# ── robots ────────────────────────────────────────────────────────────────────

@server.tool(annotations=READ)
def robots_list(search: Optional[str] = None) -> list[dict[str, Any]]:
    """Robots of the company, with status and where they run."""
    return _call("robots_list", lambda c: ops.robots_list(c, search))


@server.tool(annotations=READ)
def robots_get(robot_id: str) -> dict[str, Any]:
    """One robot with its options."""
    return _call("robots_get", lambda c: ops.robots_get(c, robot_id))


@server.tool(annotations=WRITE)
def robots_create(name: str, description: Optional[str] = None) -> dict[str, Any]:
    """Create an empty robot. It stays pending_executable until the first robots_deploy. Reuse the same robot and redeploy instead of creating new ones."""
    return _call("robots_create", lambda c: ops.robots_create(c, name, description))


@server.tool(annotations=DESTRUCTIVE)
def robots_delete(robot_id: str) -> dict[str, Any]:
    """Delete a robot. Only when the user explicitly asks for it."""
    return _call("robots_delete", lambda c: ops.robots_delete(c, robot_id))


@server.tool(annotations=WRITE)
def robots_target(robot_id: str, cloud: bool = False, launcher_id: Optional[str] = None,
                  execution_type_id: Optional[str] = None) -> dict[str, Any]:
    """Choose where the robot runs: cloud=true for the Zibloh cloud runner, or launcher_id for one of the company's launchers."""
    return _call("robots_target", lambda c: ops.robots_target(c, robot_id, cloud=cloud, launcher_id=launcher_id, execution_type_id=execution_type_id))


@server.tool(annotations=LOCAL)
def robots_init(directory: str, template: Literal["basic", "browser"] = "basic", force: bool = False) -> dict[str, Any]:
    """Scaffold a robot directory (main.py, config.json, requirements.txt) from the official template."""
    return _local(lambda: ops.robots_init(_path(directory), template, force))


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def robots_validate(directory: str, target: Optional[Literal["cloud", "onprem"]] = None) -> dict[str, Any]:
    """Check a robot directory: errors block a deploy, warnings explain runner limits."""
    return _local(lambda: ops.robots_validate(_path(directory), target))


@server.tool(annotations=WRITE)
def robots_deploy(robot_id: str, directory: str) -> dict[str, Any]:
    """Validate, zip and upload the robot's code; activates the robot. Fails if validation has errors."""
    return _call("robots_deploy", lambda c: ops.robots_deploy(c, robot_id, _path(directory)))


@server.tool(annotations=READ)
def robots_options_list(robot_id: str) -> list[dict[str, Any]]:
    """Options (configuration) of a robot with their values."""
    return _call("robots_options_list", lambda c: ops.options_list(c, robot_id))


@server.tool(annotations=WRITE)
def robots_options_define(robot_id: str, config_path: Optional[str] = None, options: Optional[list[dict]] = None) -> dict[str, Any]:
    """Declare options from a config.json (`opciones` list) or an explicit list of
    {name, option_type, data_type, default, value, display_name}. Existing names are left unchanged."""
    if bool(config_path) == bool(options):
        raise ToolError("Pass exactly one of config_path or options")
    if config_path:
        return _call("robots_options_define", lambda c: ops.options_define_from_config(c, robot_id, _path(config_path)))
    return _call("robots_options_define", lambda c: ops.options_define(c, robot_id, options))


@server.tool(annotations=WRITE)
def robots_options_set(robot_id: str, values: dict[str, str]) -> dict[str, Any]:
    """Set values of existing options by name, e.g. {"test_mode": "true"}. Never put secret values in options."""
    return _call("robots_options_set", lambda c: ops.options_set(c, robot_id, values))


@server.tool(annotations=READ)
def robots_metrics_get(robot_id: str) -> list[dict[str, Any]]:
    """Minutes/euros saved metrics of a robot."""
    return _call("robots_metrics_get", lambda c: ops.metrics_get(c, robot_id))


@server.tool(annotations=WRITE)
def robots_metrics_set(robot_id: str, minutes: Optional[float] = None, euros: Optional[float] = None,
                       scope: Literal["execution", "process"] = "execution") -> dict[str, Any]:
    """Minutes and/or euros saved per run (scope=execution) or per processed unit (scope=process)."""
    return _call("robots_metrics_set", lambda c: ops.metrics_set(c, robot_id, minutes, euros, scope))


# ── executions ────────────────────────────────────────────────────────────────

@server.tool(annotations=WRITE)
def robots_run(robot_id: str, wait: bool = False, timeout_seconds: int = 300, tail: int = 50) -> dict[str, Any]:
    """Launch a robot. With wait=true, block until it ends (up to timeout_seconds) and return the
    status and the last `tail` log lines; otherwise poll robots_executions_get. Ask the user
    before runs that touch real data or send emails."""
    def fn(c):
        launched = ops.robots_run(c, robot_id)
        if not wait:
            return launched
        result = ops.wait_for_execution(c, launched["robot_execution_id"], timeout=timeout_seconds, tail=tail)
        result["launch"] = launched
        return result
    return _call("robots_run", fn)


@server.tool(annotations=READ)
def robots_executions_list(robot_id: str, limit: int = 20) -> list[dict[str, Any]]:
    """Recent executions of a robot."""
    return _call("robots_executions_list", lambda c: ops.executions_list(c, robot_id, limit))


@server.tool(annotations=READ)
def robots_executions_get(execution_id: str) -> dict[str, Any]:
    """Current status (is_terminal tells if it ended) and status history of an execution."""
    return _call("robots_executions_get", lambda c: ops.execution_get(c, execution_id))


@server.tool(annotations=READ)
def robots_executions_logs(execution_id: str, tail: Optional[int] = 200) -> dict[str, Any]:
    """Log of an execution (last `tail` lines). Available once the run ends."""
    def fn(c):
        log = ops.execution_logs(c, execution_id, tail)
        if log is None:
            raise ops.OpError("The log is not uploaded yet: the runner uploads it when the execution ends")
        return log
    return _call("robots_executions_logs", fn)


@server.tool(annotations=DESTRUCTIVE)
def robots_executions_cancel(execution_id: str) -> dict[str, Any]:
    """Cancel a queued execution or ask a running one to stop."""
    return _call("robots_executions_cancel", lambda c: ops.execution_cancel(c, execution_id))


# ── secrets ───────────────────────────────────────────────────────────────────

@server.tool(annotations=READ)
def secrets_list() -> list[dict[str, Any]]:
    """Company secrets: names and kinds only. Values are never returned."""
    return _call("secrets_list", ops.secrets_list)


@server.tool(annotations=WRITE)
def secrets_create(name: str, from_env: Optional[str] = None, from_file: Optional[str] = None,
                   kind: Literal["env", "file"] = "env", description: Optional[str] = None) -> dict[str, Any]:
    """Create a secret (NAME in ^[A-Z0-9_]+$) from an environment variable of this server or a file.
    For values the user must type, ask them to run `zibloh secrets create NAME` in their terminal instead."""
    value = _secret_value(from_env, from_file)
    return _call("secrets_create", lambda c: ops.secrets_create(c, name, value, kind=kind, description=description))


@server.tool(annotations=WRITE)
def secrets_rotate(name: str, from_env: Optional[str] = None, from_file: Optional[str] = None) -> dict[str, Any]:
    """Replace a secret's value from an environment variable of this server or a file."""
    value = _secret_value(from_env, from_file)
    return _call("secrets_rotate", lambda c: ops.secrets_rotate(c, name, value))


@server.tool(annotations=DESTRUCTIVE)
def secrets_delete(name: str) -> dict[str, Any]:
    """Deactivate a secret. Unbind it from every robot first."""
    return _call("secrets_delete", lambda c: ops.secrets_delete(c, name))


@server.tool(annotations=READ)
def robots_secrets_list(robot_id: str) -> list[dict[str, Any]]:
    """Secrets bound to a robot and how each is exposed (env var or file)."""
    return _call("robots_secrets_list", lambda c: ops.robot_secrets_list(c, robot_id))


@server.tool(annotations=WRITE)
def robots_secrets_bind(robot_id: str, name: str, expose_as: Optional[Literal["env", "file"]] = None,
                        env_name: Optional[str] = None, filename: Optional[str] = None, required: bool = False) -> list[dict[str, Any]]:
    """Give a robot access to a secret: as env var `env_name` (default: the secret name), or for
    file secrets a file whose path is in env var `env_name`."""
    return _call("robots_secrets_bind", lambda c: ops.robot_secrets_bind(c, robot_id, name, expose_as=expose_as, env_name=env_name, filename=filename, required=required))


@server.tool(annotations=WRITE)
def robots_secrets_unbind(robot_id: str, name: str) -> list[dict[str, Any]]:
    """Remove a robot's access to a secret."""
    return _call("robots_secrets_unbind", lambda c: ops.robot_secrets_unbind(c, robot_id, name))


def run() -> None:
    import logging

    # httpx logs every request at INFO; the MCP client shows stderr as server logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")

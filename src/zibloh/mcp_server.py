"""MCP server (stdio) for Claude Code: the same operations as the CLI, as `<module>_<action>` tools.

Secret values never pass through the model: `secrets_create`/`secrets_rotate` only take
`from_env` (a variable in this server's environment) or `from_file` (a path the user wrote),
and nothing returns a value.
"""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from typing_extensions import NotRequired, TypedDict

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from zibloh import __version__, browser_login, config, ops, warehouse
from zibloh.client import ApiError, Client

INSTRUCTIONS = """\
Zibloh robots: write, deploy, configure, run and debug RPA robots for the user's company.
Start with `whoami` to see the company and what this API key can do. If it says the user is
not logged in, call `auth_login`, give the user the URL and code it returns, and after they
approve in the browser call `auth_status`. Never ask the user for an API key.
Each API key belongs to one company. To work on another company, `profiles_list` the saved
logins and `profiles_use` one; only call `auth_login` for a company with no profile yet. Follow the
zibloh-workflow skill for the loop and zibloh-robot-authoring for the robot contract.
Never ask the user to paste a secret value into the chat: ask them to run
`zibloh secrets create NAME` in their own terminal, or use from_env/from_file.
Ask before running a robot against real data or one that sends emails; first runs use test_mode.
Warehouse (document types and their fields, documents and files, approval flows, approvals,
linked documents): the `warehouse_*` tools, following the zibloh-warehouse skill. A type can be
given by id, name or path. Approve or reject only when the user decides it, never on your own.
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


# ── login ─────────────────────────────────────────────────────────────────────

# One browser login at a time per server process. The poll runs in a thread so the
# tool call returns at once with the URL; `auth_status` reports how it ended.
_login_lock = threading.Lock()
_login: dict[str, Any] = {"state": "none"}


def _login_worker(pending: browser_login.Pending, profile: str) -> None:
    try:
        result = browser_login.wait(pending, profile)
        state = {"state": "logged_in", **result}
    except browser_login.LoginFailed as exc:
        state = {"state": "failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 - surface anything through auth_status
        state = {"state": "failed", "error": f"{type(exc).__name__}: {exc}"}
    with _login_lock:
        if _login.get("device_code") == pending.device_code:
            _login.clear()
            _login.update(state)


@server.tool(annotations=LOCAL)
def auth_login(env: Literal["prod", "dev"] = "prod", profile: Optional[str] = None) -> dict[str, Any]:
    """Log this computer in through the browser. Returns a URL and a code: show both to the user,
    ask them to open the URL (it may already be open), check the code, pick the company and click
    Autorizar, then call auth_status. The key is saved as its own profile (named after the
    company unless `profile` is given) and becomes the default; other companies' profiles are kept.
    The API key goes straight to the local credentials file; it never passes through the
    conversation. dev is Milmoh's test platform; customers use prod."""
    try:
        pending = browser_login.start(config.resolve_base_url(env), env, client="mcp")
    except (browser_login.LoginFailed, config.ConfigError) as exc:
        raise ToolError(str(exc)) from exc
    with _login_lock:
        _login.clear()
        _login.update({"state": "pending", "device_code": pending.device_code, "user_code": pending.user_code,
                       "verification_url": pending.verification_url, "profile": profile, "env": env})
    threading.Thread(target=_login_worker, args=(pending, profile), daemon=True).start()
    opened = browser_login.open_browser(pending.verification_url)
    return {"verification_url": pending.verification_url, "user_code": pending.user_code,
            "browser_opened": opened, "expires_in_seconds": int(pending.expires_at - time.monotonic()),
            "next": "Ask the user to approve in the browser, then call auth_status."}


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def auth_status(wait_seconds: int = 0) -> dict[str, Any]:
    """State of the last auth_login: pending, logged_in (with company and key name) or failed.
    wait_seconds (max 120) waits for the approval before answering."""
    deadline = time.monotonic() + max(0, min(wait_seconds, 120))
    while True:
        with _login_lock:
            state = {k: v for k, v in _login.items() if k != "device_code"}
        if state["state"] != "pending" or time.monotonic() >= deadline:
            return state
        time.sleep(1)


@server.tool(annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False))
def profiles_list() -> dict[str, Any]:
    """Saved logins on this computer, one per company (never their keys), and the active one."""
    try:
        return config.list_profiles()
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from exc


@server.tool(annotations=LOCAL)
def profiles_use(profile: str) -> dict[str, Any]:
    """Switch company without logging in again: make `profile` (from profiles_list) the default.
    Every following tool call, and the CLI, use it. Tell the user which company is now active."""
    try:
        config.use_profile(profile)
        profiles = config.list_profiles()
    except config.ConfigError as exc:
        raise ToolError(str(exc)) from exc
    result = {"default_profile": profile, **profiles["profiles"][profile]}
    if profiles["active_profile"] != profile:
        result["warning"] = f"ZIBLOH_PROFILE={profiles['active_profile']} is set in this server's environment and still wins."
    return result


# ── identity and launchers ────────────────────────────────────────────────────

@server.tool(annotations=READ)
def whoami() -> dict[str, Any]:
    """Company, API key, scopes, enabled flags (self_serve, robot_secrets, warehouse, warehouse_approvals) and the robot capabilities this key has."""
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
def robots_deploy(robot_id: str, directory: str, note: str) -> dict[str, Any]:
    """Validate, zip and upload the robot's code; activates the robot. Fails if validation has errors.
    `note` is required: what changes in this version and why (at least 10 characters). It is kept in the robot's
    deploy history and Zibloh Know reads it, so write it for a person, not a commit hash."""
    return _call("robots_deploy", lambda c: ops.robots_deploy(c, robot_id, _path(directory), note))


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


# No robots_metrics_set: metrics are configured by Zibloh in the Backoffice.


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


# ── warehouse ───────────────────────────────────────────────────────────────────

FieldType = Literal["text", "textarea", "number", "date", "boolean", "list", "table"]
ColumnType = Literal["text", "textarea", "number", "date", "boolean"]


class Column(TypedDict):
    name: str
    type: ColumnType


class Field(TypedDict):
    name: str
    type: FieldType
    required: NotRequired[bool]
    columns: NotRequired[list[Column]]  # only for type "table": the columns of each line


class Step(TypedDict):
    name: str
    requirement: NotRequired[Literal["any", "all", "quorum"]]  # who must approve: one, all, or quorum_count
    quorum_count: NotRequired[int]
    stage: NotRequired[Literal["sequential", "sequential_sum"]]
    auto_approve: NotRequired[bool]
    rule: NotRequired[dict]  # {"conditions": [{"field", "op", "value"}, {"connector": "y"|"o", ...}]}; none = always applies
    approvers: NotRequired[list[str]]  # emails or user_ids
    position: NotRequired[int]


class StepChanges(TypedDict, total=False):
    name: str
    position: int
    requirement: Literal["any", "all", "quorum"]
    quorum_count: int
    stage: Literal["sequential", "sequential_sum"]
    auto_approve: bool
    rule: Optional[dict]


DocStatus = Literal["uploaded", "missing_data", "in_approval", "approved", "rejected"]


@server.tool(annotations=READ)
def warehouse_types_list(folder: Optional[str] = None, with_fields: bool = False) -> dict[str, Any]:
    """Document types (the web app's lists) and folders. A type's path is its folder plus its
    name, e.g. "Contabilidad/Facturas". with_fields adds each type's fields."""
    return _call("warehouse_types_list", lambda c: warehouse.types_list(c, folder, with_fields))


@server.tool(annotations=READ)
def warehouse_types_get(type: str) -> dict[str, Any]:
    """One document type (id, name or path): fields, primary keys, allowed statuses and approval flows."""
    return _call("warehouse_types_get", lambda c: warehouse.types_get(c, type))


@server.tool(annotations=WRITE)
def warehouse_types_create(name: str, fields: list[Field], path: Optional[str] = None, description: Optional[str] = None,
                           primary_keys: Optional[list[str]] = None, allowed_statuses: Optional[list[str]] = None,
                           file_type: Optional[str] = None) -> dict[str, Any]:
    """Create a document type. `path` places it in folders ("Contabilidad/Facturas"; folders are
    created on the fly); without it, it sits at the root. Names are unique per company, even
    archived ones. primary_keys: fields that identify a document (used to link documents)."""
    return _call("warehouse_types_create", lambda c: warehouse.types_create(
        c, name, path=path, description=description, fields=list(fields), primary_keys=primary_keys,
        allowed_statuses=allowed_statuses, file_type=file_type))


@server.tool(annotations=WRITE)
def warehouse_types_update(type: str, name: Optional[str] = None, description: Optional[str] = None,
                           primary_keys: Optional[list[str]] = None, allowed_statuses: Optional[list[str]] = None,
                           file_type: Optional[str] = None) -> dict[str, Any]:
    """Rename or reconfigure a document type. Fields: warehouse_fields_*. Move it: warehouse_folders_rename."""
    return _call("warehouse_types_update", lambda c: warehouse.types_update(
        c, type, name=name, description=description, primary_keys=primary_keys,
        allowed_statuses=allowed_statuses, file_type=file_type))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_types_archive(type: str) -> dict[str, Any]:
    """Archive a document type: it disappears from the lists, its documents are kept and its name stays taken."""
    return _call("warehouse_types_archive", lambda c: warehouse.types_archive(c, type))


@server.tool(annotations=WRITE)
def warehouse_fields_add(type: str, fields: list[Field]) -> dict[str, Any]:
    """Add fields to a document type, after the existing ones."""
    return _call("warehouse_fields_add", lambda c: warehouse.fields_add(c, type, list(fields)))


@server.tool(annotations=WRITE)
def warehouse_fields_update(type: str, field: str, new_name: Optional[str] = None, field_type: Optional[FieldType] = None,
                            required: Optional[bool] = None, columns: Optional[list[Column]] = None,
                            migrate_documents: bool = True) -> dict[str, Any]:
    """Rename a field, change its type, make it (not) required or replace a table's columns.
    A rename also moves the value in every document (migrate_documents); approved documents are
    locked and reported as skipped."""
    return _call("warehouse_fields_update", lambda c: warehouse.fields_update(
        c, type, field, new_name=new_name, type=field_type, required=required,
        columns=list(columns) if columns is not None else None, migrate_documents=migrate_documents))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_fields_remove(type: str, field: str) -> dict[str, Any]:
    """Remove a field from a document type (documents keep the old value, hidden)."""
    return _call("warehouse_fields_remove", lambda c: warehouse.fields_remove(c, type, field))


@server.tool(annotations=WRITE)
def warehouse_folders_rename(path: str, new_path: str) -> dict[str, Any]:
    """Rename or move a folder (or a single type's path) with everything under it."""
    return _call("warehouse_folders_rename", lambda c: warehouse.folders_rename(c, path, new_path))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_folders_delete(path: str) -> dict[str, Any]:
    """PERMANENTLY delete a folder with every type, document, file, approval and link under it.
    Only when the user explicitly asks for it, after telling them what it contains."""
    return _call("warehouse_folders_delete", lambda c: warehouse.folders_delete(c, path))


@server.tool(annotations=READ)
def warehouse_docs_list(type: Optional[str] = None, search: Optional[str] = None, status: Optional[str] = None,
                        filters: Optional[list[dict]] = None, sort_by: Optional[str] = None,
                        sort_dir: Literal["asc", "desc"] = "asc", limit: int = 50, offset: int = 0,
                        include_archived: bool = False) -> dict[str, Any]:
    """Documents, newest first, a page at a time (limit <= 100; has_more tells if there are more).
    status: uploaded | missing_data | in_approval | approved | rejected (needs type), or a raw
    status of the type. search matches name and author. sort_by: created_time, name, created_by
    or content.<field>. filters (AND): {"col": "content.<field>", "op": "in", "values": [...]},
    {"col", "op": "contains", "text"}, {"col", "op": "range", "min", "max"} (numbers, YYYY-MM-DD dates)."""
    return _call("warehouse_docs_list", lambda c: warehouse.docs_list(
        c, type, search=search, status=status, filters=filters, sort_by=sort_by, sort_dir=sort_dir,
        limit=limit, offset=offset, include_archived=include_archived))


@server.tool(annotations=READ)
def warehouse_docs_get(document_id: str, history: bool = False) -> dict[str, Any]:
    """One document: data, status, file name, approval progress; history=true adds its event log."""
    return _call("warehouse_docs_get", lambda c: warehouse.docs_get(c, document_id, history))


@server.tool(annotations=WRITE)
def warehouse_docs_create(type: str, content: Optional[dict[str, Any]] = None, name: Optional[str] = None,
                          file: Optional[str] = None, status: Optional[str] = None) -> dict[str, Any]:
    """Create a document of a type with its data (keys = field names) and, optionally, a local
    file to attach (absolute path). Dates are YYYY-MM-DD; tables are lists of objects. Missing
    required fields are allowed (the document shows as missing_data)."""
    return _call("warehouse_docs_create", lambda c: warehouse.docs_create(
        c, type, name=name, content=content, file=_path(file) if file else None, status=status))


@server.tool(annotations=WRITE)
def warehouse_docs_upload(document_id: str, file: str) -> dict[str, Any]:
    """Attach a local file to a document, replacing its current file."""
    return _call("warehouse_docs_upload", lambda c: warehouse.docs_upload(c, document_id, _path(file)))


@server.tool(annotations=LOCAL)
def warehouse_docs_download(document_id: str, dest: str) -> dict[str, Any]:
    """Save a document's file locally: dest is a directory (keeps the original name) or a file path.
    Returns the path; read the file from there if its content is needed."""
    return _call("warehouse_docs_download", lambda c: warehouse.docs_download(c, document_id, dest))


@server.tool(annotations=WRITE)
def warehouse_docs_update(document_id: str, content: Optional[dict[str, Any]] = None, replace_content: bool = False,
                          name: Optional[str] = None, status: Optional[str] = None,
                          type: Optional[str] = None) -> dict[str, Any]:
    """Edit a document. content is merged into its data (null clears a field) unless
    replace_content. Approved documents are locked."""
    return _call("warehouse_docs_update", lambda c: warehouse.docs_update(
        c, document_id, content=content, replace_content=replace_content, name=name, status=status, type_ref=type))


@server.tool(annotations=WRITE)
def warehouse_docs_archive(document_id: str, restore: bool = False) -> dict[str, Any]:
    """Archive a document (still visible with include_archived), or restore it with restore=true."""
    return _call("warehouse_docs_archive", lambda c: warehouse.docs_archive(c, document_id, restore))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_docs_delete(document_id: str) -> dict[str, Any]:
    """Delete a document (it disappears from every view). Only when the user asks; prefer archiving."""
    return _call("warehouse_docs_delete", lambda c: warehouse.docs_delete(c, document_id))


@server.tool(annotations=LOCAL)
def warehouse_docs_export(type: str, dest: str, what: Literal["csv", "files"] = "csv", search: Optional[str] = None,
                          status: Optional[str] = None, filters: Optional[list[dict]] = None,
                          include_archived: bool = False) -> dict[str, Any]:
    """Export the documents of a type that match the filters (same as warehouse_docs_list):
    what=csv saves their data as CSV, what=files a ZIP with their files (max 500). dest is a
    directory or a file path."""
    return _call("warehouse_docs_export", lambda c: warehouse.docs_export(
        c, type, dest, what=what, search=search, status=status, filters=filters,
        include_archived=include_archived))


@server.tool(annotations=READ)
def warehouse_flows_list(type: Optional[str] = None, include_inactive: bool = False) -> list[dict[str, Any]]:
    """Approval flows, optionally of one document type."""
    return _call("warehouse_flows_list", lambda c: warehouse.flows_list(c, type, include_inactive))


@server.tool(annotations=READ)
def warehouse_flows_get(flow_id: str) -> dict[str, Any]:
    """An approval flow with its steps, rules and approvers."""
    return _call("warehouse_flows_get", lambda c: warehouse.flows_get(c, flow_id))


@server.tool(annotations=WRITE)
def warehouse_flows_create(name: str, type: str, steps: list[Step], any_order: bool = False,
                           linked_types: Optional[list[str]] = None) -> dict[str, Any]:
    """Create an approval flow for a document type with its steps (in order) and approvers.
    A step applies to a document when its rule holds (no rule: always); steps whose rule fails
    are skipped. Rule ops: eq, neq, gt, lt, gte, lte, contains, not_contains, starts_with,
    ends_with, in, not_in; conditions after the first carry "connector": "y" (and) or "o" (or),
    evaluated left to right. linked_types: types whose linked documents rules can use
    ({"type": "linked_sum"|"linked_match", ...}). any_order lets steps be approved in any order.
    Approvers need at least read access to the type (see warehouse_approvers_eligible)."""
    return _call("warehouse_flows_create", lambda c: warehouse.flows_create(
        c, name, type, steps=[dict(s) for s in steps], any_order=any_order, linked_types=linked_types))


@server.tool(annotations=WRITE)
def warehouse_flows_update(flow_id: str, name: Optional[str] = None, active: Optional[bool] = None,
                           any_order: Optional[bool] = None, linked_types: Optional[list[str]] = None) -> dict[str, Any]:
    """Rename, (de)activate or reconfigure a flow. Deactivating cancels its approvals in progress."""
    return _call("warehouse_flows_update", lambda c: warehouse.flows_update(
        c, flow_id, name=name, active=active, any_order=any_order, linked_types=linked_types))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_flows_archive(flow_id: str) -> dict[str, Any]:
    """Archive a flow; its approvals in progress are cancelled (finished ones are kept)."""
    return _call("warehouse_flows_archive", lambda c: warehouse.flows_archive(c, flow_id))


@server.tool(annotations=WRITE)
def warehouse_steps_add(flow_id: str, step: Step) -> dict[str, Any]:
    """Add a step to a flow (last unless position is given), with its approvers."""
    return _call("warehouse_steps_add", lambda c: warehouse.steps_add(c, flow_id, dict(step)))


@server.tool(annotations=WRITE)
def warehouse_steps_update(step_id: str, changes: StepChanges) -> dict[str, Any]:
    """Change a step's name, position, requirement, quorum, stage, auto_approve or rule."""
    return _call("warehouse_steps_update", lambda c: warehouse.steps_update(c, step_id, dict(changes)))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_steps_remove(step_id: str) -> dict[str, Any]:
    """Remove a step from its flow."""
    return _call("warehouse_steps_remove", lambda c: warehouse.steps_remove(c, step_id))


@server.tool(annotations=WRITE)
def warehouse_approvers_add(step_id: str, who: str) -> dict[str, Any]:
    """Make a person (email or user_id) an approver of a step."""
    return _call("warehouse_approvers_add", lambda c: warehouse.approvers_add(c, step_id, who))


@server.tool(annotations=WRITE)
def warehouse_approvers_remove(step_id: str, user_id: str) -> dict[str, Any]:
    """Remove an approver from a step."""
    return _call("warehouse_approvers_remove", lambda c: warehouse.approvers_remove(c, step_id, user_id))


@server.tool(annotations=READ)
def warehouse_approvers_eligible(type: Optional[str] = None) -> list[dict[str, Any]]:
    """People who can be approvers for a document type (they can read it)."""
    return _call("warehouse_approvers_eligible", lambda c: warehouse.approvers_eligible(c, type))


@server.tool(annotations=READ)
def warehouse_approvals_inbox(flow_id: Optional[str] = None,
                              bucket: Literal["my_turn", "pending_other", "all"] = "my_turn",
                              state: Optional[Literal["pending", "approved", "rejected"]] = "pending",
                              limit: int = 50) -> dict[str, Any]:
    """Documents in approval flows: my_turn = waiting for the logged-in person's decision. It also
    lists documents not sent to approval yet whose first step is theirs (no `approval` field)."""
    return _call("warehouse_approvals_inbox", lambda c: warehouse.approvals_inbox(
        c, flow_id, bucket=bucket, state=state, limit=limit))


@server.tool(annotations=READ)
def warehouse_approvals_status(document_id: str) -> dict[str, Any]:
    """A document's approval: status, each step with its approvers, and the decisions so far."""
    return _call("warehouse_approvals_status", lambda c: warehouse.approvals_status(c, document_id))


@server.tool(annotations=WRITE)
def warehouse_approvals_start(document_id: str, flow_id: Optional[str] = None) -> dict[str, Any]:
    """Send a document to approval (its type's active flow unless flow_id). Required fields must be filled."""
    return _call("warehouse_approvals_start", lambda c: warehouse.approvals_start(c, document_id, flow_id))


@server.tool(annotations=WRITE)
def warehouse_approvals_decide(document_id: str, decision: Literal["approve", "reject", "request_changes"],
                               comment: Optional[str] = None, step_id: Optional[str] = None) -> dict[str, Any]:
    """Record the logged-in person's decision on a document's approval (reject needs a comment).
    Only when the user has decided it explicitly for this document."""
    return _call("warehouse_approvals_decide", lambda c: warehouse.approvals_decide(
        c, document_id, decision, comment, step_id))


@server.tool(annotations=DESTRUCTIVE)
def warehouse_approvals_force(document_id: str, comment: Optional[str] = None) -> dict[str, Any]:
    """Approve every pending step at once, skipping approvers and order (warehouse admins). Only on explicit request."""
    return _call("warehouse_approvals_force", lambda c: warehouse.approvals_force(c, document_id, comment))


@server.tool(annotations=READ)
def warehouse_links_list(document_id: str) -> list[dict[str, Any]]:
    """Documents linked to a primary document (e.g. an invoice's delivery notes) and whether their fields match."""
    return _call("warehouse_links_list", lambda c: warehouse.links_list(c, document_id))


@server.tool(annotations=WRITE)
def warehouse_links_create(document_id: str, member_ids: list[str], fields: Optional[list[str]] = None) -> dict[str, Any]:
    """Link documents of the same folder to a primary one, comparing `fields` (default: the
    primary keys). Adds to the existing link if there is one."""
    return _call("warehouse_links_create", lambda c: warehouse.links_create(c, document_id, member_ids, fields))


@server.tool(annotations=WRITE)
def warehouse_links_remove(link_id: str, document_id: str) -> dict[str, Any]:
    """Unlink one document; unlinking the last one removes the link."""
    return _call("warehouse_links_remove", lambda c: warehouse.links_remove(c, link_id, document_id))


@server.tool(annotations=WRITE)
def warehouse_links_mark(link_id: str, status: Literal["correct", "error", "pending"], note: Optional[str] = None) -> dict[str, Any]:
    """Resolve a link by hand (overridden again if its members change)."""
    return _call("warehouse_links_mark", lambda c: warehouse.links_mark(c, link_id, status, note))


def run() -> None:
    import logging

    # httpx logs every request at INFO; the MCP client shows stderr as server logs.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")

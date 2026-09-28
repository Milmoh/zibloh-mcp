"""Operations shared by the CLI commands and the MCP tools.

Each function takes a `Client`, calls the API and returns plain data (dicts/lists) that
both front ends print or return as-is. Secret values only enter through
`secrets_create`/`secrets_rotate` and are never returned.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Callable, Optional

from zibloh import packaging
from zibloh.client import ApiError, Client

ROBOTS = "/automate/robots"
EXECUTIONS = "/robot-execution"
MULTIPART_THRESHOLD = 64 * 1024 * 1024
ZIP_CONTENT_TYPE = "application/zip"


class OpError(Exception):
    """A failure the user can act on (bad input, missing resource...)."""


# ── identity and launchers ────────────────────────────────────────────────────

def whoami(c: Client) -> dict:
    data = c.get("/self-serve/whoami")
    data["base_url"] = c.profile.base_url
    data["profile"] = c.profile.name
    return data


def launchers_list(c: Client) -> dict:
    launchers = c.get("/launcher/available", params={"limit": 500})
    types = c.get("/launcher/execution-types")
    return {"launchers": launchers["items"], "execution_types": types}


def _cloud_execution_type(c: Client, execution_type_id: Optional[str]) -> dict:
    types = c.get("/launcher/execution-types")
    if execution_type_id:
        match = [t for t in types if t["execution_type_id"] == execution_type_id]
        if not match:
            raise OpError(f"Unknown execution type {execution_type_id}")
        return match[0]
    cloud = [t for t in types if (t.get("tipology") or "").strip().lower() == "cloud"]
    if len(cloud) != 1:
        names = ", ".join(f"{t['name']} ({t['execution_type_id']})" for t in cloud) or "none"
        raise OpError(f"Expected one cloud execution type, found: {names}. Pass --execution-type.")
    return cloud[0]


# ── robots ────────────────────────────────────────────────────────────────────

def robots_list(c: Client, search: Optional[str] = None, limit: int = 100) -> list:
    data = c.get(f"{ROBOTS}/", params={"search": search, "limit": limit, "skip": 0})
    return [_robot_summary(r) for r in data["items"]]


def _robot_summary(r: dict) -> dict:
    return {
        "robot_id": r["robot_id"],
        "name": r["name"],
        "status": r["status"],
        "execution": r.get("current_execution_status"),
        "target": r.get("execution_type_name") or r.get("execution_tipology"),
        "launcher_id": r.get("launcher_id"),
        "launcher_name": r.get("launcher_name"),
        "last_exec": r.get("last_exec"),
        "successful_executions": r.get("successful_executions"),
        "failure_executions": r.get("failure_executions"),
    }


def robots_get(c: Client, robot_id: str) -> dict:
    return c.get(f"{ROBOTS}/{robot_id}")


def robots_create(c: Client, name: str, description: Optional[str] = None) -> dict:
    return c.post(f"{ROBOTS}/", json={"name": name, "description": description})


def robots_delete(c: Client, robot_id: str) -> dict:
    return c.delete(f"{ROBOTS}/{robot_id}")


def robots_target(c: Client, robot_id: str, *, cloud: bool = False, launcher_id: Optional[str] = None,
                  execution_type_id: Optional[str] = None) -> dict:
    """Where the robot runs: the cloud runner or one launcher of the company."""
    if cloud == bool(launcher_id):
        raise OpError("Choose exactly one of --cloud or --launcher")
    if cloud:
        et = _cloud_execution_type(c, execution_type_id)
        # Unpin first: a pinned launcher must share the robot's execution type.
        c.patch(f"{ROBOTS}/{robot_id}/launcher", json={"launcher_id": None})
        c.patch(f"{ROBOTS}/{robot_id}/execution-type", json={"execution_type_id": et["execution_type_id"]})
        return {"robot_id": robot_id, "target": "cloud", "execution_type": et}

    launchers = c.get("/launcher/available", params={"limit": 500})["items"]
    match = [l for l in launchers if l["launcher_id"] == launcher_id]
    if not match:
        raise OpError(f"Launcher {launcher_id} not found in this company (see `zibloh launchers list`)")
    launcher = match[0]
    if not launcher.get("execution_type_id"):
        raise OpError("That launcher has no execution type yet; ask a Zibloh superadmin to set it")
    c.patch(f"{ROBOTS}/{robot_id}/launcher", json={"launcher_id": None})
    c.patch(f"{ROBOTS}/{robot_id}/execution-type", json={"execution_type_id": launcher["execution_type_id"]})
    c.patch(f"{ROBOTS}/{robot_id}/launcher", json={"launcher_id": launcher_id})
    return {"robot_id": robot_id, "target": "launcher", "launcher": launcher}


def robots_init(directory: Path, template: str = "basic", force: bool = False) -> dict:
    from importlib.resources import files

    source = files("zibloh") / "templates" / template
    if not source.is_dir():
        raise OpError(f"Unknown template '{template}' (basic, browser)")
    directory.mkdir(parents=True, exist_ok=True)
    written = []
    for item in source.iterdir():
        dest = directory / item.name
        if dest.exists() and not force:
            raise OpError(f"{dest} already exists (use --force to overwrite)")
        dest.write_bytes(item.read_bytes())
        written.append(str(dest))
    return {"directory": str(directory), "template": template, "files": sorted(written)}


def robots_validate(directory: Path, target: Optional[str] = None) -> dict:
    return packaging.validate(directory, target).as_dict()


def _target_kind(robot: dict) -> Optional[str]:
    tipology = (robot.get("execution_tipology") or "").strip().lower()
    if not tipology:
        return None
    return "cloud" if tipology == "cloud" else "onprem"


def robots_deploy(c: Client, robot_id: str, directory: Path, progress: Callable[[str], None] = lambda _: None) -> dict:
    robot = robots_get(c, robot_id)
    report = packaging.validate(directory, _target_kind(robot))
    if not report.ok:
        raise OpError("Validation failed:\n- " + "\n- ".join(report.errors))
    package = packaging.build_zip(directory)
    progress(f"packaged {package.files} files, {package.size / 1e6:.1f} MB, sha256 {package.sha256[:12]}…")
    try:
        audit = {"sha256": package.sha256, "validation_warnings": report.warnings[:50]}
        if package.size <= MULTIPART_THRESHOLD:
            result = _upload_single(c, robot_id, package, audit)
        else:
            result = _upload_multipart(c, robot_id, package, audit, progress)
    finally:
        package.path.unlink(missing_ok=True)
    return {
        "robot_id": robot_id,
        "robot_name": result.get("robot_name"),
        "file_id": result.get("file_hash"),
        "size": package.size,
        "sha256": package.sha256,
        "files": package.files,
        "warnings": report.warnings,
    }


def _upload_single(c: Client, robot_id: str, package: packaging.Package, audit: dict) -> dict:
    created = c.post(f"{ROBOTS}/{robot_id}/upload-url", json={
        "filename": "robot.zip", "content_type": ZIP_CONTENT_TYPE, "file_size_bytes": package.size,
    })
    c.upload(created["upload"]["url"], package.path, headers=created["upload"].get("headers"))
    file = created["file"]
    return c.post(f"{ROBOTS}/{robot_id}/upload/complete", json={
        "file_id": file["file_id"], "file_path": file["file_path"], "file_size_bytes": package.size, **audit,
    })


def _upload_multipart(c: Client, robot_id: str, package: packaging.Package, audit: dict, progress) -> dict:
    started = c.post(f"{ROBOTS}/{robot_id}/multipart/initiate", json={
        "filename": "robot.zip", "content_type": ZIP_CONTENT_TYPE, "file_size_bytes": package.size,
    })
    upload_id = started["upload"]["upload_id"]
    part_size = started["upload"]["part_size_bytes"]
    file_id = started["file"]["file_id"]
    numbers = list(range(1, -(-package.size // part_size) + 1))
    parts = []
    try:
        for batch_start in range(0, len(numbers), 100):
            batch = numbers[batch_start:batch_start + 100]
            urls = c.post(f"{ROBOTS}/{robot_id}/multipart/part-urls", json={
                "upload_id": upload_id, "file_id": file_id, "part_numbers": batch,
            })["upload"]["parts"]
            for part in urls:
                n = part["part_number"]
                response = c.upload(part["url"], package.path, offset=(n - 1) * part_size, length=part_size)
                parts.append({"part_number": n, "etag": response.headers.get("etag", "").strip('"')})
                progress(f"uploaded part {n}/{len(numbers)}")
        return c.post(f"{ROBOTS}/{robot_id}/multipart/complete", json={
            "upload_id": upload_id, "file_id": file_id, "file_size_bytes": package.size, "parts": parts, **audit,
        })
    except Exception:
        try:
            c.post(f"{ROBOTS}/{robot_id}/multipart/abort", json={"upload_id": upload_id, "file_id": file_id})
        except ApiError:
            pass
        raise


# ── options and metrics ───────────────────────────────────────────────────────

def options_list(c: Client, robot_id: str) -> list:
    return c.get(f"{ROBOTS}/options/{robot_id}")


def _config_options(config_path: Path) -> list[dict]:
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OpError(f"Cannot read {config_path}: {exc}") from exc
    options = data.get("opciones") or data.get("robot_options") or []
    if not isinstance(options, list):
        raise OpError(f"{config_path}: 'opciones' must be a list")
    return options


def options_define(c: Client, robot_id: str, options: list[dict]) -> dict:
    """Declare options; the ones that already exist (by name) are left alone."""
    existing = {o["name"] for o in options_list(c, robot_id)}
    new = []
    for index, opt in enumerate(options):
        if not opt.get("name"):
            raise OpError(f"option #{index + 1} has no name")
        if opt["name"] in existing:
            continue
        new.append({
            "option_type": opt.get("option_type") or "custom",
            "data_type": opt.get("data_type") or "string",
            "name": opt["name"],
            "display_name": opt.get("display_name") or opt["name"],
            "value": opt.get("value"),
            "list_options": opt.get("list_options"),
            "is_required": opt.get("is_required"),
            "default": opt.get("default") if opt.get("default") is not None else "",
            "order": opt.get("order"),
        })
    skipped = sorted(existing & {o["name"] for o in options})
    if not new:
        return {"created": [], "skipped_existing": skipped}
    result = c.post(f"{ROBOTS}/options/{robot_id}", json={"robot_options": new})
    return {"created": [o["name"] for o in result["robot_options"]], "skipped_existing": skipped}


def options_define_from_config(c: Client, robot_id: str, config_path: Path) -> dict:
    return options_define(c, robot_id, _config_options(config_path))


def options_set(c: Client, robot_id: str, values: dict[str, Any]) -> dict:
    """Set values of existing options by name."""
    if not values:
        raise OpError("Nothing to set (use NAME=VALUE)")
    result = c.patch(f"{ROBOTS}/options/{robot_id}/values", json=values)
    changed = set(values)
    return {"updated": {o["name"]: o.get("value") for o in result["robot_options"] if o["name"] in changed}}


def metrics_get(c: Client, robot_id: str) -> list:
    return c.get(f"/robotMetrics/robots/{robot_id}")


def metrics_set(c: Client, robot_id: str, minutes: Optional[float] = None, euros: Optional[float] = None,
                scope: str = "execution") -> dict:
    """Minutes and/or euros saved per execution (scope=execution) or per processed unit (scope=process)."""
    if minutes is None and euros is None:
        raise OpError("Pass --minutes and/or --euros")
    if scope not in ("execution", "process"):
        raise OpError("scope must be 'execution' or 'process'")
    wanted = {"minutes": minutes, "euros": euros}
    current = {m["metric_type"]: m for m in metrics_get(c, robot_id)}
    creates, updates = [], []
    for base, value in wanted.items():
        if value is None:
            continue
        metric_type = base if scope == "execution" else f"{base}_process"
        if metric_type in current:
            updates.append({"robot_metrics_id": current[metric_type]["robot_metrics_id"], "value": str(value)})
        else:
            creates.append({"metric_type": base, "metric_scope": scope, "name": metric_type, "value": str(value), "default": "0"})
    if creates:
        c.post(f"/robotMetrics/robots/{robot_id}", json={"metrics": creates})
    if updates:
        c.put(f"/robotMetrics/robots/{robot_id}", json={"metrics": updates})
    return {"metrics": metrics_get(c, robot_id)}


# ── executions ────────────────────────────────────────────────────────────────

def robots_run(c: Client, robot_id: str) -> dict:
    return c.post(f"{EXECUTIONS}/launch/{robot_id}")


def execution_get(c: Client, execution_id: str) -> dict:
    return c.get(f"{EXECUTIONS}/executions/{execution_id}/detail")


def execution_logs(c: Client, execution_id: str, tail: Optional[int] = None) -> Optional[dict]:
    """None while the runner has not uploaded the log (it does when the run ends)."""
    return c.get(f"{EXECUTIONS}/executions/{execution_id}/logs", params={"tail": tail}, allow_404=True)


def executions_list(c: Client, robot_id: str, limit: int = 20) -> list:
    data = c.get(f"{EXECUTIONS}/executions/history", params={"robot_id": robot_id, "limit": limit, "skip": 0})
    return [
        {k: item.get(k) for k in ("execution_id", "status", "started_at", "ended_at", "processed_count", "processed_count_detail", "detail")}
        for item in data["items"]
    ]


def execution_cancel(c: Client, execution_id: str) -> dict:
    return c.delete(f"{EXECUTIONS}/queue/delete/{execution_id}")


def wait_for_execution(c: Client, execution_id: str, timeout: float = 900, interval: float = 3.0,
                       tail: int = 50, progress: Callable[[str], None] = lambda _: None) -> dict:
    """Poll until the execution ends, then fetch the log tail (waiting briefly for the upload)."""
    deadline = time.monotonic() + timeout
    last_status = None
    detail = execution_get(c, execution_id)
    while not detail["is_terminal"]:
        if detail["status"] != last_status:
            progress(f"{detail['status']}: {detail.get('log_message') or ''}".strip())
            last_status = detail["status"]
        if time.monotonic() > deadline:
            return {"execution": detail, "timed_out": True, "log": None}
        time.sleep(interval)
        detail = execution_get(c, execution_id)
    progress(f"{detail['status']}: {detail.get('log_message') or ''}".strip())

    log = None
    log_deadline = time.monotonic() + 30
    while log is None and time.monotonic() < log_deadline:
        log = execution_logs(c, execution_id, tail=tail)
        if log is None:
            time.sleep(3)
    return {"execution": detail, "timed_out": False, "log": log}


# ── secrets ───────────────────────────────────────────────────────────────────

def secrets_list(c: Client) -> list:
    return [
        {k: s.get(k) for k in ("company_secret_id", "name", "kind", "description", "is_active")}
        for s in c.get("/company-secrets")
    ]


def _secret_by_name(c: Client, name: str) -> dict:
    match = [s for s in c.get("/company-secrets") if s["name"] == name]
    if not match:
        raise OpError(f"Secret {name} not found (see `zibloh secrets list`)")
    return match[0]


def secrets_create(c: Client, name: str, value: bytes, *, kind: str = "env", description: Optional[str] = None) -> dict:
    if kind == "env":
        text = value.decode("utf-8")
        result = c.post("/company-secrets", json={"name": name, "description": description, "value": text})
    elif kind == "file":
        result = c.post("/company-secrets/file", data={"name": name, "description": description or ""},
                        files={"file": (name, value, "application/octet-stream")})
    else:
        raise OpError("kind must be 'env' or 'file'")
    return {k: result.get(k) for k in ("company_secret_id", "name", "kind", "is_active")}


def secrets_rotate(c: Client, name: str, value: bytes) -> dict:
    secret = _secret_by_name(c, name)
    sid = secret["company_secret_id"]
    if secret["kind"] == "env":
        result = c.post(f"/company-secrets/{sid}/value", json={"value": value.decode("utf-8")})
    else:
        result = c.post(f"/company-secrets/{sid}/file", files={"file": (name, value, "application/octet-stream")})
    return {k: result.get(k) for k in ("company_secret_id", "name", "kind", "is_active")}


def secrets_delete(c: Client, name: str) -> dict:
    secret = _secret_by_name(c, name)
    c.delete(f"/company-secrets/{secret['company_secret_id']}")
    return {"name": name, "deactivated": True}


def robot_secrets_list(c: Client, robot_id: str) -> list:
    return c.get(f"/robots/{robot_id}/secrets")


def _binding_payload(b: dict) -> dict:
    return {k: b.get(k) for k in ("company_secret_id", "expose_as", "target_env_name", "mount_filename", "is_required")}


def robot_secrets_bind(c: Client, robot_id: str, name: str, *, expose_as: Optional[str] = None,
                       env_name: Optional[str] = None, filename: Optional[str] = None, required: bool = False) -> list:
    secret = _secret_by_name(c, name)
    expose_as = expose_as or ("file" if secret["kind"] == "file" else "env")
    current = [_binding_payload(b) for b in robot_secrets_list(c, robot_id) if b["secret_name"] != name]
    current.append({
        "company_secret_id": secret["company_secret_id"],
        "expose_as": expose_as,
        "target_env_name": env_name or name,
        "mount_filename": filename if expose_as == "file" else None,
        "is_required": required,
    })
    return c.put(f"/robots/{robot_id}/secrets", json=current)


def robot_secrets_unbind(c: Client, robot_id: str, name: str) -> list:
    bindings = robot_secrets_list(c, robot_id)
    if not any(b["secret_name"] == name for b in bindings):
        raise OpError(f"{name} is not bound to this robot")
    remaining = [_binding_payload(b) for b in bindings if b["secret_name"] != name]
    return c.put(f"/robots/{robot_id}/secrets", json=remaining)

"""Warehouse operations: document types (lists) and their fields, folders, documents and
their files, approval flows and approvals, and linked documents (matches).

Same contract as `ops.py`: each function takes a `Client` and returns plain data for the
CLI and the MCP tools. Document types are called "types" here and "schemas" by the API;
documents are "objects" in the API. Anywhere a type is expected, its id, name or path works.
"""
from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from zibloh.client import Client
from zibloh.ops import OpError

WH = "/warehouse"
PAGE = 100
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)

# Field types, as the web app's list editor writes them into json_schema.
FIELD_TYPES = {
    "text": {"type": "string"},
    "textarea": {"type": "string", "format": "textarea"},
    "number": {"type": "number"},
    "date": {"type": "string", "format": "date"},
    "boolean": {"type": "boolean"},
    "list": {"type": "array"},
    "table": {"type": "array", "items": {"type": "object"}},
}
COLUMN_TYPES = {k: v for k, v in FIELD_TYPES.items() if k not in ("list", "table")}

DECISIONS = {"approve": "aprobar", "reject": "rechazar", "request_changes": "cambios"}
REQUIREMENT_MODES = {"any": "cualquiera", "all": "todos", "quorum": "quorum"}
STAGE_MODES = {"sequential": "secuencial", "sequential_sum": "secuencial_sumatoria"}
UNIFIED_STATUSES = ("uploaded", "missing_data", "in_approval", "approved", "rejected")


# ── fields ↔ json_schema ──────────────────────────────────────────────────────

def field_type(prop: dict) -> str:
    """Inverse of FIELD_TYPES (same rules as the web app)."""
    kind, fmt = prop.get("type"), prop.get("format")
    if kind == "string" and fmt == "date":
        return "date"
    if kind == "string" and fmt == "textarea":
        return "textarea"
    if kind in ("number", "integer"):
        return "number"
    if kind == "boolean":
        return "boolean"
    if kind == "array":
        return "table" if (prop.get("items") or {}).get("type") == "object" else "list"
    return "text"


def fields_of(json_schema: Optional[dict]) -> list[dict]:
    json_schema = json_schema or {}
    required = set(json_schema.get("required") or [])
    fields = []
    for name, prop in (json_schema.get("properties") or {}).items():
        field = {"name": name, "type": field_type(prop), "required": name in required}
        if field["type"] == "table":
            field["columns"] = [{"name": col, "type": field_type(cp)}
                                for col, cp in ((prop.get("items") or {}).get("properties") or {}).items()]
        if prop.get("x-transcription"):
            field["transcription"] = True
        fields.append(field)
    return fields


def _property(field: dict) -> dict:
    name = (field.get("name") or "").strip()
    if not name:
        raise OpError("Every field needs a name")
    kind = field.get("type") or "text"
    if kind not in FIELD_TYPES:
        raise OpError(f"Field '{name}': unknown type '{kind}' (use {', '.join(FIELD_TYPES)})")
    if kind != "table":
        if field.get("columns"):
            raise OpError(f"Field '{name}': only table fields have columns")
        return {**FIELD_TYPES[kind], "title": name}
    columns = {}
    for col in field.get("columns") or []:
        col_name = (col.get("name") or "").strip()
        col_kind = col.get("type") or "text"
        if not col_name:
            raise OpError(f"Table '{name}': every column needs a name")
        if col_kind not in COLUMN_TYPES:
            raise OpError(f"Table '{name}', column '{col_name}': type must be one of {', '.join(COLUMN_TYPES)}")
        columns[col_name] = {**COLUMN_TYPES[col_kind], "title": col_name}
    return {"type": "array", "title": name, "items": {"type": "object", "properties": columns}}


def build_json_schema(fields: Iterable[dict]) -> dict:
    properties: dict[str, dict] = {}
    required = []
    for field in fields:
        prop = _property(field)
        name = prop["title"]
        if name in properties:
            raise OpError(f"Field '{name}' is defined twice")
        properties[name] = prop
        if field.get("required"):
            required.append(name)
    if not properties:
        return {}
    return {"type": "object", "properties": properties, **({"required": required} if required else {})}


# ── types (schemas) and folders ───────────────────────────────────────────────

def _type_summary(s: dict, with_fields: bool = True) -> dict:
    path = s.get("path") or s["name"]
    out = {
        "type_id": s["schema_id"],
        "name": s["name"],
        "path": path,
        "folder": path.rsplit("/", 1)[0] if "/" in path else "",
        "description": s.get("description"),
        "file_type": s.get("file_type"),
        "is_active": s.get("is_active", True),
        "primary_keys": s.get("primary_keys") or [],
        "allowed_statuses": s.get("allowed_statuses") or [],
    }
    if with_fields:
        out["fields"] = fields_of(s.get("json_schema"))
    return out


def _schemas(c: Client) -> list[dict]:
    return c.get(f"{WH}/schemas/list")


def resolve_type(c: Client, ref: str) -> dict:
    """The raw schema for an id, name or path (case-insensitive)."""
    ref = (ref or "").strip()
    if not ref:
        raise OpError("Missing document type")
    schemas = _schemas(c)
    if UUID_RE.match(ref):
        for s in schemas:
            if s["schema_id"] == ref:
                return s
        return c.get(f"{WH}/schemas/{ref}")  # archived types are not listed
    wanted = ref.strip("/").casefold()
    for key in ("name", "path"):
        match = [s for s in schemas if (s.get(key) or s["name"]).strip("/").casefold() == wanted]
        if len(match) == 1:
            return match[0]
    names = ", ".join(sorted(s.get("path") or s["name"] for s in schemas)) or "none"
    raise OpError(f"No document type '{ref}'. Types: {names}")


def types_list(c: Client, folder: Optional[str] = None, with_fields: bool = False) -> dict:
    types = [_type_summary(s, with_fields) for s in _schemas(c)]
    if folder:
        prefix = folder.strip("/").casefold()
        types = [t for t in types if t["path"].casefold() == prefix or t["path"].casefold().startswith(prefix + "/")]
    folders = sorted({t["folder"] for t in types if t["folder"]})
    return {"types": sorted(types, key=lambda t: t["path"].casefold()), "folders": folders}


def types_get(c: Client, ref: str) -> dict:
    s = resolve_type(c, ref)
    out = _type_summary(s)
    out["json_schema"] = s.get("json_schema") or {}
    out["version"] = s.get("version")
    flows = [f for f in c.get(f"{WH}/approval-flows") if f.get("schema_id") == s["schema_id"]]
    out["approval_flows"] = [{"flow_id": f["flow_id"], "name": f["name"], "is_active": f["is_active"]} for f in flows]
    return out


def types_create(c: Client, name: str, *, path: Optional[str] = None, description: Optional[str] = None,
                 fields: Optional[list[dict]] = None, json_schema: Optional[dict] = None,
                 primary_keys: Optional[list[str]] = None, allowed_statuses: Optional[list[str]] = None,
                 file_type: Optional[str] = None) -> dict:
    """`path` is the full location of the list, folders included ("Contabilidad/Facturas");
    without it the list sits at the root under its name. Names are unique per company."""
    if fields is not None and json_schema is not None:
        raise OpError("Pass fields or json_schema, not both")
    schema = json_schema if json_schema is not None else build_json_schema(fields or [])
    created = c.post(f"{WH}/schemas", json={
        "name": name, "path": path.strip("/") if path else None, "description": description,
        "file_type": file_type, "json_schema": schema, "allowed_statuses": allowed_statuses or [],
        "primary_keys": primary_keys or [],
    })
    return _type_summary(created)


def types_update(c: Client, ref: str, *, name: Optional[str] = None, description: Optional[str] = None,
                 file_type: Optional[str] = None, primary_keys: Optional[list[str]] = None,
                 allowed_statuses: Optional[list[str]] = None, json_schema: Optional[dict] = None) -> dict:
    s = resolve_type(c, ref)
    body = {k: v for k, v in {
        "name": name, "description": description, "file_type": file_type, "primary_keys": primary_keys,
        "allowed_statuses": allowed_statuses, "json_schema": json_schema,
    }.items() if v is not None}
    if not body:
        raise OpError("Nothing to update")
    return _type_summary(c.patch(f"{WH}/schemas/{s['schema_id']}", json=body))


def types_archive(c: Client, ref: str) -> dict:
    s = resolve_type(c, ref)
    c.delete(f"{WH}/schemas/{s['schema_id']}")
    return {"type_id": s["schema_id"], "name": s["name"], "archived": True}


def fields_add(c: Client, ref: str, fields: list[dict]) -> dict:
    if not fields:
        raise OpError("No fields to add")
    s = resolve_type(c, ref)
    schema = dict(s.get("json_schema") or {})
    properties = dict(schema.get("properties") or {})
    required = list(schema.get("required") or [])
    for field in fields:
        prop = _property(field)
        if prop["title"] in properties:
            raise OpError(f"Field '{prop['title']}' already exists; use fields update")
        properties[prop["title"]] = prop
        if field.get("required"):
            required.append(prop["title"])
    schema.update({"type": "object", "properties": properties})
    if required:
        schema["required"] = required
    return _type_summary(c.patch(f"{WH}/schemas/{s['schema_id']}", json={"json_schema": schema}))


def fields_update(c: Client, ref: str, field: str, *, new_name: Optional[str] = None, type: Optional[str] = None,
                  required: Optional[bool] = None, columns: Optional[list[dict]] = None,
                  migrate_documents: bool = True) -> dict:
    """Change one field. A rename also renames the key in primary keys and, with
    `migrate_documents`, in every document's data (approved documents are locked and skipped)."""
    s = resolve_type(c, ref)
    schema = dict(s.get("json_schema") or {})
    properties = dict(schema.get("properties") or {})
    if field not in properties:
        raise OpError(f"No field '{field}' in {s['name']}. Fields: {', '.join(properties) or 'none'}")
    current = {"name": field, "type": field_type(properties[field])}
    if current["type"] == "table":
        current["columns"] = fields_of({"properties": (properties[field].get("items") or {}).get("properties") or {}})
    wanted = {"name": new_name or field, "type": type or current["type"],
              "columns": columns if columns is not None else current.get("columns")}
    if wanted["type"] != "table":
        wanted.pop("columns")
    prop = _property(wanted)
    for key in ("x-transcription",):
        if key in properties[field]:
            prop[key] = properties[field][key]
    name = prop["title"]
    if name != field and name in properties:
        raise OpError(f"Field '{name}' already exists")
    properties = {(name if k == field else k): (prop if k == field else v) for k, v in properties.items()}
    req = [name if r == field else r for r in (schema.get("required") or [])]
    if required is True and name not in req:
        req.append(name)
    if required is False:
        req = [r for r in req if r != name]
    schema.update({"type": "object", "properties": properties})
    schema["required"] = req
    if not req:
        schema.pop("required")
    body: dict[str, Any] = {"json_schema": schema}
    keys = s.get("primary_keys") or []
    if name != field and field in keys:
        body["primary_keys"] = [name if k == field else k for k in keys]
    updated = _type_summary(c.patch(f"{WH}/schemas/{s['schema_id']}", json=body))
    result: dict[str, Any] = {"type": updated}
    if name != field and migrate_documents:
        result["documents"] = _rename_in_documents(c, s["schema_id"], field, name)
    return result


def _rename_in_documents(c: Client, schema_id: str, old: str, new: str) -> dict:
    migrated, skipped = 0, []
    for doc in _iter_documents(c, {"schema_id": schema_id, "include_archived": True}):
        content = doc.get("content") or {}
        if old not in content:
            continue
        renamed = {(new if k == old else k): v for k, v in content.items()}
        if doc.get("status") == "approved":
            skipped.append({"document_id": doc["object_id"], "name": doc.get("name"), "reason": "approved (locked)"})
            continue
        c.patch(f"{WH}/objects/{doc['object_id']}", json={"content": renamed, "what": "field_renamed"})
        migrated += 1
    return {"migrated": migrated, "skipped": skipped}


def fields_remove(c: Client, ref: str, field: str) -> dict:
    """Remove a field from the type. Documents keep their old value (hidden) until edited."""
    s = resolve_type(c, ref)
    schema = dict(s.get("json_schema") or {})
    properties = dict(schema.get("properties") or {})
    if field not in properties:
        raise OpError(f"No field '{field}' in {s['name']}")
    properties.pop(field)
    schema["properties"] = properties
    req = [r for r in (schema.get("required") or []) if r != field]
    schema["required"] = req
    if not req:
        schema.pop("required")
    body: dict[str, Any] = {"json_schema": schema}
    keys = s.get("primary_keys") or []
    if field in keys:
        body["primary_keys"] = [k for k in keys if k != field]
    return _type_summary(c.patch(f"{WH}/schemas/{s['schema_id']}", json=body))


def folders_rename(c: Client, path: str, new_path: str) -> dict:
    return c.patch(f"{WH}/folders", json={"path": path.strip("/"), "nuevo_path": new_path.strip("/")})


def folders_delete(c: Client, path: str) -> dict:
    """Hard delete of the folder with every type, document, approval and link under it."""
    return c.delete(f"{WH}/folders", params={"path": path.strip("/"), "confirmar": "true"})


# ── documents ─────────────────────────────────────────────────────────────────

def _doc_summary(d: dict) -> dict:
    out = {
        "document_id": d["object_id"],
        "name": d.get("name"),
        "type_id": d.get("schema_id"),
        "status": d.get("status"),
        "content": d.get("content") or {},
        "has_file": bool(d.get("file_url")),
        "file_name": d["file_url"].rsplit("/", 1)[-1] if d.get("file_url") else None,
        "created_by": (d.get("created_by_actor") or {}).get("label") or d.get("created_by_name"),
        "created_time": d.get("created_time"),
        "modified_time": d.get("modified_time"),
    }
    if d.get("approval_instance_status"):
        out["approval"] = {
            "status": d["approval_instance_status"],
            "steps_approved": d.get("approval_steps_approved"),
            "steps_total": d.get("approval_steps_total"),
            "my_turn": bool(d.get("approval_my_pending_step_id")),
            "approved_at": d.get("approval_finished_at"),
        }
    return out


def _filters_param(filters: Optional[Any]) -> Optional[str]:
    if filters is None or filters == []:
        return None
    if isinstance(filters, str):
        return filters
    return json.dumps(filters, ensure_ascii=False)


def docs_list(c: Client, type_ref: Optional[str] = None, *, search: Optional[str] = None,
              status: Optional[str] = None, filters: Optional[Any] = None, sort_by: Optional[str] = None,
              sort_dir: str = "asc", limit: int = 50, offset: int = 0, include_archived: bool = False) -> dict:
    """`status` is the web's "Estado" column (uploaded, missing_data, in_approval, approved,
    rejected; needs a type) or any raw status of the type."""
    schema = resolve_type(c, type_ref) if type_ref else None
    params: dict[str, Any] = {
        "schema_id": schema["schema_id"] if schema else None, "search": search, "sort_by": sort_by,
        "sort_dir": sort_dir, "filters": _filters_param(filters), "limit": max(1, min(limit, PAGE)),
        "offset": offset, "include_archived": str(include_archived).lower(),
    }
    if status in UNIFIED_STATUSES:
        if not schema:
            raise OpError(f"status '{status}' needs a document type")
        params["unified_status"] = status
    elif status:
        params["status"] = status
    page = c.get(f"{WH}/objects/list", params=params)
    return {
        "type": schema["name"] if schema else None,
        "documents": [_doc_summary(d) for d in page["objects"]],
        "total": page["total"], "offset": page["offset"], "has_more": page["has_more"],
    }


def _iter_documents(c: Client, params: dict) -> Iterable[dict]:
    offset = 0
    while True:
        page = c.get(f"{WH}/objects/list", params={**params, "limit": PAGE, "offset": offset,
                                                   "include_archived": str(params.get("include_archived", False)).lower()})
        yield from page["objects"]
        if not page["has_more"]:
            return
        offset += PAGE


def docs_get(c: Client, document_id: str, history: bool = False) -> dict:
    d = c.get(f"{WH}/objects/{document_id}")
    out = _doc_summary(d)
    out["links"] = d.get("links") or {}
    out["processing_status"] = d.get("processing_status")
    if history:
        out["history"] = docs_history(c, document_id)
    return out


def docs_history(c: Client, document_id: str) -> list[dict]:
    return [{k: e.get(k) for k in ("ts", "event_type", "actor_type", "actor_name", "message")}
            for e in c.get(f"{WH}/objects/{document_id}/events")]


def _content_type(path: Path) -> str:
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _upload(c: Client, document_id: str, file: Path) -> dict:
    if not file.is_file():
        raise OpError(f"{file} is not a file")
    content_type = _content_type(file)
    target = c.post(f"{WH}/objects/{document_id}/upload", json={"filename": file.name, "content_type": content_type})
    c.upload(target["upload"]["url"], file, headers=target["upload"].get("headers") or {"Content-Type": content_type})
    return c.post(f"{WH}/objects/{document_id}/upload/complete", json={"s3_key": target["file"]["s3_key"]})


def docs_create(c: Client, type_ref: str, *, name: Optional[str] = None, content: Optional[dict] = None,
                file: Optional[Path] = None, status: Optional[str] = None) -> dict:
    """Create a document of a type, optionally with its file. If the upload fails the
    half-created document is deleted, so a retry does not leave duplicates."""
    schema = resolve_type(c, type_ref)
    created = c.post(f"{WH}/objects", json={
        "schema_id": schema["schema_id"], "name": name or (file.name if file else None),
        "content": content or {}, "status": status,
    })
    if file is None:
        return _doc_summary(created)
    try:
        return _doc_summary(_upload(c, created["object_id"], file))
    except Exception:
        try:
            c.delete(f"{WH}/objects/{created['object_id']}")
        except Exception:  # noqa: BLE001 - report the upload error, not the cleanup one
            pass
        raise


def docs_upload(c: Client, document_id: str, file: Path) -> dict:
    """Attach (or replace) the file of an existing document."""
    return _doc_summary(_upload(c, document_id, file))


def _as_dir(dest: str | Path) -> bool:
    """A destination is a directory if it is one, ends with a separator or has no extension
    (`Path` drops a trailing slash, so the raw string is checked)."""
    raw = str(dest)
    return Path(raw).expanduser().is_dir() or raw.endswith(("/", "\\")) or not Path(raw).suffix


def docs_download(c: Client, document_id: str, dest: str | Path) -> dict:
    """Save the document's file. `dest` is a directory (created if needed; the original file
    name is kept) or a file path."""
    info = c.get(f"{WH}/objects/{document_id}/download")
    base = Path(dest).expanduser().resolve()
    target = base / info["filename"] if _as_dir(dest) else base
    size = c.download(info["download_url"], target)
    return {"document_id": document_id, "path": str(target), "bytes": size}


def docs_update(c: Client, document_id: str, *, content: Optional[dict] = None, replace_content: bool = False,
                name: Optional[str] = None, status: Optional[str] = None, type_ref: Optional[str] = None) -> dict:
    """`content` is merged into the current data (a key set to null clears it) unless
    `replace_content`. Approved documents are locked: their data can't change."""
    body: dict[str, Any] = {}
    if content is not None:
        if replace_content:
            body["content"] = content
        else:
            current = c.get(f"{WH}/objects/{document_id}").get("content") or {}
            body["content"] = {**current, **content}
    if name is not None:
        body["name"] = name
    if status is not None:
        body["status"] = status
    if type_ref is not None:
        body["schema_id"] = resolve_type(c, type_ref)["schema_id"]
    if not body:
        raise OpError("Nothing to update")
    return _doc_summary(c.patch(f"{WH}/objects/{document_id}", json=body))


def docs_archive(c: Client, document_id: str, restore: bool = False) -> dict:
    c.post(f"{WH}/objects/{document_id}/{'unarchive' if restore else 'archive'}")
    return {"document_id": document_id, "archived": not restore}


def docs_delete(c: Client, document_id: str) -> dict:
    c.delete(f"{WH}/objects/{document_id}")
    return {"document_id": document_id, "deleted": True}


def docs_export(c: Client, type_ref: str, dest: str | Path, *, what: str = "csv", search: Optional[str] = None,
                status: Optional[str] = None, filters: Optional[Any] = None, include_archived: bool = False) -> dict:
    """`what="csv"`: the documents' data as CSV; `what="files"`: a ZIP of their files (max 500)."""
    if what not in ("csv", "files"):
        raise OpError("what must be 'csv' or 'files'")
    schema = resolve_type(c, type_ref)
    params: dict[str, Any] = {"schema_id": schema["schema_id"], "search": search, "filters": _filters_param(filters),
                              "include_archived": str(include_archived).lower()}
    if status in UNIFIED_STATUSES:
        params["unified_status"] = status
    elif status:
        params["status"] = status
    target = Path(dest).expanduser().resolve()
    if _as_dir(dest):
        target = target / f"{schema['name']}.{'csv' if what == 'csv' else 'zip'}"
    dest = target
    size = c.get_to_file(f"{WH}/objects/{'export' if what == 'csv' else 'export-files'}", dest, params)
    return {"type": schema["name"], "path": str(dest), "bytes": size}


# ── approval flows ────────────────────────────────────────────────────────────

def _step_body(step: dict, position: Optional[int] = None) -> dict:
    body: dict[str, Any] = {}
    for key in ("name", "quorum_count", "auto_approve", "rule"):
        if key in step:
            body[key] = step[key]
    if position is not None or "position" in step:
        body["position"] = step.get("position", position)
    if "requirement" in step:
        if step["requirement"] not in REQUIREMENT_MODES:
            raise OpError(f"requirement must be one of {', '.join(REQUIREMENT_MODES)}")
        body["requirement_mode"] = REQUIREMENT_MODES[step["requirement"]]
    if "stage" in step:
        if step["stage"] not in STAGE_MODES:
            raise OpError(f"stage must be one of {', '.join(STAGE_MODES)}")
        body["stage_mode"] = STAGE_MODES[step["stage"]]
    return body


def _people(c: Client, schema_id: Optional[str]) -> dict[str, str]:
    """user_id → name of the people who can approve in that type (best effort: needs admin)."""
    try:
        return {p["user_id"]: p["name"] for p in c.get(f"{WH}/approval-flows/eligible-approvers",
                                                       params={"schema_id": schema_id})}
    except Exception:  # noqa: BLE001 - names are a nicety
        return {}


def _flow_out(c: Client, flow: dict, names: Optional[dict[str, str]] = None) -> dict:
    names = names if names is not None else _people(c, flow.get("schema_id"))
    steps = c.get(f"{WH}/approval-flows/{flow['flow_id']}/steps")
    reverse_req = {v: k for k, v in REQUIREMENT_MODES.items()}
    reverse_stage = {v: k for k, v in STAGE_MODES.items()}
    return {
        "flow_id": flow["flow_id"], "name": flow["name"], "type_id": flow.get("schema_id"),
        "is_active": flow["is_active"], "any_order": flow["any_order"],
        "linked_type_ids": flow.get("linked_schema_ids") or [],
        "steps": [{
            "step_id": s["step_id"], "name": s["name"], "position": s["position"],
            "requirement": reverse_req.get(s["requirement_mode"], s["requirement_mode"]),
            "quorum_count": s.get("quorum_count"),
            "stage": reverse_stage.get(s["stage_mode"], s["stage_mode"]),
            "auto_approve": s.get("auto_approve"), "rule": s.get("rule"),
            "approvers": [{"user_id": u, "name": names.get(u)} for u in s.get("approver_ids") or []],
        } for s in steps],
    }


def flows_list(c: Client, type_ref: Optional[str] = None, include_inactive: bool = False) -> list[dict]:
    flows = c.get(f"{WH}/approval-flows")
    if type_ref:
        schema_id = resolve_type(c, type_ref)["schema_id"]
        flows = [f for f in flows if f.get("schema_id") == schema_id]
    if not include_inactive:
        flows = [f for f in flows if f["is_active"]]
    type_names = {s["schema_id"]: s["name"] for s in _schemas(c)}
    return [{"flow_id": f["flow_id"], "name": f["name"], "type_id": f.get("schema_id"),
             "type": type_names.get(f.get("schema_id")), "is_active": f["is_active"],
             "any_order": f["any_order"]} for f in flows]


def flows_get(c: Client, flow_id: str) -> dict:
    return _flow_out(c, c.get(f"{WH}/approval-flows/{flow_id}"))


def _approver_body(who: str) -> dict:
    return {"user_id": who} if UUID_RE.match(who) else {"email": who}


def flows_create(c: Client, name: str, type_ref: str, *, steps: Optional[list[dict]] = None, any_order: bool = False,
                 linked_types: Optional[list[str]] = None) -> dict:
    """Create a flow for a type, with its steps and their approvers in one go. A step is
    {name, requirement: any|all|quorum, quorum_count, stage: sequential|sequential_sum,
    auto_approve, rule, approvers: [email or user_id]}; positions follow the list order."""
    schema = resolve_type(c, type_ref)
    linked = [resolve_type(c, t)["schema_id"] for t in linked_types or []]
    flow = c.post(f"{WH}/approval-flows", json={"name": name, "schema_id": schema["schema_id"],
                                                "any_order": any_order, "linked_schema_ids": linked})
    try:
        for position, step in enumerate(steps or [], start=1):
            if not step.get("name"):
                raise OpError(f"Step {position} has no name")
            created = c.post(f"{WH}/approval-flows/{flow['flow_id']}/steps", json=_step_body(step, position))
            for who in step.get("approvers") or []:
                c.post(f"{WH}/approval-steps/{created['step_id']}/approvers", json=_approver_body(who))
    except Exception as exc:
        raise OpError(f"Flow {flow['flow_id']} was created but setting up its steps failed: {exc}. "
                      "Fix it with the step tools, or archive the flow.") from exc
    return flows_get(c, flow["flow_id"])


def flows_update(c: Client, flow_id: str, *, name: Optional[str] = None, active: Optional[bool] = None,
                 any_order: Optional[bool] = None, linked_types: Optional[list[str]] = None) -> dict:
    """Deactivating a flow cancels its open approvals (documents go back to active)."""
    body: dict[str, Any] = {k: v for k, v in {"name": name, "is_active": active, "any_order": any_order}.items()
                            if v is not None}
    if linked_types is not None:
        body["linked_schema_ids"] = [resolve_type(c, t)["schema_id"] for t in linked_types]
    if not body:
        raise OpError("Nothing to update")
    return _flow_out(c, c.patch(f"{WH}/approval-flows/{flow_id}", json=body))


def flows_archive(c: Client, flow_id: str) -> dict:
    c.delete(f"{WH}/approval-flows/{flow_id}")
    return {"flow_id": flow_id, "archived": True}


def steps_add(c: Client, flow_id: str, step: dict) -> dict:
    """Add a step (same shape as in flows_create). Without `position` it goes last."""
    if not step.get("name"):
        raise OpError("The step needs a name")
    position = step.get("position")
    if position is None:
        position = max([s["position"] for s in c.get(f"{WH}/approval-flows/{flow_id}/steps")] or [0]) + 1
    created = c.post(f"{WH}/approval-flows/{flow_id}/steps", json=_step_body(step, position))
    for who in step.get("approvers") or []:
        c.post(f"{WH}/approval-steps/{created['step_id']}/approvers", json=_approver_body(who))
    return flows_get(c, flow_id)


def steps_update(c: Client, step_id: str, changes: dict) -> dict:
    body = _step_body(changes)
    if not body:
        raise OpError("Nothing to update (name, position, requirement, quorum_count, stage, auto_approve, rule)")
    return c.patch(f"{WH}/approval-steps/{step_id}", json=body)


def steps_remove(c: Client, step_id: str) -> dict:
    c.delete(f"{WH}/approval-steps/{step_id}")
    return {"step_id": step_id, "removed": True}


def approvers_add(c: Client, step_id: str, who: str) -> dict:
    """`who` is an email or a user_id. They need at least read access to the type."""
    return c.post(f"{WH}/approval-steps/{step_id}/approvers", json=_approver_body(who))


def approvers_remove(c: Client, step_id: str, user_id: str) -> dict:
    c.delete(f"{WH}/approval-steps/{step_id}/approvers/{user_id}")
    return {"step_id": step_id, "user_id": user_id, "removed": True}


def approvers_eligible(c: Client, type_ref: Optional[str] = None) -> list[dict]:
    schema_id = resolve_type(c, type_ref)["schema_id"] if type_ref else None
    return c.get(f"{WH}/approval-flows/eligible-approvers", params={"schema_id": schema_id})


# ── approvals at runtime ──────────────────────────────────────────────────────

def approvals_inbox(c: Client, flow_id: Optional[str] = None, *, bucket: str = "my_turn",
                    state: Optional[str] = "pending", limit: int = 50) -> dict:
    """Documents waiting in approval flows. bucket: my_turn (the logged-in person must
    decide), pending_other (waiting for someone else) or all."""
    if bucket not in ("my_turn", "pending_other", "all"):
        raise OpError("bucket must be my_turn, pending_other or all")
    flows = [c.get(f"{WH}/approval-flows/{flow_id}")] if flow_id else [
        f for f in c.get(f"{WH}/approval-flows") if f["is_active"]]
    out = []
    for flow in flows:
        page = c.get(f"{WH}/approval-flows/{flow['flow_id']}/documents", params={
            "bucket": None if bucket == "all" else bucket, "state": state, "limit": max(1, min(limit, PAGE)),
        })
        if not page["objects"] and not flow_id:
            continue
        out.append({"flow_id": flow["flow_id"], "flow": flow["name"], "counts": page.get("counts"),
                    "total": page["total"], "documents": [_doc_summary(d) for d in page["objects"]]})
    return {"bucket": bucket, "flows": out}


def _instance_of(c: Client, document_id: str) -> dict:
    instance = c.get(f"{WH}/objects/{document_id}/approval-instance")
    if not instance:
        raise OpError("This document has no approval; start one with approvals start")
    return instance


def approvals_status(c: Client, document_id: str) -> dict:
    instance = c.get(f"{WH}/objects/{document_id}/approval-instance")
    if not instance:
        return {"document_id": document_id, "approval": None}
    flow = flows_get(c, instance["flow_id"])
    steps = {s["step_id"]: s for s in flow["steps"]}
    names = {a["user_id"]: a["name"] for s in flow["steps"] for a in s["approvers"]}
    states = c.get(f"{WH}/approval-instances/{instance['instance_id']}/step-states")
    decisions = c.get(f"{WH}/approval-instances/{instance['instance_id']}/decisions")
    return {
        "document_id": document_id, "instance_id": instance["instance_id"], "status": instance["status"],
        "flow": flow["name"], "flow_id": flow["flow_id"], "current_step_id": instance.get("current_step_id"),
        "started_at": instance["started_at"], "finished_at": instance.get("finished_at"),
        "steps": [{"step_id": st["step_id"], "name": (steps.get(st["step_id"]) or {}).get("name"),
                   "status": st["status"], "resolved_at": st.get("resolved_at"),
                   "approvers": (steps.get(st["step_id"]) or {}).get("approvers", [])} for st in states],
        "decisions": [{"step": (steps.get(d["step_id"]) or {}).get("name"), "decision": d["decision"],
                       "by": names.get(d.get("user_id")) or d.get("user_id"), "comment": d.get("comment"),
                       "at": d["decided_at"]} for d in decisions],
    }


def approvals_start(c: Client, document_id: str, flow_id: Optional[str] = None) -> dict:
    """Send a document to approval (its type's active flow unless `flow_id`). Required fields
    must be filled in first."""
    c.post(f"{WH}/objects/{document_id}/approval-instance", json={"flow_id": flow_id})
    return approvals_status(c, document_id)


def approvals_decide(c: Client, document_id: str, decision: str, comment: Optional[str] = None,
                     step_id: Optional[str] = None) -> dict:
    """approve | reject (needs a comment) | request_changes, as the logged-in person."""
    if decision not in DECISIONS:
        raise OpError(f"decision must be one of {', '.join(DECISIONS)}")
    instance = _instance_of(c, document_id)
    c.post(f"{WH}/approval-instances/{instance['instance_id']}/decision",
           json={"decision": DECISIONS[decision], "comment": comment, "step_id": step_id})
    return approvals_status(c, document_id)


def approvals_force(c: Client, document_id: str, comment: Optional[str] = None) -> dict:
    """Approve every pending step at once, skipping approvers and order (warehouse admins only)."""
    instance = _instance_of(c, document_id)
    c.post(f"{WH}/approval-instances/{instance['instance_id']}/force-approve", json={"comment": comment})
    return approvals_status(c, document_id)


# ── linked documents (matches) ────────────────────────────────────────────────

def _match_out(c: Client, match: dict) -> dict:
    members = c.get(f"{WH}/matches/{match['match_id']}/members")
    return {"link_id": match["match_id"], "primary_document_id": match["primary_object_id"],
            "status": match["status"], "resolution": match.get("resolution"),
            "members": [{"document_id": m["object_id"], "match_status": m["match_status"],
                         "field_results": m.get("field_results")} for m in members]}


def links_list(c: Client, document_id: str) -> list[dict]:
    return [_match_out(c, m) for m in c.get(f"{WH}/matches", params={"primary_object_id": document_id})]


def links_create(c: Client, document_id: str, member_ids: list[str], fields: Optional[list[str]] = None) -> dict:
    """Link documents (e.g. delivery notes) to a primary one (the invoice) and compare `fields`
    (default: the type's primary keys). If the primary already has a link, members are added to it."""
    if not member_ids:
        raise OpError("Pass at least one document to link")
    existing = c.get(f"{WH}/matches", params={"primary_object_id": document_id})
    if existing:
        match = existing[0]
        present = {m["object_id"] for m in c.get(f"{WH}/matches/{match['match_id']}/members")}
        for member in member_ids:
            if member not in present:
                c.post(f"{WH}/matches/{match['match_id']}/members", json={"object_id": member})
        return _match_out(c, c.get(f"{WH}/matches/{match['match_id']}"))
    return _match_out(c, c.post(f"{WH}/objects/{document_id}/match",
                                json={"member_object_ids": member_ids, "fields": fields}))


def links_remove(c: Client, link_id: str, document_id: str) -> dict:
    """Unlink one member; removing the last one deletes the link."""
    c.delete(f"{WH}/matches/{link_id}/members/{document_id}")
    return {"link_id": link_id, "document_id": document_id, "removed": True}


def links_mark(c: Client, link_id: str, status: str, note: Optional[str] = None) -> dict:
    """Resolve a link by hand: correct | error | pending."""
    mapping = {"correct": "correcta", "error": "error", "pending": "pendiente"}
    if status not in mapping:
        raise OpError("status must be correct, error or pending")
    resolution = None
    if note:
        # The resolution also keeps the compared fields, which new members reuse: merge, don't replace.
        resolution = {**(c.get(f"{WH}/matches/{link_id}").get("resolution") or {}), "nota": note}
    match = c.patch(f"{WH}/matches/{link_id}", json={"status": mapping[status], "resolution": resolution})
    return _match_out(c, match)

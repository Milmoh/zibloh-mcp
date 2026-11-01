"""Warehouse operations against a mocked API: types and fields, documents and files,
approval flows and decisions, links, and the CLI/MCP front ends."""
import json

import httpx
import pytest
import respx
from mcp.server.mcpserver.exceptions import ToolError
from typer.testing import CliRunner

from zibloh import cli, config, mcp_server, warehouse
from zibloh.client import Client
from zibloh.ops import OpError

BASE = "https://api.test/api/v1"
WH = f"{BASE}/warehouse"
KEY = "zbl_" + "k" * 40
T1 = "aaaaaaaa-0000-0000-0000-000000000001"
DOC = "dddddddd-0000-0000-0000-000000000001"
FLOW = "ffffffff-0000-0000-0000-000000000001"
STEP = "eeeeeeee-0000-0000-0000-000000000001"
INST = "99999999-0000-0000-0000-000000000001"
USER = "77777777-0000-0000-0000-000000000001"

FACTURAS = {
    "schema_id": T1, "name": "Facturas", "path": "Contabilidad/Facturas", "description": None, "file_type": "pdf",
    "is_active": True, "primary_keys": ["Número"], "allowed_statuses": [], "version": 1,
    "json_schema": {"type": "object", "required": ["Número"], "properties": {
        "Número": {"type": "string", "title": "Número"},
        "Importe": {"type": "number", "title": "Importe"},
        "Líneas": {"type": "array", "title": "Líneas", "items": {"type": "object", "properties": {
            "Concepto": {"type": "string", "title": "Concepto"}}}},
    }},
}


def _doc(**extra):
    return {"object_id": DOC, "schema_id": T1, "name": "F-1", "status": "active", "content": {"Número": "1"},
            "file_url": None, "created_by_name": "Ana", "created_time": "2026-11-01T10:00:00Z",
            "modified_time": "2026-11-01T10:00:00Z", **extra}


@pytest.fixture
def client():
    c = Client(config.Profile(name="t", base_url=BASE, api_key=KEY), client_name="mcp")
    c.tool = "warehouse_test"
    yield c
    c.close()


@pytest.fixture
def logged_in(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    for var in ("ZIBLOH_API_KEY", "ZIBLOH_BASE_URL", "ZIBLOH_ENV", "ZIBLOH_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    config.save_profile("acme", KEY, None, BASE, company_id="c", company_name="Acme")


def _schemas(*items):
    return respx.get(f"{WH}/schemas/list").mock(return_value=httpx.Response(200, json=list(items or [FACTURAS])))


# ── fields ↔ json_schema ──────────────────────────────────────────────────────

def test_fields_round_trip_like_the_web_app():
    fields = [
        {"name": "Número", "type": "text", "required": True},
        {"name": "Fecha", "type": "date"},
        {"name": "Notas", "type": "textarea"},
        {"name": "Pagada", "type": "boolean"},
        {"name": "Líneas", "type": "table", "columns": [{"name": "Concepto", "type": "text"}, {"name": "Base", "type": "number"}]},
    ]
    schema = warehouse.build_json_schema(fields)
    assert schema["required"] == ["Número"]
    assert schema["properties"]["Fecha"] == {"type": "string", "format": "date", "title": "Fecha"}
    assert schema["properties"]["Líneas"]["items"]["properties"]["Base"] == {"type": "number", "title": "Base"}
    assert list(schema["properties"]) == ["Número", "Fecha", "Notas", "Pagada", "Líneas"]
    back = warehouse.fields_of(schema)
    assert [f["type"] for f in back] == ["text", "date", "textarea", "boolean", "table"]
    assert back[4]["columns"][1] == {"name": "Base", "type": "number"}


@pytest.mark.parametrize("bad", [
    [{"name": "", "type": "text"}],
    [{"name": "X", "type": "money"}],
    [{"name": "X", "type": "table", "columns": [{"name": "Y", "type": "table"}]}],
    [{"name": "X", "type": "text", "columns": [{"name": "Y"}]}],
    [{"name": "X"}, {"name": "X"}],
])
def test_bad_fields_are_rejected_before_calling_the_api(bad):
    with pytest.raises(OpError):
        warehouse.build_json_schema(bad)


# ── types ─────────────────────────────────────────────────────────────────────

@respx.mock
def test_warehouse_requests_carry_the_key_in_both_headers(client):
    route = _schemas()
    warehouse.types_list(client)
    sent = route.calls.last.request.headers
    assert sent["x-api-key"] == KEY and sent["authorization"] == f"Bearer {KEY}"
    assert sent["x-zibloh-client"] == "mcp"


@respx.mock
def test_types_resolve_by_id_name_or_path(client):
    _schemas()
    assert warehouse.resolve_type(client, T1)["name"] == "Facturas"
    assert warehouse.resolve_type(client, "facturas")["schema_id"] == T1
    assert warehouse.resolve_type(client, "/Contabilidad/Facturas/")["schema_id"] == T1
    with pytest.raises(OpError, match="Contabilidad/Facturas"):
        warehouse.resolve_type(client, "Albaranes")


@respx.mock
def test_types_list_groups_folders(client):
    _schemas(FACTURAS, {**FACTURAS, "schema_id": "x", "name": "Raíz", "path": None})
    listed = warehouse.types_list(client)
    assert listed["folders"] == ["Contabilidad"]
    assert [t["path"] for t in listed["types"]] == ["Contabilidad/Facturas", "Raíz"]
    assert "fields" not in listed["types"][0]
    assert warehouse.types_list(client, folder="contabilidad", with_fields=True)["types"][0]["fields"][0]["name"] == "Número"


@respx.mock
def test_types_create_sends_the_web_apps_json_schema(client):
    route = respx.post(f"{WH}/schemas").mock(return_value=httpx.Response(201, json=FACTURAS))
    warehouse.types_create(client, "Facturas", path="/Contabilidad/Facturas", fields=[
        {"name": "Número", "type": "text", "required": True}], primary_keys=["Número"])
    body = json.loads(route.calls.last.request.content)
    assert body["path"] == "Contabilidad/Facturas" and body["primary_keys"] == ["Número"]
    assert body["json_schema"] == {"type": "object", "required": ["Número"],
                                   "properties": {"Número": {"type": "string", "title": "Número"}}}


@respx.mock
def test_fields_add_appends_and_refuses_duplicates(client):
    _schemas()
    patch = respx.patch(f"{WH}/schemas/{T1}").mock(return_value=httpx.Response(200, json=FACTURAS))
    warehouse.fields_add(client, "Facturas", [{"name": "Proveedor", "type": "text", "required": True}])
    schema = json.loads(patch.calls.last.request.content)["json_schema"]
    assert list(schema["properties"])[-1] == "Proveedor"
    assert schema["required"] == ["Número", "Proveedor"]
    with pytest.raises(OpError, match="already exists"):
        warehouse.fields_add(client, "Facturas", [{"name": "Importe", "type": "number"}])


@respx.mock
def test_renaming_a_field_moves_keys_required_and_document_data(client):
    _schemas()
    patch = respx.patch(f"{WH}/schemas/{T1}").mock(return_value=httpx.Response(200, json=FACTURAS))
    respx.get(f"{WH}/objects/list").mock(return_value=httpx.Response(200, json={
        "objects": [_doc(content={"Número": "1", "Importe": 5}),
                    _doc(object_id="locked", status="approved", content={"Número": "2"}),
                    _doc(object_id="empty", content={})],
        "total": 3, "limit": 100, "offset": 0, "has_more": False}))
    doc_patch = respx.patch(f"{WH}/objects/{DOC}").mock(return_value=httpx.Response(200, json=_doc()))

    result = warehouse.fields_update(client, "Facturas", "Número", new_name="Nº factura")

    body = json.loads(patch.calls.last.request.content)
    assert list(body["json_schema"]["properties"])[0] == "Nº factura"
    assert body["json_schema"]["required"] == ["Nº factura"] and body["primary_keys"] == ["Nº factura"]
    assert json.loads(doc_patch.calls.last.request.content) == {
        "content": {"Nº factura": "1", "Importe": 5}, "what": "field_renamed"}
    assert result["documents"]["migrated"] == 1
    assert result["documents"]["skipped"][0]["document_id"] == "locked"


@respx.mock
def test_fields_update_type_and_required_and_remove(client):
    _schemas()
    patch = respx.patch(f"{WH}/schemas/{T1}").mock(return_value=httpx.Response(200, json=FACTURAS))
    warehouse.fields_update(client, "Facturas", "Importe", type="text", required=True)
    schema = json.loads(patch.calls.last.request.content)["json_schema"]
    assert schema["properties"]["Importe"]["type"] == "string" and "Importe" in schema["required"]
    assert "primary_keys" not in json.loads(patch.calls.last.request.content)

    warehouse.fields_remove(client, "Facturas", "Número")
    body = json.loads(patch.calls.last.request.content)
    assert "Número" not in body["json_schema"]["properties"] and "required" not in body["json_schema"]
    assert body["primary_keys"] == []


@respx.mock
def test_folders_delete_confirms_on_the_api(client):
    route = respx.delete(f"{WH}/folders").mock(return_value=httpx.Response(200, json={"carpeta": "A", "tipos": 1}))
    warehouse.folders_delete(client, "/A/")
    assert route.calls.last.request.url.params["confirmar"] == "true"
    assert route.calls.last.request.url.params["path"] == "A"


# ── documents ─────────────────────────────────────────────────────────────────

@respx.mock
def test_docs_list_uses_the_unified_status_and_json_filters(client):
    _schemas()
    route = respx.get(f"{WH}/objects/list").mock(return_value=httpx.Response(200, json={
        "objects": [_doc(approval_instance_status="pendiente", approval_my_pending_step_id=STEP,
                         approval_steps_total=2, approval_steps_approved=1, file_url="company/c/warehouse/d/f.pdf")],
        "total": 1, "limit": 50, "offset": 0, "has_more": False}))
    page = warehouse.docs_list(client, "Facturas", status="in_approval",
                               filters=[{"col": "content.Importe", "op": "range", "min": 100}])
    params = route.calls.last.request.url.params
    assert params["unified_status"] == "in_approval" and "status" not in params
    assert json.loads(params["filters"])[0]["min"] == 100 and params["schema_id"] == T1
    doc = page["documents"][0]
    assert doc["file_name"] == "f.pdf" and doc["approval"]["my_turn"] is True
    with pytest.raises(OpError, match="needs a document type"):
        warehouse.docs_list(client, status="approved")


@respx.mock
def test_docs_create_uploads_without_the_api_key(client, tmp_path):
    _schemas()
    pdf = tmp_path / "F-1.pdf"
    pdf.write_bytes(b"%PDF-1.4")
    create = respx.post(f"{WH}/objects").mock(return_value=httpx.Response(201, json=_doc()))
    respx.post(f"{WH}/objects/{DOC}/upload").mock(return_value=httpx.Response(200, json={
        "upload": {"method": "PUT", "url": "https://s3.test/put?sig=1", "headers": {"Content-Type": "application/pdf"}},
        "file": {"s3_key": "company/c/warehouse/d/F-1.pdf"}}))
    put = respx.put("https://s3.test/put?sig=1").mock(return_value=httpx.Response(200))
    complete = respx.post(f"{WH}/objects/{DOC}/upload/complete").mock(return_value=httpx.Response(200, json=_doc(
        file_url="company/c/warehouse/d/F-1.pdf")))

    doc = warehouse.docs_create(client, "Facturas", content={"Número": "1"}, file=pdf)

    assert json.loads(create.calls.last.request.content)["name"] == "F-1.pdf"
    sent = put.calls.last.request
    assert "authorization" not in sent.headers and "x-api-key" not in sent.headers
    assert sent.headers["content-type"] == "application/pdf" and sent.content == b"%PDF-1.4"
    assert json.loads(complete.calls.last.request.content) == {"s3_key": "company/c/warehouse/d/F-1.pdf"}
    assert doc["has_file"] and doc["file_name"] == "F-1.pdf"


@respx.mock
def test_a_failed_upload_deletes_the_half_created_document(client, tmp_path):
    _schemas()
    pdf = tmp_path / "F-1.pdf"
    pdf.write_bytes(b"x")
    respx.post(f"{WH}/objects").mock(return_value=httpx.Response(201, json=_doc()))
    respx.post(f"{WH}/objects/{DOC}/upload").mock(return_value=httpx.Response(200, json={
        "upload": {"url": "https://s3.test/put", "headers": {}}, "file": {"s3_key": "k"}}))
    respx.put("https://s3.test/put").mock(return_value=httpx.Response(403, text="<Error>SignatureDoesNotMatch</Error>"))
    cleanup = respx.delete(f"{WH}/objects/{DOC}").mock(return_value=httpx.Response(204))
    with pytest.raises(Exception, match="SignatureDoesNotMatch"):
        warehouse.docs_create(client, "Facturas", file=pdf)
    assert cleanup.called


@respx.mock
def test_docs_download_keeps_the_original_name(client, tmp_path):
    respx.get(f"{WH}/objects/{DOC}/download").mock(return_value=httpx.Response(200, json={
        "download_url": "https://s3.test/get?sig=1", "filename": "F-1.pdf", "expires_in": 900}))
    get = respx.get("https://s3.test/get?sig=1").mock(return_value=httpx.Response(200, content=b"%PDF"))
    result = warehouse.docs_download(client, DOC, tmp_path)
    assert result["path"] == str(tmp_path / "F-1.pdf") and (tmp_path / "F-1.pdf").read_bytes() == b"%PDF"
    assert "authorization" not in get.calls.last.request.headers
    assert not list(tmp_path.glob("*.part"))


@respx.mock
def test_docs_update_merges_content_unless_replacing(client):
    respx.get(f"{WH}/objects/{DOC}").mock(return_value=httpx.Response(200, json=_doc(content={"Número": "1", "Importe": 5})))
    patch = respx.patch(f"{WH}/objects/{DOC}").mock(return_value=httpx.Response(200, json=_doc()))
    warehouse.docs_update(client, DOC, content={"Importe": 7, "Número": None})
    assert json.loads(patch.calls.last.request.content) == {"content": {"Número": None, "Importe": 7}}
    warehouse.docs_update(client, DOC, content={"Importe": 7}, replace_content=True)
    assert json.loads(patch.calls.last.request.content) == {"content": {"Importe": 7}}


@respx.mock
def test_docs_export_streams_to_a_file_with_the_key(client, tmp_path):
    _schemas()
    route = respx.get(f"{WH}/objects/export").mock(return_value=httpx.Response(200, content="﻿a;b\n".encode()))
    result = warehouse.docs_export(client, "Facturas", tmp_path)
    assert result["path"] == str(tmp_path / "Facturas.csv") and (tmp_path / "Facturas.csv").read_bytes().endswith(b"a;b\n")
    assert route.calls.last.request.headers["x-api-key"] == KEY

    respx.get(f"{WH}/objects/export-files").mock(return_value=httpx.Response(403, json={"detail": "sin scope"}))
    with pytest.raises(Exception, match="sin scope"):
        warehouse.docs_export(client, "Facturas", tmp_path, what="files")
    assert not (tmp_path / "Facturas.zip").exists()


# ── approvals ─────────────────────────────────────────────────────────────────

def _flow_routes():
    respx.get(f"{WH}/approval-flows/{FLOW}").mock(return_value=httpx.Response(200, json={
        "flow_id": FLOW, "name": "Facturas", "schema_id": T1, "is_active": True, "any_order": False,
        "linked_schema_ids": []}))
    respx.get(f"{WH}/approval-flows/{FLOW}/steps").mock(return_value=httpx.Response(200, json=[{
        "step_id": STEP, "flow_id": FLOW, "name": "Revisión", "position": 1, "stage_mode": "secuencial",
        "requirement_mode": "cualquiera", "quorum_count": None, "auto_approve": False, "rule": None,
        "approver_ids": [USER]}]))
    respx.get(f"{WH}/approval-flows/eligible-approvers").mock(return_value=httpx.Response(200, json=[
        {"user_id": USER, "name": "Ana"}]))


@respx.mock
def test_flows_create_sets_up_steps_and_approvers_in_order(client):
    _schemas()
    _flow_routes()
    create = respx.post(f"{WH}/approval-flows").mock(return_value=httpx.Response(201, json={"flow_id": FLOW}))
    steps = respx.post(f"{WH}/approval-flows/{FLOW}/steps").mock(
        side_effect=[httpx.Response(201, json={"step_id": STEP}), httpx.Response(201, json={"step_id": "s2"})])
    approvers = respx.post(url__regex=rf"{WH}/approval-steps/.+/approvers").mock(return_value=httpx.Response(201, json={}))

    flow = warehouse.flows_create(client, "Facturas", "Facturas", steps=[
        {"name": "Revisión", "approvers": ["ana@acme.test"]},
        {"name": "Dirección", "requirement": "quorum", "quorum_count": 2,
         "rule": {"conditions": [{"field": "Importe", "op": "gt", "value": 3000}]}, "approvers": [USER]},
    ])

    assert json.loads(create.calls.last.request.content)["schema_id"] == T1
    second = json.loads(steps.calls[1].request.content)
    assert second == {"name": "Dirección", "position": 2, "requirement_mode": "quorum", "quorum_count": 2,
                      "rule": {"conditions": [{"field": "Importe", "op": "gt", "value": 3000}]}}
    assert [json.loads(call.request.content) for call in approvers.calls] == [{"email": "ana@acme.test"}, {"user_id": USER}]
    assert flow["steps"][0]["approvers"] == [{"user_id": USER, "name": "Ana"}]
    assert flow["steps"][0]["requirement"] == "any"


@respx.mock
def test_a_failed_step_says_which_flow_was_left_behind(client):
    _schemas()
    respx.post(f"{WH}/approval-flows").mock(return_value=httpx.Response(201, json={"flow_id": FLOW}))
    respx.post(f"{WH}/approval-flows/{FLOW}/steps").mock(return_value=httpx.Response(422, json={"detail": "quorum requiere quorum_count > 0"}))
    with pytest.raises(OpError, match=FLOW):
        warehouse.flows_create(client, "F", "Facturas", steps=[{"name": "x", "requirement": "quorum"}])


@respx.mock
def test_decide_maps_to_the_api_vocabulary_and_reports_the_status(client):
    _flow_routes()
    respx.get(f"{WH}/objects/{DOC}/approval-instance").mock(return_value=httpx.Response(200, json={
        "instance_id": INST, "object_id": DOC, "flow_id": FLOW, "current_step_id": None, "status": "rechazado",
        "started_at": "t0", "finished_at": "t1"}))
    decision = respx.post(f"{WH}/approval-instances/{INST}/decision").mock(return_value=httpx.Response(200, json={}))
    respx.get(f"{WH}/approval-instances/{INST}/step-states").mock(return_value=httpx.Response(200, json=[
        {"instance_id": INST, "step_id": STEP, "status": "rechazado", "resolved_at": "t1"}]))
    respx.get(f"{WH}/approval-instances/{INST}/decisions").mock(return_value=httpx.Response(200, json=[
        {"decision_id": "d", "instance_id": INST, "step_id": STEP, "user_id": USER, "decision": "rechazar",
         "comment": "Importe mal", "decided_at": "t1"}]))

    status = warehouse.approvals_decide(client, DOC, "reject", comment="Importe mal")

    assert json.loads(decision.calls.last.request.content) == {"decision": "rechazar", "comment": "Importe mal", "step_id": None}
    assert status["status"] == "rechazado"
    assert status["decisions"] == [{"step": "Revisión", "decision": "rechazar", "by": "Ana", "comment": "Importe mal", "at": "t1"}]
    with pytest.raises(OpError):
        warehouse.approvals_decide(client, DOC, "maybe")


@respx.mock
def test_decide_without_an_approval_says_how_to_start_one(client):
    respx.get(f"{WH}/objects/{DOC}/approval-instance").mock(return_value=httpx.Response(200, json=None))
    with pytest.raises(OpError, match="approvals start"):
        warehouse.approvals_decide(client, DOC, "approve")


@respx.mock
def test_inbox_lists_my_turn_per_active_flow(client):
    respx.get(f"{WH}/approval-flows").mock(return_value=httpx.Response(200, json=[
        {"flow_id": FLOW, "name": "Facturas", "is_active": True}, {"flow_id": "old", "name": "Old", "is_active": False}]))
    docs = respx.get(f"{WH}/approval-flows/{FLOW}/documents").mock(return_value=httpx.Response(200, json={
        "objects": [_doc()], "total": 1, "offset": 0, "has_more": False, "counts": {"my_turn": 1}}))
    inbox = warehouse.approvals_inbox(client)
    assert docs.calls.last.request.url.params["bucket"] == "my_turn"
    assert inbox["flows"][0]["documents"][0]["document_id"] == DOC and len(inbox["flows"]) == 1


# ── links ─────────────────────────────────────────────────────────────────────

@respx.mock
def test_links_create_adds_to_an_existing_link_instead_of_duplicating(client):
    respx.get(f"{WH}/matches").mock(return_value=httpx.Response(200, json=[{"match_id": "m1"}]))
    respx.get(f"{WH}/matches/m1/members").mock(return_value=httpx.Response(200, json=[
        {"object_id": "a", "match_status": "coincide"}]))
    add = respx.post(f"{WH}/matches/m1/members").mock(return_value=httpx.Response(201, json={}))
    respx.get(f"{WH}/matches/m1").mock(return_value=httpx.Response(200, json={
        "match_id": "m1", "primary_object_id": DOC, "status": "correcta", "resolution": {"fields": ["Número"]}}))
    created = respx.post(f"{WH}/objects/{DOC}/match")

    warehouse.links_create(client, DOC, ["a", "b"])

    assert [json.loads(c.request.content) for c in add.calls] == [{"object_id": "b"}]
    assert not created.called


@respx.mock
def test_links_mark_keeps_the_compared_fields(client):
    respx.get(f"{WH}/matches/m1").mock(return_value=httpx.Response(200, json={
        "match_id": "m1", "primary_object_id": DOC, "status": "error", "resolution": {"fields": ["Número"]}}))
    patch = respx.patch(f"{WH}/matches/m1").mock(return_value=httpx.Response(200, json={
        "match_id": "m1", "primary_object_id": DOC, "status": "correcta", "resolution": {}}))
    respx.get(f"{WH}/matches/m1/members").mock(return_value=httpx.Response(200, json=[]))
    warehouse.links_mark(client, "m1", "correct", note="revisado")
    assert json.loads(patch.calls.last.request.content) == {
        "status": "correcta", "resolution": {"fields": ["Número"], "nota": "revisado"}}


# ── front ends ────────────────────────────────────────────────────────────────

@respx.mock
def test_mcp_tool_turns_api_errors_into_tool_errors(logged_in):
    route = respx.get(f"{WH}/schemas/list").mock(return_value=httpx.Response(403, json={
        "detail": "Esta API Key no permite el uso por MCP (falta el scope 'mcp:use')."}))
    with pytest.raises(ToolError, match="mcp:use"):
        mcp_server.warehouse_types_list()
    assert route.calls.last.request.headers["x-zibloh-tool"] == "warehouse_types_list"


@respx.mock
def test_cli_types_create_from_field_specs(logged_in):
    route = respx.post(f"{WH}/schemas").mock(return_value=httpx.Response(201, json=FACTURAS))
    out = CliRunner().invoke(cli.app, ["warehouse", "types", "create", "Facturas", "--path", "Contabilidad/Facturas",
                                       "--field", "Número:text:required", "--field", "Importe:number",
                                       "--primary-key", "Número", "--json"])
    assert out.exit_code == 0, out.output
    body = json.loads(route.calls.last.request.content)
    assert body["json_schema"]["properties"]["Importe"]["type"] == "number"
    assert body["json_schema"]["required"] == ["Número"]
    assert route.calls.last.request.headers["x-zibloh-tool"] == "warehouse types create"


def test_cli_rejects_bad_json_cleanly(logged_in):
    out = CliRunner().invoke(cli.app, ["warehouse", "docs", "update", DOC, "--content", "{oops", "--json"])
    assert out.exit_code == 1 and "invalid JSON" in out.output

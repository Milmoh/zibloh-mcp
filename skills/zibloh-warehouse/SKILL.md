---
name: zibloh-warehouse
description: Work with the Zibloh Warehouse from Claude Code through the warehouse_* MCP tools or `zibloh warehouse …`. Use when the user wants to set up document types (lists) and their fields or folders, list, search, upload, download, edit, archive or export documents, configure approval flows (steps, rules, approvers), see what is waiting for their approval, approve or reject documents, or link documents to each other (invoice ↔ delivery notes).
---

# Zibloh Warehouse

The Warehouse stores **documents** (data + an optional file) in **document types** (the web
app's "listas"), which live in **folders**. A type defines the **fields** of its documents.
**Approval flows** send documents of a type through **steps** with approvers; documents can
be **linked** to a primary one and compared field by field.

Every MCP tool is `warehouse_<area>_<action>`; the CLI is `zibloh warehouse <area> <action>`
with `--json`. Wherever a type is expected, its id, name or path works ("Facturas" or
"Contabilidad/Facturas").

## 0. Before anything

Call `whoami`. Check `warehouse: true` (the company has the module) and the scopes:

| Scopes | Login level ("Warehouse" in the browser) | Can |
|---|---|---|
| `warehouse:schema:read`, `object:read`, `file:read` | Lectura | look and download |
| + `warehouse:object:*`, `file:*` | Documentos | upload, edit, send to approval, approve |
| `warehouse:schema:*` too | Completo | also types, fields, folders and approval flows |

Missing a scope (403 "no tiene los scopes requeridos") means the login was approved at a lower
level: `auth_login` again and ask the user to choose the right Warehouse level (zibloh-setup).
Approval flows also need the company flag `warehouse_approvals` (`whoami`).

The key acts for the company, except in approvals: **decisions are made as the person who
approved the login**, with their own permissions (they must be an approver of the step or a
warehouse admin).

## 1. Set up a document type

1. `warehouse_types_list` to see what exists (names are unique per company, archived ones too).
2. Propose the fields to the user before creating. Field types: `text`, `textarea`, `number`,
   `date` (values `YYYY-MM-DD`), `boolean`, `list` (of texts), `table` (lines; give `columns`,
   scalar types only). Mark `required` what must be filled before approval.
3. `warehouse_types_create(name, path="Folder/Sub/Name", fields=[…], primary_keys=[…])`.
   `primary_keys` are the fields that identify a document (used to compare linked documents).
4. Change later with `warehouse_fields_add` / `warehouse_fields_update` (rename, type, required;
   a rename moves the data of every document, approved ones are locked and reported) /
   `warehouse_fields_remove`, and `warehouse_types_update` for name, keys and statuses.
   Change the type's fields first, then the documents that must fit the new fields.
5. Folders: `warehouse_folders_rename` moves a folder with everything below.
   `warehouse_folders_delete` and `warehouse_types_archive` only when the user asks; say first
   how many types and documents are affected (the delete is permanent).

## 2. Documents

- Find: `warehouse_docs_list(type, search, status, filters, sort_by)` pages of up to 100;
  follow `has_more` with `offset`. `status`: `uploaded`, `missing_data`, `in_approval`,
  `approved`, `rejected`. Filters (AND):
  `{"col": "content.Importe", "op": "range", "min": 1000}`,
  `{"col": "content.Proveedor", "op": "in", "values": ["ACME"]}`,
  `{"col": "name", "op": "contains", "text": "2026"}`; `col` also takes `created_time`.
- Create: `warehouse_docs_create(type, content={field: value}, file="/abs/path.pdf")`. Use the
  exact field names of the type. If the upload fails the document is removed, so a retry is safe.
  Before loading many files, check the type's primary keys against existing documents to avoid
  duplicates.
- Files: `warehouse_docs_upload` (replace the file), `warehouse_docs_download(id, dest=dir)` saves
  locally; read the saved file if you need its content. `warehouse_docs_export(type, dest,
  what=csv|files)` for many at once.
- Edit: `warehouse_docs_update(id, content={…})` merges; `null` clears a field. Approved
  documents are locked (409).
- `warehouse_docs_archive` (reversible with `restore`) is preferred over `warehouse_docs_delete`.

## 3. Approval flows

1. `warehouse_approvers_eligible(type)`: only people with at least read access to the type
   can approve. Confirm the approvers with the user (by email).
2. `warehouse_flows_create(name, type, steps=[…])`. A step:
   `{"name", "requirement": "any"|"all"|"quorum", "quorum_count", "rule", "approvers": [emails]}`.
   Steps are in order unless `any_order`. A step applies to a document only if its `rule`
   holds; otherwise it is skipped. Example: amounts over 3,000 € also need management:
   ```json
   {"name": "Dirección", "approvers": ["dir@acme.com"],
    "rule": {"conditions": [{"field": "Importe", "op": "gt", "value": 3000}]}}
   ```
   Ops: `eq neq gt lt gte lte contains not_contains starts_with ends_with in not_in`; later
   conditions carry `"connector": "y"` (and) or `"o"` (or), evaluated left to right. A missing
   field never matches. With `linked_types`, rules can use linked documents:
   `{"type": "linked_sum", "field": "Importe", "op": "neq", "link_schema_id": "<type id>", "sum_field": "Base"}`.
3. Adjust with `warehouse_steps_add/update/remove`, `warehouse_approvers_add/remove`,
   `warehouse_flows_update` (deactivating cancels approvals in progress: warn the user).
   Check the result with `warehouse_flows_get`.

## 4. Approvals

- `warehouse_approvals_start(document)` sends a document to its type's active flow. Required
  fields must be filled first (422 lists the missing ones).
- `warehouse_approvals_inbox()` lists what waits for the user (`bucket: my_turn`).
- `warehouse_approvals_status(document)` shows steps, approvers and decisions.
- `warehouse_approvals_decide(document, approve|reject|request_changes, comment)`: **only when
  the user has decided, document by document**. Never approve on your own judgement, never in
  bulk without an explicit list from the user. Reject needs a comment. Show the document's data
  (and offer to download its file) before asking.
- `warehouse_approvals_force` skips approvers and order; warehouse admins only, explicit request.

## 5. Linked documents

`warehouse_links_create(primary, member_ids, fields)` links documents of the same folder (e.g.
delivery notes to an invoice) and compares `fields` (default: the primary keys); it adds to an
existing link instead of duplicating it. `warehouse_links_list(primary)` shows each member's
`match_status` and field results; `warehouse_links_mark` resolves by hand;
`warehouse_links_remove` unlinks.

## Troubleshooting

| Symptom | Fix |
|---|---|
| 403 module 'warehouse' not enabled | Ask Zibloh to enable the Warehouse for the company |
| 403 missing scopes / "no permite el uso por MCP" | Log in again with the right Warehouse level and MCP ticked |
| 403 "Requiere administrador de Warehouse" | The action needs a warehouse admin (flows, force-approve) |
| 403 on a decision "No eres responsable de este paso" | The logged-in person is not an approver of that step |
| 409 "Documento aprobado: los datos están bloqueados" | Approved data can't change |
| 422 "Faltan datos obligatorios" on start | Fill the listed fields with `warehouse_docs_update` |
| Upload/download blocked by Claude's network allowlist | Files go through `app.zibloh.com/files/warehouse/…`: allow `app.zibloh.com` (zibloh-setup). A URL on `amazonaws.com` means the server is not routing files through the app domain yet: tell Zibloh |

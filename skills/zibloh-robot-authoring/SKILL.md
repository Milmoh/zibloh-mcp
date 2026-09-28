---
name: zibloh-robot-authoring
description: The Zibloh robot contract and how to write a correct robot (main.py, config.json, requirements.txt) that runs on the cloud Lambda runner and on Windows/Linux launchers. Use when writing, editing, reviewing or debugging the Python code of a Zibloh robot, declaring its options, reading its secrets, choosing its dependencies, producing mail.html reports, or when a run fails because of the output protocol, timeouts, imports or persistence.
---

# Writing a Zibloh robot

A robot is a directory with `main.py` (entrypoint), `config.json` (declared options), optional
`requirements.txt`, vendored modules and data files. `zibloh robots init <dir>` copies the
template: `--template basic` gives `main.py` with every helper below (`get_option`,
`load_secrets`, `emit_progress`, `emit_runner_result`, `write_mail_artifact`, counters);
`--template browser` gives a Playwright robot with `build_context()` (headless Chromium when run
by a runner, persistent local Chrome profile otherwise) and `capture_browser_debug_artifacts()`.
Start from them instead of rewriting helpers. For Datawarehouse robots, the template repo's
`main_warehouse.py` documents the SDK pitfalls.

## Runners at a glance

| | Cloud (Lambda) | Windows launcher | Linux launcher (Docker) |
|---|---|---|---|
| Time limit | `max_execution_time` min, hard cap 10 min (default 10) | 30 min default, `max_execution_time` overrides (1 h fallback watchdog) | `max_execution_time`, capped by launcher (default 600 s) |
| Dependencies | Image only + vendored pure-Python code. No pip at runtime | venv per robot, `pip install -r requirements.txt` on each new version | `pip install -r requirements.txt` on every run (image is plain Python) |
| Package dir | `/tmp/robot-run-<id>/package` | launcher userData `robots/<robot_id>/` (CWD) | `/app/package`, read-only |
| Scratch | `/tmp` (wiped per run) | `temp/` in robot dir or system temp | `/tmp` tmpfs; rootfs read-only |
| Persistence | Data files in package dir saved as state (rules below) | Robot dir stays on disk | None between runs |
| Secrets | env vars / files injected directly | env vars / files injected directly | `/app/secrets.json`, applied by `load_secrets()` |
| Final status | exit code + last status JSON + log heuristics | exit code + last status JSON | exit code only |

The same code must work on all three: always emit the final JSON line **and** exit non-zero on
failure.

## Configuration and options

- Declare every configurable value in `config.json` → `opciones` with `option_type: "custom"`,
  `name`, `display_name`, `data_type` (`string|integer|number|boolean|json`), `value`, `default`.
  `zibloh robots options define <id> --from-config config.json` publishes them. Keep the platform
  options (`timezone`, `cron`, `notifications_enabled`, `notification_emails`,
  `max_execution_time` in minutes, `auto_execute`).
- At run time every runner **overwrites** `config.json` with the platform's options, so the
  zipped values are only local-dev defaults. Values set with `options set` win.
- Resolution in `get_option(name)`: `ROBOT_PARAMS_JSON` (typed dict, set by cloud and Windows;
  absent on Linux) → `config.json` `opciones` (`value`, else `default`; always strings) → default.
- Therefore **normalize types yourself**: the same option can arrive as `True` or `"true"`,
  `5` or `"5"`. Use helpers like `opt_bool`/`opt_int` in the skeleton.
- Always add two safety options: `TEST_MODE` (boolean, default `true`: read-only, small batch, no
  writes to external systems) and `TEST_EMAIL_REDIRECT` (string: when set, every email the robot
  sends goes only there).
- Validate required options at start and fail fast with a clear message.

## Secrets

- Never hardcode credentials, and never put them in `config.json`, options, `robot.env.json`, a
  zipped `.env` or code. The platform injects bound secrets.
- Call `load_secrets()` first thing in `main()`. On Linux it reads `/app/secrets.json` and sets
  the env vars / writes files to `/tmp/zibloh-secrets/`; on cloud and Windows the file does not
  exist and it is a no-op (values are already injected).
- `expose_as env`: `os.environ["<target_env_name>"]` holds the value.
- `expose_as file`: `os.environ["<target_env_name>"]` holds the **path** of a temp file
  (named `mount_filename`, default the env name lowercased) with the decoded content. Open it by
  path; do not copy it into the package dir (it would be persisted as state on cloud).
- Read with `os.environ.get(...)` and fail clearly if a required one is missing. Never print or
  log values, lengths or prefixes; log only `configured: true/false`.

## Output protocol (stdout)

Every stdout line should be one JSON object; use `print(json.dumps(...), flush=True)`.

Progress:
```json
{"status": "running", "log_message": "Procesando facturas.", "processed_count": 3, "processed_count_detail": "Se han procesado 3 facturas."}
```
Final line, success (exit 0):
```json
{"status": "success", "log_message": "Robot ejecutado correctamente.", "processed_count": 25, "processed_count_detail": "Se han procesado 25 facturas.", "summary": {"skipped": 2}}
```
Final line, failure (also the short error on stderr, then `sys.exit(1)`):
```json
{"status": "error", "log_message": "El robot finalizo con error.", "processed_count": 7, "processed_count_detail": "Se han procesado 7 facturas antes del fallo.", "error_detail": "RuntimeError: Falta la opcion ERP_URL"}
```

Rules:
- The final result must be the **last** stdout line. Nothing after it, including library output.
- `processed_count` starts at 0 and only grows; one unit = one real piece of work (email,
  invoice, document). Send it and `processed_count_detail` in every line, errors included.
- Never write `error`, `exception`, `traceback`, `fatal`, `not found`, `no se pudo`, `access
  denied`, `permission denied` in `running` logs: the cloud runner scans logs for these when the
  last line is not an explicit success. Say "incidencia", "aviso", "omitido".
- Keep `log_message` short and user-facing (it is shown in the web app and in emails). Keep logs
  concise: the cloud keeps only the first and last 5 MB of each stream.
- Wrap `main()` in `try/except Exception`: stderr line, final error JSON, `sys.exit(1)`.

## Reports and email (mail.html)

- Write the HTML report to `$ROBOT_OUTPUT_DIR/mail.html` and also `/tmp/app/output/mail.html`
  (`write_mail_artifact()` does both). When `ROBOT_OUTPUT_DIR` is unset (Linux), the template
  falls back to `/app/output`. On Windows, `config.json` also has `report_path`: if set, write the
  same HTML there too; never fail if it is not writable.
- If the platform sends notifications (`robot_handles_notifications=false`), it emails
  `mail.html`, or a default template with the log tail if there is none. Cloud and Linux also
  honor `mail_subject` and a `notification` block (`request_notification()`) in the final JSON.
- If the robot sends email itself, honor `TEST_EMAIL_REDIRECT` and `TEST_MODE`.
- Debug files (screenshots, HTML dumps) go to `$ROBOT_DEBUG_ARTIFACT_DIR` when set.

## Persistence and filesystem

- Cloud: fresh sandbox per run (own `HOME`, `/tmp` wiped, child processes killed). Data files the
  robot creates or changes **inside the package dir** (`state.json`, `token.json`, `.csv`) are
  saved and restored next run. Never persisted: `.py/.pth/.so/.exe/.sh/.js` etc., `config.json`,
  `requirements.txt`, hidden files/dirs, `__pycache__/`, `output/`, `.venv/`. Deletions do not
  propagate (overwrite with empty state instead). Max 50 MB zipped. Cancelled runs save nothing.
- Windows: the robot dir persists; a new deploy extracts over it.
- Linux: nothing persists. A robot that needs state between runs must keep it externally or
  run on cloud/Windows.
- Scratch files always go to `/tmp` (or `tempfile`), never next to the code.

## Dependencies

- Cloud image (Python 3.12) includes: boto3, requests, httpx, pandas, numpy, openpyxl, pypdf,
  PyMuPDF (`fitz`), pdf2image (+poppler), opencv-python-headless (`cv2`), Pillow, playwright +
  Chromium headless, odoorpc, gspread, google-api-python-client, google-auth(-oauthlib),
  google-genai, python-dotenv, imap_tools, openai, pg8000, SQLAlchemy, zstandard, msal. No
  tesseract binary, no GUI. Anything else: vendor pure-Python code in the zip (the package dir is
  on `PYTHONPATH`) or run on a launcher. `browser_runtime` (Lambda-safe `scrape_page`) is
  importable on cloud only.
- On-prem: list every import in `requirements.txt` with pinned versions (`==`), including
  playwright for browser robots (Linux browser robots also need a `zibloh-runtime.json` with
  `{"runtime_mode": "docker", "profile": "scraping"}`; ask the launcher admin whether that
  profile exists).
- Do not ship `.venv/`, `__pycache__/`, browser profiles, credential files or large test data.

## Naming

- Never use `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`, `AWS_REGION` or
  other `AWS_*`/`ROBOT_*`/`ZIBLOH_LICENSE*` names for robot settings: the runner uses them (cloud
  injects optional Textract credentials under the AWS names). Namespace:
  `OBJECT_STORAGE_ACCESS_KEY_ID`, `ERP_URL`, `SMTP_USER`, `SERVICE_X_TOKEN`.
- For S3-compatible storage pass credentials and `endpoint_url` explicitly to the boto3 client.
- Runner vars you may read: `ROBOT_EXECUTION_ID` (all), `ROBOT_ID`, `ROBOT_COMPANY_ID`,
  `ROBOT_PACKAGE_DIR`, `ROBOT_OUTPUT_DIR`, `ROBOT_PARAMS_JSON` (cloud, Windows),
  `ROBOT_USER_ID`, `ROBOT_DEBUG_ARTIFACT_DIR`, `ROBOT_EVENT_JSON`, `ROBOT_AWS_REQUEST_ID` (cloud).
  Always provide fallbacks.

## Minimal skeleton

Prefer the template's `main.py`; if you write from scratch, keep this shape.

```python
import json, os, sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
IN_DOCKER = BASE_DIR.name == "package"          # Linux launcher: /app/package
SECRETS_PATH = (BASE_DIR.parent if IN_DOCKER else BASE_DIR) / "secrets.json"
OUTPUT_DIR = Path(os.getenv("ROBOT_OUTPUT_DIR") or (BASE_DIR.parent / "output" if IN_DOCKER else BASE_DIR / "output"))
count, detail = 0, None

def emit(status, msg, **extra):
    payload = {"status": status, "log_message": msg, "processed_count": count}
    if detail:
        payload["processed_count_detail"] = detail
    print(json.dumps({**payload, **extra}, ensure_ascii=True), flush=True)

def load_secrets():                               # no-op unless secrets.json exists (Linux)
    import base64
    if not SECRETS_PATH.is_file():
        return
    for s in json.loads(SECRETS_PATH.read_text("utf-8")).get("secrets") or []:
        name, value = s.get("target_env_name"), s.get("value") or ""
        if not name:
            continue
        if s.get("expose_as") == "env":
            os.environ[name] = value
        elif s.get("expose_as") == "file":
            d = Path("/tmp/zibloh-secrets"); d.mkdir(parents=True, exist_ok=True)
            p = d / (s.get("mount_filename") or name.lower())
            p.write_bytes(base64.b64decode(value) if value else b"")
            os.environ[name] = str(p)

def get_option(name, default=None):
    try:
        params = json.loads(os.getenv("ROBOT_PARAMS_JSON") or "{}")
    except json.JSONDecodeError:
        params = {}
    if params.get(name) not in (None, ""):
        return params[name]
    cfg_path = BASE_DIR / "config.json"
    cfg = json.loads(cfg_path.read_text("utf-8")) if cfg_path.is_file() else {}
    for o in cfg.get("opciones") or cfg.get("robot_options") or []:
        if o.get("name") == name:
            v = o.get("value") if o.get("value") not in (None, "") else o.get("default")
            return default if v in (None, "") else v
    return default

def opt_bool(name, default=False):
    v = get_option(name, default)
    return v if isinstance(v, bool) else str(v).strip().lower() in {"1", "true", "yes", "y", "on"}

def opt_int(name, default=0):
    return int(float(get_option(name, default)))

def main():
    global count, detail
    load_secrets()
    test_mode = opt_bool("TEST_MODE", True)
    erp_url = get_option("ERP_URL")
    token = os.environ.get("ERP_TOKEN")
    if not erp_url or not token:
        raise RuntimeError("Falta configuracion: ERP_URL o secreto ERP_TOKEN")
    emit("running", "Configuracion validada.", test_mode=test_mode)
    items = range(opt_int("MAX_ITEMS", 5) if test_mode else 1000)
    for _ in items:
        # ... real work; skip external writes when test_mode ...
        count += 1
        detail = f"Se han procesado {count} documentos."
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUTPUT_DIR / "mail.html").write_text(f"<h1>Robot finalizado</h1><p>{detail}</p>", "utf-8")
    emit("success", "Robot ejecutado correctamente.")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"
        print(err, file=sys.stderr, flush=True)
        emit("error", "El robot finalizo con error.", error_detail=err)
        sys.exit(1)
```

## Checklist before deploy

- [ ] `main.py` at the root; `python3 -m py_compile main.py` passes.
- [ ] `load_secrets()` is the first call; every secret read from `os.environ`, none hardcoded,
      none in `config.json`/options/logs; each has a matching `robots secrets bind --env-name`.
- [ ] Every option the code reads is declared in `config.json`; types normalized.
- [ ] `TEST_MODE` (default true) and `TEST_EMAIL_REDIRECT` exist and are honored.
- [ ] Final JSON is the last stdout line; failure path writes stderr, error JSON, `sys.exit(1)`.
- [ ] No failure words in `running` logs; `processed_count` and detail in every line.
- [ ] Fits the runner's time limit (cloud: under 10 min, with margin); long jobs batch and keep
      progress in a state file (cloud/Windows).
- [ ] Imports: cloud = image or vendored; on-prem = pinned in `requirements.txt`.
- [ ] State only as data files in the package dir; scratch in `/tmp`.
- [ ] No `AWS_*` names for robot settings; no credential files, `.env`, `.venv` in the dir.
- [ ] `zibloh robots validate <dir> --target cloud|onprem` shows no errors.

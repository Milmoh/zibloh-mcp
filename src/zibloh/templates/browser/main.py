from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any

from playwright.sync_api import Browser
from playwright.sync_api import BrowserContext
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ROBOT_ENV_JSON_PATH = BASE_DIR / "robot.env.json"
STORAGE_STATE_PATH = BASE_DIR / "storage_state.json"
DEFAULT_TARGET_URL = "https://example.com/"
DEFAULT_DEBUG_ARTIFACT_DIR = Path("/tmp/zibloh-robot-debug")
DEFAULT_OUTPUT_DIR = BASE_DIR.parent / "output" if BASE_DIR.name == "package" else BASE_DIR / "output"
OUTPUT_DIR = Path(os.getenv("ROBOT_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
# Ruta antigua que la plataforma todavia lee: /tmp/app/output/mail.html
LEGACY_OUTPUT_DIR = Path("/tmp/app/output")
processed_count = 0
processed_count_detail: str | None = None


def load_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_runner_params() -> dict[str, Any]:
    raw = os.getenv("ROBOT_PARAMS_JSON", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def load_config_options() -> dict[str, Any]:
    data = load_json_file(CONFIG_PATH)
    options = data.get("robot_options") or data.get("opciones") or []

    resolved: dict[str, Any] = {}
    for option in options:
        if not isinstance(option, dict):
            continue
        name = option.get("name") or option.get("nombre")
        if not isinstance(name, str) or not name.strip():
            continue
        value = option.get("value")
        if value in (None, ""):
            value = option.get("default")
        if value in (None, ""):
            value = option.get("valor_por_defecto")
        resolved[name.strip()] = value
    return resolved


def load_robot_env_json() -> dict[str, Any]:
    return load_json_file(ROBOT_ENV_JSON_PATH)


def get_nested_value(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def get_option(name: str, default: Any = None) -> Any:
    runner_params = load_runner_params()
    if name in runner_params and runner_params[name] not in (None, ""):
        return runner_params[name]

    config_options = load_config_options()
    if name in config_options and config_options[name] not in (None, ""):
        return config_options[name]

    robot_env = load_robot_env_json()
    params = robot_env.get("params", {})
    if isinstance(params, dict) and name in params and params[name] not in (None, ""):
        return params[name]

    value = os.getenv(name)
    if value not in (None, ""):
        return value

    return default


def get_processed_count() -> int:
    return processed_count


def get_processed_count_detail() -> str | None:
    return processed_count_detail


def increment_processed_count(amount: int = 1) -> int:
    global processed_count
    if amount < 0:
        raise ValueError("processed_count no puede decrementarse")
    processed_count += amount
    return processed_count


def set_processed_count_detail(detail: str | None) -> str | None:
    global processed_count_detail
    cleaned = str(detail).strip() if detail is not None else ""
    processed_count_detail = cleaned or None
    return processed_count_detail


def is_lambda_runtime() -> bool:
    return any(
        bool(os.getenv(name, "").strip())
        for name in (
            "AWS_LAMBDA_FUNCTION_NAME",
            "ROBOT_EXECUTION_ID",
            "ROBOT_PACKAGE_DIR",
        )
    )


def emit_progress(message: str, **extra: Any) -> None:
    aws_request_id = os.getenv("ROBOT_AWS_REQUEST_ID", "").strip() or os.getenv("AWS_REQUEST_ID", "").strip()
    payload = {
        "status": "running",
        "log_message": message,
        "processed_count": get_processed_count(),
    }
    detail = get_processed_count_detail()
    if detail:
        payload["processed_count_detail"] = detail
    if aws_request_id:
        payload["log_id"] = aws_request_id
        payload["aws_request_id"] = aws_request_id
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def emit_runner_result(status: str, log_message: str, **extra: Any) -> None:
    aws_request_id = os.getenv("ROBOT_AWS_REQUEST_ID", "").strip() or os.getenv("AWS_REQUEST_ID", "").strip()
    payload = {
        "status": status,
        "log_message": log_message,
        "processed_count": get_processed_count(),
    }
    detail = get_processed_count_detail()
    if detail:
        payload["processed_count_detail"] = detail
    if aws_request_id:
        payload["log_id"] = aws_request_id
        payload["aws_request_id"] = aws_request_id
    payload.update(extra)
    print(json.dumps(payload, ensure_ascii=True), flush=True)


def build_mail_html(status: str, title: str, message: str, **extra: Any) -> str:
    rows = [
        ("Estado", status),
        ("Procesos realizados", str(get_processed_count())),
        ("Detalle", get_processed_count_detail() or ""),
        ("Fecha UTC", datetime.now(timezone.utc).isoformat()),
    ]
    for key, value in extra.items():
        rows.append((key, value))

    rows_html = "\n".join(
        f"<tr><th>{escape(str(label))}</th><td>{escape(str(value))}</td></tr>"
        for label, value in rows
        if value not in (None, "")
    )
    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>{escape(title)}</title>
</head>
<body style="font-family: Arial, sans-serif; color: #031240; line-height: 1.5;">
  <h1>{escape(title)}</h1>
  <p>{escape(message)}</p>
  <table cellpadding="6" cellspacing="0" style="border-collapse: collapse;">
    {rows_html}
  </table>
</body>
</html>
"""


def write_mail_artifact(subject: str, html_content: str) -> Path:
    mail_path = OUTPUT_DIR / "mail.html"
    written_paths = []
    for output_dir in dict.fromkeys((OUTPUT_DIR, LEGACY_OUTPUT_DIR)):
        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / "mail.html"
        path.write_text(html_content, encoding="utf-8")
        written_paths.append(str(path))
    emit_progress(
        "HTML de correo preparado para archivado por el launcher.",
        mail_subject=subject,
        mail_artifact_path=str(mail_path),
        mail_artifact_paths=written_paths,
    )
    return mail_path


def _safe_artifact_label(label: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9_.-]+", "-", label.strip())
    normalized = normalized.strip(".-")
    return normalized or "browser-error"


def capture_browser_debug_artifacts(page: Page, label: str = "browser-error") -> dict[str, str]:
    artifact_dir = Path(os.getenv("ROBOT_DEBUG_ARTIFACT_DIR", str(DEFAULT_DEBUG_ARTIFACT_DIR)))
    artifact_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    safe_label = _safe_artifact_label(label)
    screenshot_path = artifact_dir / f"{timestamp}-{safe_label}.png"
    html_path = artifact_dir / f"{timestamp}-{safe_label}.html"
    metadata_path = artifact_dir / f"{timestamp}-{safe_label}.json"

    artifacts: dict[str, str] = {}
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        artifacts["screenshot_path"] = str(screenshot_path)
    except Exception as exc:
        artifacts["screenshot_error"] = f"{type(exc).__name__}: {exc}"

    try:
        html_path.write_text(page.content(), encoding="utf-8")
        artifacts["html_path"] = str(html_path)
    except Exception as exc:
        artifacts["html_error"] = f"{type(exc).__name__}: {exc}"

    try:
        metadata = {
            "url": page.url,
            "title": page.title(),
            "debug_s3_bucket": os.getenv("ROBOT_DEBUG_S3_BUCKET", ""),
            "debug_s3_prefix": os.getenv("ROBOT_DEBUG_S3_PREFIX", ""),
        }
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=True, indent=2), encoding="utf-8")
        artifacts["metadata_path"] = str(metadata_path)
    except Exception as exc:
        artifacts["metadata_error"] = f"{type(exc).__name__}: {exc}"

    emit_progress("Artefactos debug de navegador guardados.", artifacts=artifacts)
    return artifacts


def resolve_browser_runtime_helpers():
    try:
        from browser_runtime import build_browser_context_kwargs
        from browser_runtime import build_chromium_launch_kwargs
        return build_chromium_launch_kwargs, build_browser_context_kwargs, None
    except ImportError:
        pass

    launch_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--disable-software-rasterizer",
        "--single-process",
        "--no-zygote",
        "--disable-blink-features=AutomationControlled",
    ]

    def _fallback_launch_kwargs() -> dict[str, Any]:
        return {
            "headless": True,
            "chromium_sandbox": False,
            "args": launch_args,
        }

    def _fallback_context_kwargs() -> dict[str, Any]:
        return {
            "viewport": {"width": 1280, "height": 800},
            "user_agent": (
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }

    return _fallback_launch_kwargs, _fallback_context_kwargs, "fallback"


def build_context(playwright: Any) -> tuple[Browser | None, BrowserContext, Page]:
    if is_lambda_runtime():
        build_launch_kwargs, build_context_kwargs, runtime_source = resolve_browser_runtime_helpers()
        emit_progress(
            "Inicializando navegador en Lambda.",
            runtime_source=runtime_source or "runner_browser_runtime",
        )
        browser = playwright.chromium.launch(**build_launch_kwargs())
        emit_progress("Creando browser context en Lambda.")
        context = browser.new_context(**build_context_kwargs())

        if STORAGE_STATE_PATH.is_file():
            storage_data = load_json_file(STORAGE_STATE_PATH)
            cookies = storage_data.get("cookies", [])
            if cookies:
                emit_progress("Cargando cookies desde storage_state.json.", cookie_count=len(cookies))
                context.add_cookies(cookies)

        emit_progress("Creando page en Lambda.")
        page = context.new_page()
        return browser, context, page

    robot_env = load_robot_env_json()
    local_profile_dir = str(
        get_nested_value(
            robot_env,
            "runtime",
            "local_profile_dir",
            default=BASE_DIR / "chrome-profile",
        )
    ).strip()
    browser_channel = str(
        get_nested_value(
            robot_env,
            "runtime",
            "browser_channel",
            default="chrome",
        )
    ).strip()

    profile_path = Path(local_profile_dir).expanduser()
    if not profile_path.is_absolute():
        profile_path = (BASE_DIR / profile_path).resolve()
    profile_path.mkdir(parents=True, exist_ok=True)

    emit_progress("Inicializando navegador local persistente.")
    context = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_path),
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
        channel=browser_channel or "chrome",
    )
    page = context.new_page()
    return None, context, page


def close_browser(playwright: Any, browser: Browser | None, context: BrowserContext | None) -> None:
    if context is not None:
        try:
            context.close()
        except Exception:
            pass
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    if playwright is not None:
        try:
            playwright.stop()
        except Exception:
            pass


def run_browser_check() -> None:
    target_url = str(get_option("TARGET_URL", DEFAULT_TARGET_URL)).strip() or DEFAULT_TARGET_URL

    playwright = sync_playwright().start()
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    try:
        browser, context, page = build_context(playwright)
        emit_progress("Abriendo URL objetivo.", url=target_url)
        page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(1500)
        title = page.title()
        increment_processed_count()
        set_processed_count_detail("Se ha procesado 1 pagina objetivo.")
        emit_progress(
            "Primer proceso registrado en el contador.",
            processed_unit_note=(
                "Los procesos realizados pueden ser emails procesados, "
                "documentos procesados, facturas procesadas, etc."
            ),
        )
        mail_subject = "Robot browser ejecutado correctamente"
        write_mail_artifact(
            mail_subject,
            build_mail_html(
                "success",
                mail_subject,
                "El robot browser finalizo correctamente.",
                target_url=page.url,
                title=title,
            ),
        )
        emit_runner_result(
            "success",
            "Robot browser ejecutado correctamente.",
            processed_count_detail=get_processed_count_detail(),
            summary={
                "target_url": page.url,
                "title": title,
                "processed_count_note": (
                    "Los procesos realizados pueden ser emails procesados, "
                    "documentos procesados, facturas procesadas, etc."
                ),
            },
        )
    except Exception:
        if page is not None:
            capture_browser_debug_artifacts(page, "browser-check-error")
        raise
    finally:
        close_browser(playwright, browser, context)


if __name__ == "__main__":
    try:
        run_browser_check()
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        print(error_detail, file=sys.stderr)
        emit_runner_result(
            "error",
            "El robot browser finalizo con error.",
            processed_count_detail=get_processed_count_detail(),
            error_detail=error_detail,
        )
        sys.exit(1)

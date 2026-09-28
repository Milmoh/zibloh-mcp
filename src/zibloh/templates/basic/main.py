from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from typing import Any


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ROBOT_ENV_JSON_PATH = BASE_DIR / "robot.env.json"
DEFAULT_OUTPUT_DIR = BASE_DIR.parent / "output" if BASE_DIR.name == "package" else BASE_DIR / "output"
OUTPUT_DIR = Path(os.getenv("ROBOT_OUTPUT_DIR", str(DEFAULT_OUTPUT_DIR)))
# Ruta legacy concreta que la plataforma mantiene de momento: /tmp/app/output/mail.html
LEGACY_OUTPUT_DIR = Path("/tmp/app/output")
# Solo el launcher local (Docker) monta esto: secretos de robot ya resueltos
# por el backend (ver SECRETS_ARCHITECTURE_RFC.md). En Lambda no existe este
# fichero — los secretos llegan directos como env vars/rutas de archivo.
SECRETS_PATH = BASE_DIR.parent / "secrets.json" if BASE_DIR.name == "package" else BASE_DIR / "secrets.json"
SECRETS_FILE_DIR = Path("/tmp/zibloh-secrets")
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


def load_secrets() -> None:
    """Vuelca los secretos de robot resueltos por el backend a os.environ.

    Solo aplica bajo el launcher local: Lambda ya inyecta los secretos como
    env vars/archivos directamente en el subproceso, sin este fichero. Los
    `env` van a os.environ; los `file` se decodifican de base64 y se escriben
    en un tmpfs por ejecución, con la ruta expuesta vía os.environ.
    """
    import base64

    data = load_json_file(SECRETS_PATH)
    for secret in data.get("secrets") or []:
        if not isinstance(secret, dict):
            continue
        target_env_name = secret.get("target_env_name")
        if not target_env_name:
            continue
        expose_as = secret.get("expose_as")
        value = secret.get("value") or ""
        if expose_as == "env":
            os.environ[target_env_name] = value
        elif expose_as == "file":
            SECRETS_FILE_DIR.mkdir(parents=True, exist_ok=True)
            mount_filename = secret.get("mount_filename") or target_env_name.lower()
            file_path = SECRETS_FILE_DIR / mount_filename
            file_path.write_bytes(base64.b64decode(value) if value else b"")
            os.environ[target_env_name] = str(file_path)


def get_option(name: str, default: Any = None) -> Any:
    runner_params = load_runner_params()
    if name in runner_params and runner_params[name] not in (None, ""):
        return runner_params[name]

    config_options = load_config_options()
    if name in config_options and config_options[name] not in (None, ""):
        return config_options[name]

    return default


def get_nested_value(data: dict[str, Any], *path: str, default: Any = None) -> Any:
    current: Any = data
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


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


def request_notification(
    *,
    subject: str | None = None,
    recipients: list[str] | None = None,
) -> dict[str, Any]:
    """Build a notification block to pass as extra to emit_runner_result.

    Executors (Lambda/launcher) read this block and forward it to the backend
    notification endpoint:
    - subject: overrides mail_subject for this email
    - recipients: overrides the robot's notification_emails option

    The decision to send or not is owned by the platform options
    (notifications_enabled + robot_handles_notifications), not the robot.
    """
    block: dict[str, Any] = {}
    if subject is not None:
        block["subject"] = subject
    if recipients is not None:
        block["recipients_override"] = recipients
    return block


def emit_runner_result(status: str, log_message: str, **extra: Any) -> None:
    aws_request_id = os.getenv("ROBOT_AWS_REQUEST_ID", "").strip() or os.getenv("AWS_REQUEST_ID", "").strip()
    payload: dict[str, Any] = {
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
    print(json.dumps(payload, ensure_ascii=True))


def emit_progress(log_message: str, **extra: Any) -> None:
    aws_request_id = os.getenv("ROBOT_AWS_REQUEST_ID", "").strip() or os.getenv("AWS_REQUEST_ID", "").strip()
    payload: dict[str, Any] = {
        "status": "running",
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
    print(json.dumps(payload, ensure_ascii=True))


def validate_configuration() -> None:
    missing: list[str] = []

    required_options = [
        "CUSTOM_API_URL",
        "CUSTOM_API_TOKEN",
    ]
    for name in required_options:
        if not get_option(name):
            missing.append(name)

    if missing:
        raise RuntimeError(f"Faltan configuraciones criticas: {', '.join(missing)}")


def main() -> None:
    load_secrets()
    robot_execution_id = os.getenv("ROBOT_EXECUTION_ID", "")
    robot_package_dir = os.getenv("ROBOT_PACKAGE_DIR", str(BASE_DIR))
    robot_env = load_robot_env_json()

    emit_progress(
        "Iniciando robot.",
        robot_execution_id=robot_execution_id,
        robot_package_dir=robot_package_dir,
    )

    validate_configuration()

    custom_api_url = str(get_option("CUSTOM_API_URL"))
    custom_api_token = str(get_option("CUSTOM_API_TOKEN"))
    service_a_base_url = get_nested_value(robot_env, "services", "service_a", "base_url", default="")

    emit_progress(
        "Configuracion validada.",
        custom_api_url_configured=bool(custom_api_url),
        custom_api_token_configured=bool(custom_api_token),
        service_a_base_url_configured=bool(service_a_base_url),
    )
    increment_processed_count()
    set_processed_count_detail("Se ha procesado 1 documento.")
    emit_progress(
        "Primer proceso registrado en el contador.",
        processed_unit_note=(
            "Los procesos realizados pueden ser emails procesados, "
            "documentos procesados, facturas procesadas, etc."
        ),
    )
    mail_subject = "Robot ejecutado correctamente"
    write_mail_artifact(
        mail_subject,
        build_mail_html(
            "success",
            mail_subject,
            "El robot finalizo correctamente.",
            custom_api_url=custom_api_url,
            service_a_base_url=service_a_base_url,
        ),
    )
    emit_runner_result(
        "success",
        "Robot ejecutado correctamente.",
        processed_count_detail=get_processed_count_detail(),
        summary={
            "validated_options": [
                "CUSTOM_API_URL",
                "CUSTOM_API_TOKEN",
            ],
            "processed_count_note": (
                "Los procesos realizados pueden ser emails procesados, "
                "documentos procesados, facturas procesadas, etc."
            ),
        },
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        error_detail = f"{type(exc).__name__}: {exc}"
        print(error_detail, file=sys.stderr)
        emit_runner_result(
            "error",
            "El robot finalizo con error.",
            processed_count_detail=get_processed_count_detail(),
            error_detail=error_detail,
        )
        sys.exit(1)

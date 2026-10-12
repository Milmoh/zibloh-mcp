"""Robot package checks and zipping (`zibloh robots validate|deploy`).

Errors block a deploy; warnings are printed and sent to the backend with the deploy
(stored in the audit row only).
"""
from __future__ import annotations

import ast
import hashlib
import py_compile
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

MAX_PACKAGE_BYTES = 1024 * 1024 * 1024  # backend ROBOT_UPLOAD_MAX_SIZE_BYTES
SCAN_MAX_FILE_BYTES = 2 * 1024 * 1024

# Never packaged: local tooling, caches, browser profiles, outputs.
EXCLUDED_DIRS = {
    ".git", ".hg", ".svn", ".venv", "venv", "env", "__pycache__", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".idea", ".vscode", "node_modules", "chrome-profile", "output", ".claude",
}
EXCLUDED_FILES = {".DS_Store", "Thumbs.db"}
EXCLUDED_SUFFIXES = {".pyc", ".pyo", ".zip"}

# Files that are credentials by name. Secrets go in `zibloh secrets`, never in the package.
CREDENTIAL_NAMES = {
    ".env", "robot.env.json", "credentials.json", "client_secret.json", "service_account.json",
    ".netrc", ".npmrc", ".pypirc", "id_rsa", "id_ed25519", "id_ecdsa", "id_dsa",
}
CREDENTIAL_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".keystore", ".jks", ".ppk"}
# Session state: not an error (browser robots rely on it) but it is a credential.
SESSION_FILES = {"storage_state.json", "token.json", "token.pickle", "cookies.json"}

SECRET_PATTERNS = [
    ("AWS access key", re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("Zibloh API key", re.compile(r"\bzbl_[A-Za-z0-9_\-]{20,}")),
    ("OpenAI/Anthropic API key", re.compile(r"\bsk-(?:ant-|proj-)?[A-Za-z0-9_\-]{20,}")),
    ("GitHub token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("Slack token", re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}")),
    ("Google API key", re.compile(r"\bAIza[0-9A-Za-z_\-]{35}\b")),
]

# Extensionless files that are fine to ship.
EXTENSIONLESS_OK = {"LICENSE", "README", "NOTICE", "Makefile", "Dockerfile", "Procfile", "CHANGELOG"}

# Import names available in the cloud runner image (zibloh-launcher-lambda
# requirements.txt / smoke_test.py MANDATORY_IMPORTS), plus their transitive deps
# robots commonly import directly.
CLOUD_IMAGE_MODULES = {
    "boto3", "botocore", "s3transfer", "requests", "urllib3", "certifi", "charset_normalizer", "idna",
    "pandas", "numpy", "openpyxl", "pypdf", "fitz", "pymupdf", "pdf2image", "cv2", "playwright",
    "odoorpc", "gspread", "googleapiclient", "google", "google_auth_oauthlib", "google_auth_httplib2",
    "httplib2", "oauth2client", "dotenv", "imap_tools", "openai", "pg8000", "sqlalchemy", "zstandard",
    "msal", "httpx", "httpcore", "anyio", "sniffio", "h11", "pydantic", "pydantic_core", "dateutil",
    "pytz", "tzdata", "six", "jwt", "PIL", "yaml", "et_xmlfile", "pyautogui", "jmespath",
    "typing_extensions", "packaging", "cryptography", "cffi", "greenlet", "attr", "attrs",
    "zibloh_ai",
    # Helper module of the runner itself (zibloh-launcher-lambda/browser_runtime.py).
    "browser_runtime",
}


# pip distribution names installed in the cloud runner image (its requirements.txt).
CLOUD_IMAGE_DISTRIBUTIONS = {
    "pyautogui", "opencv-python-headless", "google-api-python-client", "google-auth",
    "google-auth-httplib2", "google-auth-oauthlib", "google-genai", "gspread", "oauth2client",
    "boto3", "imap-tools", "odoorpc", "openai", "pg8000", "python-dotenv", "requests", "sqlalchemy",
    "zstandard", "httpx", "numpy", "openpyxl", "pandas", "pdf2image", "pymupdf", "pypdf",
    "playwright", "nuitka", "msal", "zibloh-ai",
}


def _normalize_dist(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def ok(self) -> bool:
        return not self.errors

    def as_dict(self) -> dict:
        return {"ok": self.ok, "errors": self.errors, "warnings": self.warnings,
                "files": len(self.files), "total_bytes": self.total_bytes}


def iter_package_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts[:-1]) or (path.is_dir() and path.name in EXCLUDED_DIRS):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if path.name in EXCLUDED_FILES or path.suffix.lower() in EXCLUDED_SUFFIXES:
            continue
        yield path


def _is_text(path: Path) -> bool:
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return False
    return b"\x00" not in chunk


def _imports(source: str) -> set[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


def _requirement_names(req_file: Path) -> list[tuple[str, str]]:
    """[(name, raw_line)] from requirements.txt, ignoring comments and options."""
    result = []
    for raw in req_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        name = re.split(r"[\s<>=!~;\[@]", line, maxsplit=1)[0]
        result.append((name, line))
    return result


def validate(root: Path, target: Optional[str] = None) -> Report:
    """Check a robot directory. `target`: "cloud", "onprem" or None (both kinds of warnings)."""
    report = Report()
    root = root.resolve()
    if not root.is_dir():
        report.errors.append(f"{root} is not a directory")
        return report

    main = root / "main.py"
    if not main.is_file():
        report.errors.append("main.py is missing: it is the robot entrypoint")

    local_modules = set()
    all_imports: dict[str, set[str]] = {}
    for path in iter_package_files(root):
        rel = path.relative_to(root).as_posix()
        report.files.append(rel)
        size = path.stat().st_size
        report.total_bytes += size
        name = path.name

        if name in CREDENTIAL_NAMES or path.suffix.lower() in CREDENTIAL_SUFFIXES or name.startswith(".env."):
            report.errors.append(f"{rel}: looks like a credentials file; use `zibloh secrets create` and bind it to the robot instead")
        elif name in SESSION_FILES:
            report.warnings.append(f"{rel}: contains session tokens/cookies; prefer a file secret (`zibloh secrets create --file`)")
        if not path.suffix and name not in EXTENSIONLESS_OK and not name.startswith("."):
            report.errors.append(f"{rel}: file without extension; rename it (e.g. .txt, .json) or remove it")

        if path.suffix == ".py":
            local_modules.add(path.stem)
            try:
                py_compile.compile(str(path), doraise=True, cfile=str(Path(tempfile.gettempdir()) / "zibloh_pycompile.pyc"))
            except py_compile.PyCompileError as exc:
                report.errors.append(f"{rel}: does not compile: {exc.msg.strip().splitlines()[-1]}")

        if size <= SCAN_MAX_FILE_BYTES and _is_text(path):
            text = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    report.errors.append(f"{rel}: contains what looks like a {label}; move it to a secret")
            if path.suffix == ".py":
                all_imports[rel] = _imports(text)

    # Top-level directories are importable packages/namespaces too.
    local_modules.update(p.name for p in root.iterdir() if p.is_dir() and p.name not in EXCLUDED_DIRS)

    if report.total_bytes > MAX_PACKAGE_BYTES:
        report.errors.append(f"package is {report.total_bytes / 1e9:.2f} GB; the limit is 1 GB")

    if main.is_file():
        source = main.read_text(encoding="utf-8", errors="replace")
        has_result = "emit_runner_result" in source or re.search(r"""["']status["']\s*:\s*["']success["']""", source)
        has_exit = re.search(r"\b(sys\.)?exit\(\s*1\s*\)", source)
        if not has_result:
            report.errors.append('main.py never prints a final result line ({"status": "success"|"error", ...}); use emit_runner_result()')
        if not has_exit:
            report.errors.append("main.py has no sys.exit(1) path: a failed run must exit non-zero")

    stdlib = set(sys.stdlib_module_names)
    third_party = sorted({
        name for names in all_imports.values() for name in names
        if name not in stdlib and name not in local_modules and name != "__future__"
    })
    req_file = root / "requirements.txt"
    requirements = _requirement_names(req_file) if req_file.is_file() else []

    if target in (None, "cloud"):
        missing = [m for m in third_party if m not in CLOUD_IMAGE_MODULES]
        if missing:
            report.warnings.append(
                "cloud: not in the runner image and not vendored: " + ", ".join(missing)
                + " (the cloud runner does not pip install; vendor the code in the package or run on a launcher)"
            )
        extra = [name for name, _ in requirements if _normalize_dist(name) not in CLOUD_IMAGE_DISTRIBUTIONS]
        if extra:
            report.warnings.append("cloud: requirements.txt is not installed by the cloud runner: " + ", ".join(extra))
    if target in (None, "onprem"):
        unpinned = [line for name, line in requirements if "==" not in line]
        if unpinned:
            report.warnings.append("on-prem: unpinned requirements (pin with ==): " + ", ".join(unpinned))
    return report


@dataclass
class Package:
    path: Path
    size: int
    sha256: str
    files: int


def build_zip(root: Path, dest_dir: Optional[Path] = None) -> Package:
    root = root.resolve()
    dest_dir = dest_dir or Path(tempfile.mkdtemp(prefix="zibloh-"))
    out = dest_dir / f"{root.name or 'robot'}.zip"
    count = 0
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path in iter_package_files(root):
            zf.write(path, path.relative_to(root).as_posix())
            count += 1
    digest = hashlib.sha256()
    with out.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return Package(path=out, size=out.stat().st_size, sha256=digest.hexdigest(), files=count)

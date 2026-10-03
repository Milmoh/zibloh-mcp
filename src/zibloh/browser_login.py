"""Browser login (OAuth 2.0 device authorization, RFC 8628) against `/cli-auth`.

1. `start()` asks the backend for a device code and a short user code, and builds the web
   URL `<web>/cli-login?code=XXXX-XXXX`.
2. The user opens it (we try to open the browser), checks the code and clicks Autorizar.
3. `wait()` polls until the backend hands out the new API key, once, and saves it as a
   profile. The key goes backend → this process → `credentials.json`: no human sees it,
   so there is nothing to paste anywhere.
"""
from __future__ import annotations

import os
import platform
import socket
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlsplit

import httpx

from zibloh import __version__, config

TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class LoginFailed(Exception):
    """Denied, expired, or the backend refused the login."""


@dataclass
class Pending:
    base_url: str
    env: Optional[str]
    device_code: str
    user_code: str
    verification_url: str
    expires_at: float
    interval: int


def web_url(base_url: str) -> str:
    """The web app's origin. In dev/prod it is the API's origin (CloudFront serves both);
    `ZIBLOH_WEB_URL` overrides it (local frontend on another port)."""
    override = os.environ.get("ZIBLOH_WEB_URL")
    if override:
        return override.rstrip("/")
    parts = urlsplit(base_url)
    return f"{parts.scheme}://{parts.netloc}"


def device_name() -> str:
    try:
        return socket.gethostname()[:100] or platform.node()[:100]
    except OSError:
        return platform.node()[:100]


def _post(http: httpx.Client, url: str, body: dict) -> httpx.Response:
    try:
        return http.post(url, json=body, headers={"User-Agent": f"zibloh/{__version__}"})
    except httpx.HTTPError as exc:
        raise LoginFailed(f"cannot reach {url}: {exc}") from exc


def start(base_url: str, env: Optional[str], client: str = "cli",
          transport: Optional[httpx.BaseTransport] = None) -> Pending:
    with httpx.Client(timeout=TIMEOUT, transport=transport) as http:
        response = _post(http, f"{base_url}/cli-auth/device", {"client": client, "client_name": device_name()})
    if response.status_code != 200:
        raise LoginFailed(f"{response.status_code} starting the login: {response.text[:300]}")
    data = response.json()
    url = f"{web_url(base_url)}{data['verification_path']}?code={data['user_code']}"
    return Pending(base_url, env, data["device_code"], data["user_code"], url,
                   time.monotonic() + data["expires_in"], data["interval"])


def open_browser(url: str) -> bool:
    """Best effort. In WSL `webbrowser` usually finds nothing, so try the Windows side."""
    try:
        if "microsoft" in platform.release().lower():
            for cmd in (["wslview", url], ["explorer.exe", url]):
                try:
                    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return True
                except OSError:
                    continue
        return webbrowser.open(url, new=2)
    except Exception:  # noqa: BLE001 - never fail the login because of the browser
        return False


def poll_once(pending: Pending, transport: Optional[httpx.BaseTransport] = None) -> Optional[dict]:
    """The key payload once approved, None while pending. Raises LoginFailed when it cannot succeed."""
    if time.monotonic() > pending.expires_at:
        raise LoginFailed("The login code expired. Run the login again.")
    with httpx.Client(timeout=TIMEOUT, transport=transport) as http:
        response = _post(http, f"{pending.base_url}/cli-auth/token", {"device_code": pending.device_code})
    if response.status_code == 200:
        return response.json()
    try:
        error = response.json().get("error")
    except ValueError:
        error = None
    if error == "authorization_pending":
        return None
    if error == "slow_down":
        pending.interval += 5
        return None
    if error == "access_denied":
        raise LoginFailed("The login was rejected in the web app.")
    if error == "expired_token":
        raise LoginFailed("The login code expired. Run the login again.")
    raise LoginFailed(f"{response.status_code} from the login: {response.text[:300]}")


def save(pending: Pending, result: dict, profile: str) -> config.Profile:
    return config.save_profile(profile, result["api_key"], pending.env,
                               None if pending.env else pending.base_url)


def wait(pending: Pending, profile: str, sleep: Callable[[float], None] = time.sleep,
         transport: Optional[httpx.BaseTransport] = None) -> dict:
    """Poll until approved; save the key under `profile`. Returns the payload without the key."""
    while True:
        result = poll_once(pending, transport)
        if result is not None:
            save(pending, result, profile)
            return {k: v for k, v in result.items() if k != "api_key"}
        sleep(pending.interval)

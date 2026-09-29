"""The only place that talks HTTP to the Zibloh API.

Every request carries the audit headers the backend stores per call:
`X-Zibloh-Client` (cli|mcp), `X-Zibloh-Tool` (command or MCP tool), `X-Zibloh-Session`
(one per process, or `ZIBLOH_SESSION`) and a fresh `X-Request-Id`.
Presigned S3 uploads go through `upload()`, which never sends the API key.
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

import httpx

from zibloh import __version__
from zibloh.config import Profile

SESSION_ID = os.environ.get("ZIBLOH_SESSION") or f"zs-{uuid.uuid4().hex[:16]}"
TIMEOUT = httpx.Timeout(60.0, connect=15.0)
UPLOAD_TIMEOUT = httpx.Timeout(900.0, connect=30.0)


class ApiError(Exception):
    def __init__(self, status: int, detail: Any, request_id: Optional[str] = None, method: str = "", path: str = ""):
        self.status = status
        self.detail = detail
        self.request_id = request_id
        self.method = method
        self.path = path
        super().__init__(self.message)

    @property
    def message(self) -> str:
        detail = self.detail if isinstance(self.detail, str) else _format_detail(self.detail)
        hint = _HINTS.get(self.status, "")
        rid = f" (request {self.request_id})" if self.request_id else ""
        return f"{self.status} {self.method} {self.path}: {detail}{rid}{hint}"


_HINTS = {
    401: " — the API key is invalid, revoked or expired; log in again with `zibloh login` (or the auth_login tool).",
    429: " — rate limited; wait a minute before retrying.",
}


def _format_detail(detail: Any) -> str:
    # FastAPI validation errors: [{"loc": [...], "msg": "..."}]
    if isinstance(detail, list):
        parts = []
        for item in detail:
            if isinstance(item, dict):
                loc = ".".join(str(p) for p in item.get("loc", []) if p != "body")
                parts.append(f"{loc}: {item.get('msg')}" if loc else str(item.get("msg")))
            else:
                parts.append(str(item))
        return "; ".join(parts)
    return str(detail)


class Client:
    def __init__(self, profile: Profile, client_name: str = "cli", transport: Optional[httpx.BaseTransport] = None):
        self.profile = profile
        self.client_name = client_name
        self.tool: Optional[str] = None
        self._http = httpx.Client(base_url=profile.base_url, timeout=TIMEOUT, transport=transport)
        self._upload_http = httpx.Client(timeout=UPLOAD_TIMEOUT, transport=transport)

    def close(self) -> None:
        self._http.close()
        self._upload_http.close()

    def _headers(self) -> dict:
        headers = {
            "Authorization": f"Bearer {self.profile.api_key}",
            "User-Agent": f"zibloh/{__version__}",
            "X-Zibloh-Client": self.client_name,
            "X-Zibloh-Session": SESSION_ID,
            "X-Request-Id": f"zc-{uuid.uuid4().hex}",
        }
        if self.tool:
            headers["X-Zibloh-Tool"] = self.tool[:100]
        return headers

    def request(self, method: str, path: str, *, json: Any = None, params: Optional[dict] = None,
                files: Any = None, data: Any = None, allow_404: bool = False) -> Any:
        params = {k: v for k, v in (params or {}).items() if v is not None}
        try:
            response = self._http.request(method, path, json=json, params=params, files=files, data=data, headers=self._headers())
        except httpx.HTTPError as exc:
            raise ApiError(0, f"cannot reach {self.profile.base_url}: {exc}", method=method, path=path) from exc
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text[:500]
            raise ApiError(response.status_code, detail, response.headers.get("x-request-id"), method, path)
        if response.status_code == 204 or not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    def get(self, path: str, **kwargs) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs) -> Any:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs) -> Any:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs) -> Any:
        return self.request("DELETE", path, **kwargs)

    def upload(self, url: str, body: bytes | Path, headers: Optional[dict] = None, offset: int = 0, length: Optional[int] = None) -> httpx.Response:
        """PUT to a presigned URL. No Authorization header: the URL is the credential."""
        if isinstance(body, Path):
            with body.open("rb") as fh:
                fh.seek(offset)
                content = fh.read(length) if length is not None else fh.read()
        else:
            content = body
        try:
            response = self._upload_http.put(url, content=content, headers=headers or {})
        except httpx.HTTPError as exc:
            raise ApiError(0, f"upload failed: {exc}", method="PUT", path="<presigned>") from exc
        if response.status_code >= 400:
            raise ApiError(response.status_code, response.text[:500], method="PUT", path="<presigned>")
        return response

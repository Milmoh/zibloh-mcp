"""Profiles and credentials.

Stored in `~/.config/zibloh/credentials.json` (or `$XDG_CONFIG_HOME/zibloh/`), mode 0600:

    {"default_profile": "default",
     "profiles": {"default": {"env": "prod", "base_url": "...", "api_key": "zbl_..."}}}

Environment variables win over the file: `ZIBLOH_API_KEY`, `ZIBLOH_BASE_URL`, `ZIBLOH_ENV`,
`ZIBLOH_PROFILE`.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

ENVIRONMENTS = {
    "prod": "https://app.zibloh.com/api/v1",
    "dev": "https://devapp.zibloh.com/api/v1",
}
DEFAULT_ENV = "prod"
DEFAULT_PROFILE = "default"


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Profile:
    name: str
    base_url: str
    api_key: str
    env: Optional[str] = None


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "zibloh"


def credentials_path() -> Path:
    return config_dir() / "credentials.json"


def _read() -> dict:
    path = credentials_path()
    if not path.exists():
        return {"default_profile": DEFAULT_PROFILE, "profiles": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"Cannot read {path}: {exc}") from exc
    data.setdefault("default_profile", DEFAULT_PROFILE)
    data.setdefault("profiles", {})
    return data


def _write(data: dict) -> None:
    directory = config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, 0o700)
    path = credentials_path()
    tmp = path.with_suffix(".tmp")
    # Created 0600 from the start: the key never sits in a world-readable file.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def resolve_base_url(env: Optional[str] = None, base_url: Optional[str] = None) -> str:
    if base_url:
        return base_url.rstrip("/")
    env = env or DEFAULT_ENV
    if env not in ENVIRONMENTS:
        raise ConfigError(f"Unknown environment '{env}' (use {', '.join(ENVIRONMENTS)} or --base-url)")
    return ENVIRONMENTS[env]


def save_profile(name: str, api_key: str, env: Optional[str], base_url: Optional[str], make_default: bool = True) -> Profile:
    data = _read()
    profile = Profile(name=name, base_url=resolve_base_url(env, base_url), api_key=api_key, env=None if base_url else (env or DEFAULT_ENV))
    data["profiles"][name] = {"env": profile.env, "base_url": profile.base_url, "api_key": profile.api_key}
    if make_default or not data.get("default_profile"):
        data["default_profile"] = name
    _write(data)
    return profile


def delete_profile(name: str) -> bool:
    data = _read()
    if name not in data["profiles"]:
        return False
    del data["profiles"][name]
    if data.get("default_profile") == name:
        data["default_profile"] = next(iter(data["profiles"]), DEFAULT_PROFILE)
    _write(data)
    return True


def list_profiles() -> dict:
    data = _read()
    return {
        "default_profile": data["default_profile"],
        "profiles": {
            name: {"env": p.get("env"), "base_url": p.get("base_url"), "key_prefix": (p.get("api_key") or "")[:12]}
            for name, p in data["profiles"].items()
        },
    }


def load_profile(name: Optional[str] = None) -> Profile:
    """The active profile, with env var overrides applied."""
    data = _read()
    name = name or os.environ.get("ZIBLOH_PROFILE") or data["default_profile"]
    stored = data["profiles"].get(name, {})

    api_key = os.environ.get("ZIBLOH_API_KEY") or stored.get("api_key")
    if not api_key:
        raise ConfigError(
            "Not logged in. Run `zibloh login` in your terminal (or set ZIBLOH_API_KEY)."
        )
    env_override = os.environ.get("ZIBLOH_ENV")
    base_url = os.environ.get("ZIBLOH_BASE_URL") or (
        resolve_base_url(env_override) if env_override else stored.get("base_url") or resolve_base_url(None)
    )
    return Profile(name=name, base_url=base_url.rstrip("/"), api_key=api_key, env=env_override or stored.get("env"))

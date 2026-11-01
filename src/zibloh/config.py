"""Profiles and credentials.

Stored in `~/.config/zibloh/credentials.json` (or `$XDG_CONFIG_HOME/zibloh/`), mode 0600:

    {"default_profile": "acme",
     "profiles": {"acme": {"env": "prod", "base_url": "...", "api_key": "zbl_...",
                           "company_id": "...", "company_name": "Acme"}}}

A key belongs to ONE company, and a person can belong to several: each login saves its own
profile, named after the company unless one is given (`profile_name_for`), so logging into
another company never overwrites the previous one. `use_profile` switches the default.

Environment variables win over the file: `ZIBLOH_API_KEY`, `ZIBLOH_BASE_URL`, `ZIBLOH_ENV`,
`ZIBLOH_PROFILE`.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
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


def _slug(value: str) -> str:
    ascii_only = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")[:40].strip("-")


def profile_name_for(company_id: Optional[str], company_name: Optional[str], env: Optional[str],
                     base_url: Optional[str]) -> str:
    """Profile name for a login whose caller gave none: the profile already holding this company
    on this API (so a re-login replaces its key), else the company's slug, `-dev` on dev."""
    data = _read()
    base = resolve_base_url(env, base_url)
    for name, stored in data["profiles"].items():
        if company_id and stored.get("company_id") == str(company_id) and stored.get("base_url") == base:
            return name
    name = _slug(company_name or "") or "company"
    if env == "dev" and not base_url:
        name += "-dev"
    if name in data["profiles"]:  # another company (or API) with the same name
        name = f"{name}-{str(company_id or '')[:8]}".rstrip("-")
    return name


def save_profile(name: str, api_key: str, env: Optional[str], base_url: Optional[str], make_default: bool = True,
                 company_id: Optional[str] = None, company_name: Optional[str] = None) -> Profile:
    data = _read()
    profile = Profile(name=name, base_url=resolve_base_url(env, base_url), api_key=api_key, env=None if base_url else (env or DEFAULT_ENV))
    data["profiles"][name] = {"env": profile.env, "base_url": profile.base_url, "api_key": profile.api_key,
                              "company_id": str(company_id) if company_id else None, "company_name": company_name}
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


def use_profile(name: str) -> None:
    """Make `name` the default profile: the CLI and the MCP server use it from the next call."""
    data = _read()
    if name not in data["profiles"]:
        known = ", ".join(sorted(data["profiles"])) or "none; run `zibloh login`"
        raise ConfigError(f"No profile '{name}' (saved profiles: {known})")
    data["default_profile"] = name
    _write(data)


def list_profiles() -> dict:
    """Saved profiles without their keys (only a prefix to tell them apart)."""
    data = _read()
    return {
        "default_profile": data["default_profile"],
        "active_profile": os.environ.get("ZIBLOH_PROFILE") or data["default_profile"],
        "profiles": {
            name: {"company_name": p.get("company_name"), "company_id": p.get("company_id"), "env": p.get("env"),
                   "base_url": p.get("base_url"), "key_prefix": (p.get("api_key") or "")[:12]}
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
            "Not logged in. Run `zibloh login` (it opens the browser to approve this computer), "
            "or from Claude Code use the auth_login tool."
        )
    env_override = os.environ.get("ZIBLOH_ENV")
    base_url = os.environ.get("ZIBLOH_BASE_URL") or (
        resolve_base_url(env_override) if env_override else stored.get("base_url") or resolve_base_url(None)
    )
    return Profile(name=name, base_url=base_url.rstrip("/"), api_key=api_key, env=env_override or stored.get("env"))

"""Configuration and secret management.

Non-secret settings live in a TOML file in the platform config dir. Secrets (API keys)
live in the OS keyring, with environment variables / ``.env`` honored as a fallback.

Runtime precedence for a secret: env var (incl. ``.env``) -> keyring.
"""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from platformdirs import user_config_dir

APP_NAME = "wcfi"
KEYRING_SERVICE = "wcfi"

SECRET_ENV_VARS = {
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}

DEFAULT_CONFIG: dict[str, dict[str, str]] = {
    "providers": {"summarizer": "openai", "transcriber": "openai"},
    "models": {
        "openai_summary": "gpt-5.5",
        "anthropic_summary": "claude-sonnet-5",
        "transcribe": "gpt-4o-transcribe",
    },
}


def config_dir() -> Path:
    return Path(user_config_dir(APP_NAME, appauthor=False))


def config_path() -> Path:
    return config_dir() / "config.toml"


def is_configured() -> bool:
    """True only after `wcfi setup` has completed successfully (it writes meta.configured)."""
    path = config_path()
    if not path.exists():
        return False
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except Exception:  # pragma: no cover - treat an unreadable config as not configured
        return False
    return bool(data.get("meta", {}).get("configured", False))


def load_config() -> dict[str, Any]:
    """Load config from disk, merged over defaults. Also loads ``.env`` into the environment."""
    load_dotenv()  # merges .env into os.environ if present in CWD or parents
    merged = {section: dict(values) for section, values in DEFAULT_CONFIG.items()}
    path = config_path()
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        for section, values in data.items():
            if isinstance(values, dict):
                merged.setdefault(section, {}).update(values)
    return merged


def save_config(config: dict[str, Any]) -> Path:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_dump_toml(config), encoding="utf-8")
    return path


def _dump_toml(config: dict[str, Any]) -> str:
    """Minimal TOML writer for our flat ``[table] key = "value"`` structure."""
    lines: list[str] = []
    for section, values in config.items():
        lines.append(f"[{section}]")
        for key, value in values.items():
            if isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, (int, float)):
                rendered = str(value)
            else:
                escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
                rendered = f'"{escaped}"'
            lines.append(f"{key} = {rendered}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# --- secrets -----------------------------------------------------------------


def _keyring():
    try:
        import keyring

        return keyring
    except Exception:  # pragma: no cover - environment dependent
        return None


def get_secret(provider: str) -> str | None:
    """Return the API key for a provider: env/.env first, then the OS keyring."""
    env_var = SECRET_ENV_VARS.get(provider)
    if env_var and os.environ.get(env_var):
        return os.environ[env_var]
    keyring = _keyring()
    if keyring is not None:
        try:
            return keyring.get_password(KEYRING_SERVICE, env_var or provider)
        except Exception:  # pragma: no cover
            return None
    return None


def set_secret(provider: str, value: str) -> bool:
    """Store a key in the OS keyring. Returns False if no keyring backend is available."""
    env_var = SECRET_ENV_VARS.get(provider, provider)
    keyring = _keyring()
    if keyring is None:
        return False
    try:
        keyring.set_password(KEYRING_SERVICE, env_var, value)
        return True
    except Exception:  # pragma: no cover
        return False


def secret_source(provider: str) -> str | None:
    """Where a secret is coming from ('env', 'keyring'), or None if unset. For doctor output."""
    env_var = SECRET_ENV_VARS.get(provider)
    if env_var and os.environ.get(env_var):
        return "env"
    keyring = _keyring()
    if keyring is not None:
        try:
            if keyring.get_password(KEYRING_SERVICE, env_var or provider):
                return "keyring"
        except Exception:  # pragma: no cover
            return None
    return None

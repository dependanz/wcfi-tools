"""Where the inbox keeps its private data, and the token that guards its API."""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from platformdirs import user_data_dir

from ..private_workspace import initialize_private_directory

APP_ID = "wcfi-inbox"
DEFAULT_PORT = 8787
DB_NAME = "inbox.sqlite3"
TOKEN_NAME = "token"
STATE_NAME = "server.json"
LOG_NAME = "server.log"


def default_data_dir() -> Path:
    """``WCFI_INBOX_DIR`` if set, else the per-user data dir (never inside a repo)."""
    override = os.environ.get("WCFI_INBOX_DIR")
    if override:
        return Path(override)
    return Path(user_data_dir("wcfi", appauthor=False)) / "inbox"


def default_port() -> int:
    raw = os.environ.get("WCFI_INBOX_PORT", "")
    return int(raw) if raw.isdigit() else DEFAULT_PORT


def prepare_data_dir(path: Path | None = None) -> Path:
    """Create (or reuse) the guarded private data dir. Refuses any path inside a Git worktree."""
    return initialize_private_directory(path or default_data_dir())


def load_or_create_token(data_dir: Path) -> str:
    """Return the per-install API token, creating it on first use."""
    path = data_dir / TOKEN_NAME
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if token:
            return token
    token = secrets.token_urlsafe(24)
    path.write_text(token + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:  # pragma: no cover - best effort on Windows
        pass
    return token

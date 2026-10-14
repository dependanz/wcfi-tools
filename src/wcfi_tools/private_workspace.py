"""Guardrails for local archives that must never become repository content."""

from __future__ import annotations

from pathlib import Path

PRIVATE_MARKER = ".wcfi-private"
PRIVATE_GITIGNORE = "*\n!.gitignore\n!.wcfi-private\n"


class UnsafeOutputError(ValueError):
    """Raised when private output would be placed in a Git worktree."""


def git_root_for(path: Path) -> Path | None:
    """Return the closest containing Git root, including worktree .git files."""
    resolved = path.expanduser().resolve(strict=False)
    candidates = (resolved, *resolved.parents)
    for candidate in candidates:
        if (candidate / ".git").exists():
            return candidate
    return None


def require_private_output(path: Path) -> Path:
    """Resolve *path* and refuse any destination inside a Git worktree."""
    resolved = path.expanduser().resolve(strict=False)
    git_root = git_root_for(resolved)
    if git_root is not None:
        raise UnsafeOutputError(
            f"Private archive output cannot be written inside Git worktree {git_root}: {resolved}"
        )
    return resolved


def initialize_private_directory(path: Path) -> Path:
    """Create a guarded private directory with conspicuous local-only markers."""
    resolved = require_private_output(path)
    resolved.mkdir(parents=True, exist_ok=True)
    marker = resolved / PRIVATE_MARKER
    if not marker.exists():
        marker.write_text(
            "Private WCFI working data. Do not commit, sync publicly, or send to a model "
            "without separate explicit approval.\n",
            encoding="utf-8",
        )
    ignore = resolved / ".gitignore"
    if ignore.exists():
        existing = ignore.read_text(encoding="utf-8")
        if existing != PRIVATE_GITIGNORE:
            raise UnsafeOutputError(
                f"Existing .gitignore in {resolved} is not the expected deny-all guard; "
                "it was not overwritten."
            )
    else:
        ignore.write_text(PRIVATE_GITIGNORE, encoding="utf-8")
    return resolved


def contained_path(root: Path, relative: str) -> tuple[Path, bool]:
    """Resolve an export-relative path and report whether it remains under root."""
    base = root.expanduser().resolve(strict=False)
    candidate = (base / Path(relative.replace("\\", "/"))).resolve(strict=False)
    try:
        candidate.relative_to(base)
    except ValueError:
        return candidate, False
    return candidate, True

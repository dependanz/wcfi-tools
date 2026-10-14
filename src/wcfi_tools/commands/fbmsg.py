"""Local-only `wcfi fbmsg ...` commands."""
# ruff: noqa: B008

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console

from ..fbmsg import FbmsgExportError, build_index, scan_export
from ..private_workspace import UnsafeOutputError

app = typer.Typer(
    help="Inspect a user-supplied Facebook Messenger JSON export locally.",
    no_args_is_help=True,
)
console = Console()


def _print_summary(summary: dict) -> None:
    console.print(f"[bold]Message files:[/] {summary['message_files']}")
    console.print(f"[bold]Conversations:[/] {summary['conversations']}")
    console.print(f"[bold]Messages:[/] {summary['messages']}")
    console.print(f"[bold]Attachments:[/] {summary['attachments']}")
    console.print(
        f"[bold]Missing local attachments:[/] {summary['missing_local_attachments']}"
    )
    if summary.get("attachment_kinds"):
        console.print("[bold]Attachment kinds:[/]")
        for kind, count in summary["attachment_kinds"].items():
            console.print(f"  {kind}: {count}")


@app.command("scan")
def scan(
    export_root: Path = typer.Argument(
        ..., help="Extracted Facebook Download Your Information folder in JSON format."
    ),
) -> None:
    """Count export records without printing message text or participant names."""
    try:
        summary = scan_export(export_root)
    except (FbmsgExportError, NotADirectoryError, OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    _print_summary(summary)
    console.print("[dim]Read-only scan: no messages or attachments were changed.[/]")


@app.command("index")
def index(
    export_root: Path = typer.Argument(
        ..., help="Extracted Facebook Download Your Information folder in JSON format."
    ),
    output_dir: Path = typer.Argument(
        ..., help="Private local output directory (must not be inside a Git worktree)."
    ),
    term: list[str] = typer.Option(
        [],
        "--term",
        "-t",
        help="Only create task candidates containing this term; repeat for multiple terms.",
    ),
    force: bool = typer.Option(False, help="Replace known local index files."),
) -> None:
    """Build message, attachment, and rule-based task-candidate indexes locally."""
    try:
        summary = build_index(export_root, output_dir, force=force, terms=term)
    except (
        FbmsgExportError,
        UnsafeOutputError,
        FileExistsError,
        NotADirectoryError,
        OSError,
        ValueError,
    ) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    _print_summary(summary)
    console.print(f"[bold]Task candidates needing review:[/] {summary['task_candidates']}")
    console.print(f"[green]Private index created:[/] {Path(output_dir).expanduser().resolve()}")
    console.print(
        "[dim]Raw content stayed local. No message was sent, deleted, reacted to, or "
        "submitted to an external model.[/]"
    )

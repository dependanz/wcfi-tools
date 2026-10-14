"""Read-only `wcfi wix ...` commands."""
# ruff: noqa: B008

from __future__ import annotations

import os
from pathlib import Path

import typer
from rich.console import Console

from ..private_workspace import UnsafeOutputError, initialize_private_directory
from ..wix import WixApiError, WixCredentials, WixMediaClient
from ..wix.catalog import summarize_catalog, write_catalog

app = typer.Typer(
    help="Inventory Wix Media Manager without changing the site.",
    no_args_is_help=True,
)
console = Console()


@app.command("doctor")
def doctor() -> None:
    """Check Wix environment configuration without displaying secret values."""
    token_set = bool(os.environ.get("WIX_ACCESS_TOKEN", "").strip())
    site_set = bool(os.environ.get("WIX_SITE_ID", "").strip())
    console.print(f"[bold]WIX_ACCESS_TOKEN:[/] {'set' if token_set else 'missing'}")
    console.print(f"[bold]WIX_SITE_ID:[/] {'set' if site_set else 'not set'}")
    console.print(
        "[dim]This integration only calls GET /site-media/v1/files and needs Read Media "
        "Manager permission. API keys also require WIX_SITE_ID.[/]"
    )
    if not token_set:
        raise typer.Exit(1)


@app.command("media-list")
def media_list(
    output_dir: Path = typer.Argument(
        ..., help="Private local output directory (must not be inside any Git worktree)."
    ),
    parent_folder_id: str | None = typer.Option(
        None, "--parent-folder-id", help="Limit the inventory to one Wix folder."
    ),
    page_size: int = typer.Option(
        200, min=1, max=200, help="Files requested per read-only API call."
    ),
    max_files: int = typer.Option(
        0, min=0, help="Stop after this many files; 0 means no artificial limit."
    ),
    force: bool = typer.Option(False, help="Replace an existing local catalog."),
) -> None:
    """Fetch Wix Media Manager metadata into a guarded local JSONL catalog."""
    try:
        private_dir = initialize_private_directory(output_dir)
        catalog_path = private_dir / "media-catalog.jsonl"
        credentials = WixCredentials.from_environment()
        client = WixMediaClient(credentials)
        descriptors = client.iter_files(
            parent_folder_id=parent_folder_id,
            page_size=page_size,
            max_files=max_files or None,
        )
        count = write_catalog(descriptors, catalog_path, force=force)
    except (UnsafeOutputError, WixApiError, FileExistsError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    console.print(f"[green]Cataloged {count} Wix media file(s).[/]")
    console.print(f"  {catalog_path}")
    console.print("[dim]No Wix content was edited, uploaded, deleted, restored, or published.[/]")


@app.command("inspect")
def inspect_catalog(
    catalog: Path = typer.Argument(..., help="A media-catalog.jsonl created by media-list."),
) -> None:
    """Show safe aggregate counts from a local Wix media catalog."""
    catalog = catalog.expanduser().resolve()
    if not catalog.is_file():
        console.print(f"[red]Catalog does not exist:[/] {catalog}")
        raise typer.Exit(1)
    try:
        summary = summarize_catalog(catalog)
    except (OSError, ValueError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None
    console.print(f"[bold]Files:[/] {summary['files']}")
    console.print(f"[bold]Folders represented:[/] {summary['folder_count']}")
    console.print("[bold]Media types:[/]")
    for media_type, count in summary["media_types"].items():
        console.print(f"  {media_type}: {count}")

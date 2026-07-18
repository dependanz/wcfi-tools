"""`wcfi speakers …` — manage the local enrolled-voiceprint database."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from ..speakers.voiceprints import VoiceprintDB, db_path

app = typer.Typer(help="Manage locally stored speaker voiceprints.", no_args_is_help=True)
console = Console()


@app.command("list")
def list_speakers() -> None:
    """List enrolled speakers and how many voiceprint samples each has."""
    db = VoiceprintDB.load()
    if len(db) == 0:
        console.print(f"[yellow]No speakers enrolled yet.[/] (store: {db_path()})")
        return
    table = Table(title="Enrolled speakers")
    table.add_column("Name")
    table.add_column("Samples", justify="right")
    for name in db.names():
        table.add_row(name, str(db.sample_count(name)))
    console.print(table)
    console.print(f"[dim]store: {db_path()}[/]")


@app.command("forget")
def forget(
    name: str = typer.Argument(..., help="Speaker to delete."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete a speaker's voiceprints from the local database."""
    db = VoiceprintDB.load()
    if name not in db:
        console.print(f"[red]Not found:[/] {name}")
        raise typer.Exit(1)
    if not yes and not typer.confirm(f"Delete all voiceprints for '{name}'?"):
        raise typer.Exit(0)
    db.forget(name)
    db.save()
    console.print(f"[green]Deleted[/] {name}")


@app.command("rename")
def rename(
    old: str = typer.Argument(..., help="Current name."),
    new: str = typer.Argument(..., help="New name."),
) -> None:
    """Rename a speaker (merges samples if the new name already exists)."""
    db = VoiceprintDB.load()
    if not db.rename(old, new):
        console.print(f"[red]Could not rename[/] '{old}' -> '{new}' (unknown name, or names equal).")
        raise typer.Exit(1)
    db.save()
    console.print(f"[green]Renamed[/] {old} -> {new}")


@app.command("clear")
def clear(
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Delete ALL enrolled voiceprints."""
    db = VoiceprintDB.load()
    if len(db) == 0:
        console.print("[yellow]Nothing to clear.[/]")
        return
    if not yes and not typer.confirm(f"Delete ALL {len(db)} enrolled speaker(s)?"):
        raise typer.Exit(0)
    db.clear()
    db.save()
    console.print("[green]Cleared[/] all voiceprints.")

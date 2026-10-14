"""Creation command group."""
from pathlib import Path
from typing import Optional

import typer

from ..creation import workflow
from ..creation.google import GoogleError

app = typer.Typer(help="Create assets from reviewed WCFI artifacts.", no_args_is_help=True)


@app.command("google-doc")
def google_doc(
    artifact: Optional[Path] = typer.Argument(None, help="One *.wcfi.md file; prompts when omitted."),
    folder: Optional[str] = typer.Option(None, "--folder", help="Destination Drive folder ID."),
    account: Optional[str] = typer.Option(None, "--account", help="Configured Google account email."),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate locally without connecting to Google."),
    preview: Optional[Path] = typer.Option(None, "--preview", help="Write a new local HTML preview outside Git."),
):
    """Validate, review and explicitly create one pageless Google document."""
    try:
        workflow.run(artifact, folder=folder, account=account, dry_run=dry_run, preview=preview)
    except typer.Abort:
        typer.echo("Cancelled; no further processing.")
        raise typer.Exit(1) from None
    except (ValueError, OSError, GoogleError) as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(1) from None

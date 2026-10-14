"""`wcfi inbox ...` — a local inbox that agents fill and you triage in the browser."""
# ruff: noqa: B008

from __future__ import annotations

import json
import sys
import time
import webbrowser
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from ..inbox.client import InboxClient, InboxClientError
from ..inbox.store import STATUSES
from ..private_workspace import UnsafeOutputError

app = typer.Typer(
    help="A local inbox that agents fill with things you need to know; you triage it in a browser.",
    no_args_is_help=True,
)
memory_app = typer.Typer(help="Durable notes the agent keeps about you and your sources.",
                         no_args_is_help=True)
cursor_app = typer.Typer(help="Where each source's last sweep left off.", no_args_is_help=True)
app.add_typer(memory_app, name="memory")
app.add_typer(cursor_app, name="cursor")
console = Console()

PORT = typer.Option(None, "--port", help="Server port (default 8787, or WCFI_INBOX_PORT).")


def _client(port: int | None) -> InboxClient:
    try:
        return InboxClient(port=port)
    except (UnsafeOutputError, OSError) as exc:
        console.print(f"[red]{exc}[/]")
        raise typer.Exit(1) from None


def _fail(exc: Exception) -> None:
    console.print(f"[red]{exc}[/]")
    raise typer.Exit(1) from None


def _utf8_stdout() -> None:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure:
        reconfigure(encoding="utf-8", errors="replace")


def _parse_items(raw: str) -> list[dict]:
    text = raw.strip()
    if not text:
        raise typer.BadParameter("No input. Pass a JSON file, or pipe JSON into `-`.")
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        try:
            data = [json.loads(line) for line in text.splitlines() if line.strip()]
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"Input is neither JSON nor JSON Lines: {exc}") from None
    if isinstance(data, dict):
        data = data["items"] if "items" in data else [data]
    if not isinstance(data, list):
        raise typer.BadParameter('Expected an item, a list of items, or {"items": [...]}.')
    return data


@app.command("up")
def up(
    port: int | None = PORT,
    open_browser: bool = typer.Option(False, "--open", help="Also open the inbox in a browser."),
) -> None:
    """Start the inbox server if it isn't running. Safe to run any time."""
    client = _client(port)
    try:
        started = client.ensure_running()
    except InboxClientError as exc:
        _fail(exc)
    label = "Started" if started else "Already running"
    console.print(f"[green]{label}:[/] {client.url}")
    console.print(f"[dim]Data: {client.data_dir}[/]")
    if open_browser:
        webbrowser.open(client.url)


@app.command("open")
def open_(port: int | None = PORT) -> None:
    """Open the inbox in your browser, starting the server first if needed."""
    client = _client(port)
    try:
        client.ensure_running()
    except InboxClientError as exc:
        _fail(exc)
    webbrowser.open(client.url)
    console.print(client.url)


@app.command("status")
def status(port: int | None = PORT) -> None:
    """Show whether the server is running and how much is waiting."""
    client = _client(port)
    if not client.is_running():
        console.print(f"Not running. Start it with [bold]wcfi inbox up[/].  [dim]{client.data_dir}[/]")
        raise typer.Exit(1)
    try:
        counts = client.items(limit=1)["counts"]
    except InboxClientError as exc:
        _fail(exc)
    console.print(f"[green]Running:[/] {client.url}")
    console.print(
        f"{counts['total']} items | {counts['open']} unreviewed "
        f"({counts['needs_you_open']} need you) | {counts['relevant']} relevant | "
        f"{counts['settled']} settled | {counts['notrelevant']} not relevant"
    )


@app.command("down")
def down(port: int | None = PORT) -> None:
    """Stop the inbox server. Your items and notes stay on disk."""
    client = _client(port)
    try:
        stopped = client.shutdown()
    except InboxClientError as exc:
        _fail(exc)
    console.print("Stopped." if stopped else "It wasn't running.")


@app.command("push")
def push(
    file: str = typer.Argument(
        "-", help="JSON (an item, a list, or {\"items\": [...]}) or JSON Lines. '-' reads stdin."
    ),
    source: str | None = typer.Option(
        None, "--source", "-s", help="Fill in 'source' on items that don't have one."
    ),
    start: bool = typer.Option(True, "--start/--no-start", help="Start the server if needed."),
    port: int | None = PORT,
) -> None:
    """Send items to the inbox. Re-sending an item refreshes it but keeps your decision."""
    raw = (
        sys.stdin.buffer.read().decode("utf-8-sig")
        if file == "-"
        else Path(file).read_text(encoding="utf-8-sig")
    )
    items = _parse_items(raw)
    if source:
        for item in items:
            if isinstance(item, dict):
                item.setdefault("source", source)
    client = _client(port)
    try:
        if start:
            client.ensure_running()
        result = client.push(items)
    except InboxClientError as exc:
        _fail(exc)
    console.print(
        f"[green]Added {len(result['added'])}[/], updated {len(result['updated'])}, "
        f"unchanged {len(result['unchanged'])}  ->  {client.url}"
    )


@app.command("list")
def list_(
    status: str = typer.Option("all", "--status", help="all, open, relevant, settled, notrelevant."),
    source: str | None = typer.Option(None, "--source"),
    kind: str | None = typer.Option(None, "--kind", help="needs-you, fyi or other."),
    as_json: bool = typer.Option(False, "--json", help="Print JSON for agents."),
    port: int | None = PORT,
) -> None:
    """Read items back with your decisions and notes."""
    if status not in ("all", "open", *STATUSES):
        raise typer.BadParameter(f"--status must be all, open or one of {', '.join(STATUSES)}")
    client = _client(port)
    try:
        data = client.items(status=None if status == "all" else status, source=source, kind=kind)
    except InboxClientError as exc:
        _fail(exc)
    if as_json:
        _utf8_stdout()
        sys.stdout.write(json.dumps(data["items"], ensure_ascii=False, indent=2) + "\n")
        return
    table = Table(show_lines=False)
    for column in ("Status", "Kind", "Title", "Source", "Note"):
        table.add_column(column, overflow="fold")
    for item in data["items"]:
        note = (item["note"] or "").replace("\n", " ")
        table.add_row(item["status"] or "-", item["kind"], item["title"], item["source"],
                      note[:80] + ("..." if len(note) > 80 else ""))
    console.print(table)


@memory_app.command("add")
def memory_add(
    text: str = typer.Argument(..., help="Something worth remembering next sweep."),
    scope: str = typer.Option("general", "--scope", help="A source name or topic."),
    port: int | None = PORT,
) -> None:
    """Remember a preference or fact (e.g. 'Not in the anniversary band')."""
    client = _client(port)
    try:
        client.ensure_running()
        entry = client.memory_add(text, scope)
    except InboxClientError as exc:
        _fail(exc)
    console.print(f"Saved memory #{entry['id']} [dim]({entry['scope']})[/]")


@memory_app.command("list")
def memory_list(
    scope: str | None = typer.Option(None, "--scope"),
    as_json: bool = typer.Option(False, "--json"),
    port: int | None = PORT,
) -> None:
    """Show what the agent has been told to remember."""
    client = _client(port)
    try:
        entries = client.memory_list(scope)
    except InboxClientError as exc:
        _fail(exc)
    if as_json:
        _utf8_stdout()
        sys.stdout.write(json.dumps(entries, ensure_ascii=False, indent=2) + "\n")
        return
    for entry in entries:
        console.print(f"[bold]#{entry['id']}[/] [dim]{entry['scope']}[/]  {entry['text']}")
    if not entries:
        console.print("[dim]Nothing remembered yet.[/]")


@memory_app.command("rm")
def memory_rm(mid: int = typer.Argument(...), port: int | None = PORT) -> None:
    """Forget one memory by its number."""
    client = _client(port)
    try:
        client.memory_delete(mid)
    except InboxClientError as exc:
        _fail(exc)
    console.print(f"Forgot #{mid}.")


@cursor_app.command("get")
def cursor_get(source: str = typer.Argument(...), port: int | None = PORT) -> None:
    """Print where a source's last sweep stopped (empty if never swept)."""
    client = _client(port)
    try:
        found = next((s for s in client.sources() if s["name"] == source), None)
    except InboxClientError as exc:
        _fail(exc)
    if found:
        console.print(f"{found['cursor']}  [dim]last sweep {found['last_sweep_at'] or 'never'}[/]")


@cursor_app.command("set")
def cursor_set(
    source: str = typer.Argument(...),
    value: str = typer.Argument(..., help="Anything the agent can resume from, e.g. a timestamp."),
    notes: str | None = typer.Option(None, "--notes"),
    port: int | None = PORT,
) -> None:
    """Record where a sweep stopped and mark the source as swept now."""
    client = _client(port)
    try:
        client.ensure_running()
        client.source_set(source, cursor=value, notes=notes, swept=True)
    except InboxClientError as exc:
        _fail(exc)
    console.print(f"{source}: cursor saved at {time.strftime('%H:%M')}.")

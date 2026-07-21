"""`wcfi setup` — interactive, idempotent configuration of providers and API keys."""

from __future__ import annotations

import shutil
import webbrowser

import typer
from rich.console import Console
from rich.prompt import Prompt

from .. import config as cfg

console = Console()

SUMMARIZER_CHOICES = ["openai", "anthropic"]


def _validate_openai(key: str) -> tuple[bool, str]:
    try:
        from openai import OpenAI

        OpenAI(api_key=key).models.list()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001 - surface any auth/network failure to the user
        return False, str(exc)


def _validate_anthropic(key: str) -> tuple[bool, str]:
    try:
        from anthropic import Anthropic

        Anthropic(api_key=key).models.list()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


_VALIDATORS = {"openai": _validate_openai, "anthropic": _validate_anthropic}


def _ensure_key(provider: str) -> bool:
    """Ensure a valid key exists for ``provider``; prompt + validate + store if needed."""
    noun = "token" if provider == "huggingface" else "API key"
    source = cfg.secret_source(provider)
    if source:
        console.print(f"  [green]OK[/] {provider} {noun} found (via {source})")
        return True
    env = cfg.SECRET_ENV_VARS.get(provider, provider.upper())
    console.print(f"  {provider} {noun} not found.")
    key = Prompt.ask(f"  Paste your {provider} {noun}", password=True).strip()
    if not key:
        console.print(f"  [yellow]Skipped[/] - set {env} later or re-run `wcfi setup`.")
        return False
    validator = _VALIDATORS.get(provider)
    if validator:
        ok, msg = validator(key)
        if not ok:
            console.print(f"  [red]{noun.capitalize()} failed validation:[/] {msg}")
            return False
    if cfg.set_secret(provider, key):
        verb = "validated and stored" if validator else "stored"
        console.print(f"  [green]OK[/] {provider} {noun} {verb} in the OS keyring.")
    else:
        console.print(
            f"  [yellow]No keyring backend available.[/] {noun.capitalize()} not stored - "
            f"set {env} in your environment or a .env file."
        )
    return True


def _ensure_gate(token: str) -> None:
    """Guided one-time acceptance of the pyannote model terms (open the page, verify access)."""
    from ..speaker import hf

    status, _ = hf.check_access(token)
    if status == "ok":
        console.print("  [green]OK[/] model terms already accepted.")
        return
    if status in ("no_token", "bad_token"):  # nothing to verify against yet
        return
    console.print(f"  One-time: open [underline]{hf.ACCEPT_URL}[/] and click [bold]Agree and access[/].")
    try:
        webbrowser.open(hf.ACCEPT_URL)
    except Exception:  # noqa: BLE001 - headless / no browser
        pass
    Prompt.ask("  Press Enter once you've accepted (or to skip for now)", default="")
    status, _ = hf.check_access(token)
    if status == "ok":
        console.print("  [green]OK[/] access confirmed.")
    else:
        console.print("  [yellow]Not confirmed yet[/] — accept later; wcfi will guide you again on first use.")


def _setup_diarization(config: dict) -> None:
    """Optional: enable the pyannote speaker-separation backend (HF token + one-time gate accept)."""
    from ..speaker import hf

    console.print("\n[bold]Speaker separation[/] [dim](optional)[/]")
    console.print(
        "  [dim]pyannote separates speakers far better than the built-in engine, but needs a free\n"
        "  Hugging Face account + a one-time model-access click, and pulls in PyTorch.[/]"
    )
    if not typer.confirm("  Enable pyannote speaker separation?", default=False):
        config.setdefault("diarize", {})["backend"] = "onnx"
        return

    token = cfg.get_secret("huggingface")
    who = hf.whoami(token) if token else None
    if not who:
        console.print(f"  Create a read token at [underline]{hf.TOKENS_URL}[/]")
        try:
            webbrowser.open(hf.TOKENS_URL)
        except Exception:  # noqa: BLE001
            pass
        token = Prompt.ask("  Paste your Hugging Face token", password=True).strip()
        who = hf.whoami(token) if token else None
        if not who:
            console.print("  [yellow]No valid token — leaving speaker separation on the built-in engine.[/]")
            config.setdefault("diarize", {})["backend"] = "onnx"
            return
        cfg.set_secret("huggingface", token)
    console.print(f"  [green]OK[/] Hugging Face token ({who}).")
    _ensure_gate(token)
    config.setdefault("diarize", {})["backend"] = "pyannote"
    console.print('  Install the engine when ready:  [bold]pip install -e ".[diarize]"[/]')


def _check_ffmpeg() -> bool:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        console.print(f"  [red]MISSING[/] on PATH: {', '.join(missing)} - install ffmpeg (see README).")
        return False
    console.print("  [green]OK[/] ffmpeg and ffprobe found.")
    return True


def setup(
    check: bool = typer.Option(False, "--check", help="Verify configuration without changing anything."),
) -> None:
    """Configure the API keys and providers the other tools need."""
    config = cfg.load_config()

    if check:
        console.print("[bold]wcfi configuration[/]")
        console.print(f"  config file : {cfg.config_path()}")
        console.print(f"  configured  : {'[green]yes[/]' if cfg.is_configured() else '[red]no — run wcfi setup[/]'}")
        console.print(f"  summarizer  : {config['providers']['summarizer']}")
        console.print(f"  transcriber : {config['providers'].get('transcriber', 'openai')}")
        console.print(f"  diarizer    : {config.get('diarize', {}).get('backend', 'auto')}")
        for provider in ("openai", "anthropic", "huggingface"):
            src = cfg.secret_source(provider)
            state = f"[green]set[/] (via {src})" if src else "[yellow]not set[/]"
            console.print(f"  {provider:<11}: {state}")
        _check_ffmpeg()
        raise typer.Exit(0)

    console.print("[bold]wcfi setup[/] - press Enter to accept defaults in [dim][brackets][/].\n")

    summarizer = Prompt.ask(
        "Summarization provider", choices=SUMMARIZER_CHOICES,
        default=config["providers"]["summarizer"],
    )
    config["providers"]["summarizer"] = summarizer
    config["providers"]["transcriber"] = "openai"  # only supported transcriber today
    console.print("[dim]Transcription uses OpenAI (gpt-4o-transcribe); Claude can't transcribe audio.[/]\n")

    console.print("[bold]API keys[/]")
    _ensure_key("openai")  # always needed for transcription
    if summarizer == "anthropic":
        _ensure_key("anthropic")

    _setup_diarization(config)

    console.print("\n[bold]System check[/]")
    _check_ffmpeg()

    config.setdefault("meta", {})["configured"] = True
    path = cfg.save_config(config)
    console.print(f"\n[green]Saved[/] config to {path}")
    console.print("Try it: [bold]wcfi meeting summarize <folder>[/]  (or add --dry-run first)")

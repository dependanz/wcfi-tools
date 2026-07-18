"""`wcfi setup` — interactive, idempotent configuration of providers and API keys."""

from __future__ import annotations

import shutil

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


def _validate_hf(key: str) -> tuple[bool, str]:
    try:
        from huggingface_hub import HfApi
    except ModuleNotFoundError:
        return False, "huggingface_hub not installed — run: pip install 'wcfi-tools[speakers]'"
    try:
        HfApi(token=key).whoami()
        return True, "ok"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


_VALIDATORS = {"openai": _validate_openai, "anthropic": _validate_anthropic, "hf": _validate_hf}


def _ensure_key(provider: str) -> bool:
    """Ensure a valid key exists for ``provider``; prompt + validate + store if needed."""
    source = cfg.secret_source(provider)
    if source:
        console.print(f"  [green]OK[/] {provider} key found (via {source})")
        return True
    env = cfg.SECRET_ENV_VARS.get(provider, provider.upper())
    console.print(f"  {provider} key not found.")
    key = Prompt.ask(f"  Paste your {provider} API key", password=True).strip()
    if not key:
        console.print(f"  [yellow]Skipped[/] — set {env} later or re-run `wcfi setup`.")
        return False
    ok, msg = _VALIDATORS[provider](key)
    if not ok:
        console.print(f"  [red]Key failed validation:[/] {msg}")
        return False
    if cfg.set_secret(provider, key):
        console.print(f"  [green]OK[/] {provider} key validated and stored in the OS keyring.")
    else:
        console.print(
            f"  [yellow]No keyring backend available.[/] Key validated but not stored — "
            f"set {env} in your environment or a .env file."
        )
    return True


def _check_ffmpeg() -> bool:
    missing = [t for t in ("ffmpeg", "ffprobe") if shutil.which(t) is None]
    if missing:
        console.print(f"  [red]MISSING[/] on PATH: {', '.join(missing)} - install ffmpeg (see README).")
        return False
    console.print("  [green]OK[/] ffmpeg and ffprobe found.")
    return True


def _check_diarization_gating(config: dict) -> None:
    """Report which gated pyannote model licenses still need a one-time accept click."""
    model = config["models"]["diarization"]
    token = cfg.get_secret("hf")
    try:
        from ..providers.pyannote_provider import check_model_access
    except ModuleNotFoundError:
        console.print("  [yellow]pyannote extra not installed[/] — run: pip install 'wcfi-tools[speakers]'")
        return
    try:
        ok, missing = check_model_access(token, model)
    except ModuleNotFoundError:
        console.print("  [yellow]huggingface_hub not installed[/] — run: pip install 'wcfi-tools[speakers]'")
        return
    except Exception as exc:  # noqa: BLE001 - network/permission issues shouldn't crash setup
        console.print(f"  [yellow]Could not verify model access:[/] {exc}")
        return
    if ok:
        console.print(f"  [green]OK[/] access to diarization model '{model}'.")
    else:
        console.print(
            "  [yellow]One-time action needed[/] — sign in to Hugging Face and click "
            "'Agree and access repository' on each page below, then re-run setup:"
        )
        for url in missing:
            console.print(f"    {url}")


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
        speakers_on = bool(config.get("speakers", {}).get("enabled", False))
        console.print(
            f"  speaker id  : {'[green]enabled[/] (pyannote)' if speakers_on else '[dim]disabled[/]'}"
        )
        for provider in ("openai", "anthropic", "hf"):
            src = cfg.secret_source(provider)
            state = f"[green]set[/] (via {src})" if src else "[yellow]not set[/]"
            console.print(f"  {provider:<10}: {state}")
        _check_ffmpeg()
        if speakers_on:
            console.print(f"  diarizer    : {config['models']['diarization']}")
            _check_diarization_gating(config)
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

    console.print("\n[bold]Speaker identification[/] (optional, runs locally via pyannote)")
    enable_speakers = Prompt.ask(
        "Enable local speaker identification?", choices=["y", "n"],
        default="y" if config.get("speakers", {}).get("enabled") else "n",
    )
    config.setdefault("speakers", {})["enabled"] = enable_speakers == "y"
    if enable_speakers == "y":
        config["providers"]["diarizer"] = "pyannote"
        console.print(
            "[dim]pyannote models are gated on Hugging Face: paste a token, then accept each "
            "model license once (setup will show the links).[/]"
        )
        if _ensure_key("hf"):
            _check_diarization_gating(config)

    console.print("\n[bold]System check[/]")
    _check_ffmpeg()

    config.setdefault("meta", {})["configured"] = True
    path = cfg.save_config(config)
    console.print(f"\n[green]Saved[/] config to {path}")
    console.print("Try it: [bold]wcfi meeting summarize <folder>[/]  (or add --dry-run first)")

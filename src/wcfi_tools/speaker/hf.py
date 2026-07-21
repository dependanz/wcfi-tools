"""Hugging Face gate helpers for the pyannote backend — stdlib only.

Kept dependency-free (no pyannote/torch/sherpa) so ``wcfi setup`` can check a user's token and
whether they've accepted the model terms *before* the heavy diarization extra is installed.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

_RETRIES = 6  # huggingface.co connections reset in bursts on some networks; retry transient ones

# pyannote's community pipeline: CC-BY-4.0, self-contained (all weights in this one repo), one-time
# gate acceptance. See https://huggingface.co/pyannote/speaker-diarization-community-1
MODEL = "pyannote/speaker-diarization-community-1"
ACCEPT_URL = f"https://huggingface.co/{MODEL}"
TOKENS_URL = "https://huggingface.co/settings/tokens"


class GatedModelError(RuntimeError):
    """The pyannote model can't be downloaded — no token, bad token, or terms not accepted yet."""


def _get(url: str, token: str | None, timeout: float = 20.0):
    """GET with retries on transient network errors (connection reset / timeout). An HTTP status
    (401/403/…) is a definitive answer and is raised immediately without retrying."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    last: Exception | None = None
    for attempt in range(_RETRIES):
        try:
            req = urllib.request.Request(url, headers=headers)
            return urllib.request.urlopen(req, timeout=timeout)  # noqa: S310 - fixed https host
        except urllib.error.HTTPError:
            raise
        except Exception as exc:  # noqa: BLE001 - reset / DNS / TLS blip; retry
            last = exc
            time.sleep(0.5 * (attempt + 1))
    raise last if last else RuntimeError("unreachable")


def reachable() -> bool:
    """True if huggingface.co answers at all — an unauthenticated 401 still means we reached it."""
    try:
        with _get("https://huggingface.co/api/whoami-v2", None):
            return True
    except urllib.error.HTTPError:
        return True
    except Exception:  # noqa: BLE001 - genuinely can't reach the host
        return False


def whoami(token: str) -> str | None:
    """Return the HF username for a token, or None if the token is invalid/unreachable."""
    try:
        with _get("https://huggingface.co/api/whoami-v2", token) as resp:
            return json.load(resp).get("name")
    except Exception:  # noqa: BLE001 - any failure means "can't confirm the token"
        return None


def check_access(token: str | None, model: str = MODEL) -> tuple[str, str]:
    """Diagnose access to a gated repo without downloading it.

    Returns one of ``("ok"|"no_token"|"bad_token"|"gated"|"error", detail)``. ``config.yaml`` is a
    small non-LFS file, so a successful GET means the gate is accepted for this token.
    """
    if not token:
        return "no_token", ""
    url = f"https://huggingface.co/{model}/resolve/main/config.yaml"
    try:
        with _get(url, token):
            return "ok", ""
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return ("gated", str(exc)) if whoami(token) else ("bad_token", str(exc))
        return "error", str(exc)
    except Exception as exc:  # noqa: BLE001 - offline / DNS / TLS
        return "error", str(exc)

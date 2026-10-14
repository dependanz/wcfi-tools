"""Small read-only client for the Wix Media Manager Files API."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

LIST_FILES_URL = "https://www.wixapis.com/site-media/v1/files"
Transport = Callable[[str, dict[str, str], float], dict[str, Any]]


class WixApiError(RuntimeError):
    """A sanitized Wix request failure that never includes credentials."""


@dataclass(frozen=True)
class WixCredentials:
    """Wix authentication loaded from environment variables, never arguments."""

    access_token: str
    site_id: str | None = None

    @classmethod
    def from_environment(cls) -> WixCredentials:
        token = os.environ.get("WIX_ACCESS_TOKEN", "").strip()
        if not token:
            raise WixApiError(
                "WIX_ACCESS_TOKEN is not set. Put a Wix app token or API key in the "
                "environment; do not pass it on the command line."
            )
        site_id = os.environ.get("WIX_SITE_ID", "").strip() or None
        return cls(access_token=token, site_id=site_id)

    def headers(self) -> dict[str, str]:
        headers = {
            "Authorization": self.access_token,
            "Accept": "application/json",
        }
        if self.site_id:
            headers["wix-site-id"] = self.site_id
        return headers


class WixMediaClient:
    """Enumerate Media Manager descriptors without mutating Wix."""

    def __init__(
        self,
        credentials: WixCredentials,
        *,
        timeout: float = 30.0,
        transport: Transport | None = None,
    ) -> None:
        self.credentials = credentials
        self.timeout = timeout
        self.transport = transport or self._stdlib_transport

    @staticmethod
    def _stdlib_transport(
        url: str, headers: dict[str, str], timeout: float
    ) -> dict[str, Any]:
        request = Request(url, headers=headers, method="GET")
        try:
            with urlopen(request, timeout=timeout) as response:
                payload = json.load(response)
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
            except OSError:
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise WixApiError(f"Wix returned HTTP {exc.code}{suffix}") from None
        except URLError as exc:
            raise WixApiError(f"Could not reach Wix: {exc.reason}") from None
        except json.JSONDecodeError:
            raise WixApiError("Wix returned a response that was not valid JSON") from None
        if not isinstance(payload, dict):
            raise WixApiError("Wix returned an unexpected response shape")
        return payload

    def iter_files(
        self,
        *,
        parent_folder_id: str | None = None,
        page_size: int = 200,
        max_files: int | None = None,
    ) -> Iterator[dict[str, Any]]:
        if not 1 <= page_size <= 200:
            raise ValueError("page_size must be between 1 and 200")
        offset = 0
        yielded = 0
        while True:
            params: dict[str, str | int] = {
                "paging.limit": page_size,
                "paging.offset": offset,
            }
            if parent_folder_id:
                params["parentFolderId"] = parent_folder_id
            url = f"{LIST_FILES_URL}?{urlencode(params)}"
            payload = self.transport(url, self.credentials.headers(), self.timeout)
            files = payload.get("files", [])
            if not isinstance(files, list):
                raise WixApiError("Wix response field 'files' was not a list")
            for descriptor in files:
                if not isinstance(descriptor, dict):
                    continue
                yield descriptor
                yielded += 1
                if max_files is not None and yielded >= max_files:
                    return
            if len(files) < page_size:
                return
            offset += len(files)

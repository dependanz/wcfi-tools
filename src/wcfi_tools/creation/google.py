"""Optional Google API adapter; credentials stay in the OS keyring."""
from __future__ import annotations

import json
from io import BytesIO
from functools import wraps
from pathlib import Path

from .. import config

SCOPES = ["https://www.googleapis.com/auth/drive.file", "https://www.googleapis.com/auth/drive.metadata.readonly"]
TOKEN_URI = "https://oauth2.googleapis.com/token"


class GoogleError(RuntimeError):
    pass


def credentials_for(account: str):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
    except ImportError as exc:
        raise GoogleError('Install Google support with pip install "wcfi-tools[google]"') from exc
    raw = config.get_secret("google:" + account)
    if not raw:
        raise GoogleError("Google account is not configured; run wcfi setup google")
    info = json.loads(raw)
    if info.get("token_uri") != TOKEN_URI:
        raise GoogleError("Stored Google credentials have an unexpected token endpoint; reauthorize")
    if not set(SCOPES).issubset(info.get("scopes", [])):
        raise GoogleError("Stored authorization lacks required scopes; run wcfi setup google")
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if not creds.has_scopes(SCOPES):
        raise GoogleError("Google authorization lacks the required scopes; run wcfi setup google")
    if not creds.valid:
        try:
            creds.refresh(Request())
        except Exception as exc:
            raise GoogleError("Google authorization refresh failed; run wcfi setup google") from exc
        if not config.set_secret("google:" + account, creds.to_json()):
            raise GoogleError("Could not save refreshed Google credentials to the OS keyring")
    return creds


def authorize(client_path: Path):
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError as exc:
        raise GoogleError('Install Google support with pip install "wcfi-tools[google]"') from exc
    data = json.loads(client_path.read_text(encoding="utf-8-sig"))
    installed = data.get("installed", {})
    if (installed.get("token_uri") != TOKEN_URI or
            installed.get("auth_uri") != "https://accounts.google.com/o/oauth2/auth"):
        raise GoogleError("Expected a Google Desktop app OAuth client JSON with official Google endpoints")
    flow = InstalledAppFlow.from_client_config(data, SCOPES)
    try:
        creds = flow.run_local_server(host="localhost", port=0, timeout_seconds=180,
                                     prompt="consent", access_type="offline")
        service = GoogleService(creds)
        account = service.account()["emailAddress"]
    except Exception as exc:
        raise GoogleError("Google authorization failed; no credential details were logged") from exc
    if not creds.refresh_token:
        raise GoogleError("Google did not return a refresh token; repeat consent")
    if not config.set_secret("google:" + account, creds.to_json()):
        raise GoogleError("OS keyring storage failed; Google credentials were not saved")
    settings = config.load_config()
    settings.setdefault("google", {})["account"] = account
    config.save_config(settings)
    return account


def _read_error(method):
    @wraps(method)
    def wrapped(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except GoogleError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "resp", None), "status", None)
            suffix = f" (HTTP {status})" if isinstance(status, int) else ""
            raise GoogleError(f"Google {method.__name__} failed{suffix}; check authorization, access and connection") from exc
    return wrapped


class GoogleService:
    def __init__(self, credentials):
        try:
            from googleapiclient.discovery import build
            import google_auth_httplib2
            import httplib2
        except ImportError as exc:
            raise GoogleError('Install Google support with pip install "wcfi-tools[google]"') from exc
        transport = google_auth_httplib2.AuthorizedHttp(credentials, http=httplib2.Http(timeout=60))
        self.drive = build("drive", "v3", http=transport, cache_discovery=False)
        self.docs = build("docs", "v1", http=transport, cache_discovery=False)

    @_read_error
    def account(self):
        return self.drive.about().get(fields="user(emailAddress,permissionId)").execute(num_retries=0)["user"]

    @_read_error
    def context(self, folder_id: str):
        user = self.account()
        folder = self.drive.files().get(fileId=folder_id, supportsAllDrives=True,
                                        fields="id,name,mimeType,trashed,driveId,capabilities(canAddChildren)").execute(num_retries=0)
        if (folder.get("trashed") or folder.get("mimeType") != "application/vnd.google-apps.folder"
                or not folder.get("capabilities", {}).get("canAddChildren")):
            raise GoogleError("Destination is not an accessible writable Drive folder")
        permissions, token = [], None
        while True:
            result = self.drive.permissions().list(fileId=folder["id"], supportsAllDrives=True,
                     pageToken=token, fields="nextPageToken,permissions(type,role,emailAddress,domain,allowFileDiscovery,permissionDetails)").execute(num_retries=0)
            permissions.extend(result.get("permissions", []))
            token = result.get("nextPageToken")
            if not token:
                break
        if not permissions:
            raise GoogleError("Destination access could not be established; choose a folder with reviewable permissions")
        return {"account": user, "folder": folder,
                "inherited_access": sorted(permissions, key=lambda x: json.dumps(x, sort_keys=True))}

    @_read_error
    def find_operation(self, operation: str):
        if len(operation) != 64 or any(c not in "0123456789abcdef" for c in operation):
            raise GoogleError("Invalid operation identity")
        result = self.drive.files().list(q=f"trashed = false and appProperties has {{ key='wcfi_operation' and value='{operation}' }}",
                                        spaces="drive", pageSize=100, includeItemsFromAllDrives=True,
                                        supportsAllDrives=True, fields="files(id,name,parents,appProperties),nextPageToken").execute(num_retries=0)
        if result.get("nextPageToken"):
            raise GoogleError("Ambiguous operation matches; manual reconciliation required")
        return result.get("files", [])

    def create(self, title: str, folder: str, html: str, operation: str):
        from googleapiclient.http import MediaIoBaseUpload
        return self.drive.files().create(
            body={"name": title, "mimeType": "application/vnd.google-apps.document",
                  "parents": [folder], "appProperties": {"wcfi_operation": operation}},
            media_body=MediaIoBaseUpload(BytesIO(html.encode("utf-8")), mimetype="text/html", resumable=False),
            supportsAllDrives=True, fields="id,name,parents,appProperties").execute(num_retries=0)

    @_read_error
    def read(self, identifier: str):
        return self.docs.documents().get(documentId=identifier, includeTabsContent=True).execute(num_retries=0)

    def style(self, identifier: str, requests: list, revision: str):
        return self.docs.documents().batchUpdate(documentId=identifier, body={"requests": requests,
            "writeControl": {"requiredRevisionId": revision}}).execute(num_retries=0)

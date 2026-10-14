"""The inbox receiver: a small local web server agents push items to and humans triage in.

Binds to 127.0.0.1 only. Every ``/api`` call needs the per-install token (the page served at ``/``
has it embedded; the CLI reads it from the data dir). The ``Host`` header must name localhost,
which blocks DNS rebinding, and writes must be JSON, which forces a CORS preflight this server
never grants.

Run detached by ``wcfi inbox up``; ``python -m wcfi_tools.inbox.server --port N --data-dir D``.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import os
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .. import __version__
from .paths import APP_ID, DB_NAME, STATE_NAME, load_or_create_token, prepare_data_dir
from .store import InboxError, InboxStore

_WRITE_METHODS = {"POST", "PUT", "PATCH"}


def _page(token: str) -> str:
    try:
        html = (
            resources.files("wcfi_tools.inbox")
            .joinpath("static/index.html")
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError):
        html = "<!doctype html><title>wcfi inbox</title><p>The inbox page file is missing.</p>"
    return html.replace("__INBOX_TOKEN__", token)


def _stopping(app: FastAPI) -> bool:
    server = app.state.server
    return bool(server is not None and server.should_exit)


async def _json(request: Request) -> Any:
    try:
        return json.loads((await request.body()).decode("utf-8") or "null")
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(400, "The request body is not valid JSON.") from None


def create_app(store: InboxStore, token: str, *, allowed_hosts: set[str] | None = None) -> FastAPI:
    """Build the app. ``allowed_hosts=None`` skips the Host check (tests only)."""
    app = FastAPI(title="wcfi inbox", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.server = None

    @app.middleware("http")
    async def guard(request: Request, call_next):
        if allowed_hosts is not None and request.headers.get("host", "") not in allowed_hosts:
            return JSONResponse({"detail": "Host not allowed."}, status_code=403)
        path = request.url.path
        if path.startswith("/api/"):
            supplied = request.headers.get("x-inbox-token", "")
            if not supplied and path == "/api/events":  # EventSource can't send headers
                supplied = request.query_params.get("token", "")
            if not hmac.compare_digest(supplied.encode(), token.encode()):
                return JSONResponse({"detail": "Missing or wrong inbox token."}, status_code=401)
            ctype = request.headers.get("content-type", "")
            if request.method in _WRITE_METHODS and not ctype.startswith("application/json"):
                return JSONResponse(
                    {"detail": "Send JSON with Content-Type: application/json."}, status_code=415
                )
        return await call_next(request)

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"app": APP_ID, "ok": True, "pid": os.getpid(), "version": __version__}

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_page(token), headers={"Cache-Control": "no-store"})

    @app.get("/api/items")
    def items(
        status: str | None = None,
        source: str | None = None,
        kind: str | None = None,
        since: int | None = None,
        limit: int = 1000,
    ) -> dict[str, Any]:
        try:
            found = store.list_items(
                status=status, source=source, kind=kind, since=since, limit=limit
            )
        except InboxError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"items": found, "seq": store.seq, "counts": store.counts()}

    @app.post("/api/items")
    async def push(request: Request) -> dict[str, Any]:
        body = await _json(request)
        raw = body["items"] if isinstance(body, dict) and "items" in body else body
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list):
            raise HTTPException(422, 'Send an item, a list of items, or {"items": [...]}.')
        if len(raw) > 500:
            raise HTTPException(413, "Push at most 500 items per request.")
        try:
            return store.upsert(raw)
        except InboxError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.patch("/api/items/{iid}")
    async def decide(iid: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        if not isinstance(body, dict):
            raise HTTPException(422, 'Send {"status": ..., "note": ...}.')
        fields = {key: body[key] for key in ("status", "note") if key in body}
        try:
            return store.decide(iid, **fields)
        except KeyError:
            raise HTTPException(404, "No item with that id.") from None
        except InboxError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        async def stream():
            last = -1
            idle = 0.0
            yield "retry: 2000\n\n"
            # End the stream on shutdown too; an open tab must never keep the server alive.
            while not (await request.is_disconnected() or _stopping(app)):
                current = store.seq
                if current != last:
                    last = current
                    idle = 0.0
                    yield f"event: change\ndata: {current}\n\n"
                await asyncio.sleep(0.75)
                idle += 0.75
                if idle >= 15:
                    idle = 0.0
                    yield ": keep-alive\n\n"

        return StreamingResponse(
            stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store"}
        )

    @app.get("/api/memory")
    def memory(scope: str | None = None) -> dict[str, Any]:
        return {"memory": store.memory_list(scope)}

    @app.post("/api/memory")
    async def memory_add(request: Request) -> dict[str, Any]:
        body = await _json(request)
        if not isinstance(body, dict) or not isinstance(body.get("text"), str):
            raise HTTPException(422, 'Send {"text": "...", "scope": "..."}.')
        try:
            return store.memory_add(body["text"], str(body.get("scope") or "general"))
        except InboxError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.delete("/api/memory/{mid}")
    def memory_delete(mid: int) -> dict[str, Any]:
        if not store.memory_delete(mid):
            raise HTTPException(404, "No memory with that id.")
        return {"ok": True}

    @app.get("/api/sources")
    def sources() -> dict[str, Any]:
        return {"sources": store.sources_list()}

    @app.put("/api/sources/{name}")
    async def source_set(name: str, request: Request) -> dict[str, Any]:
        body = await _json(request)
        if not isinstance(body, dict):
            raise HTTPException(422, 'Send {"cursor": "...", "notes": "...", "swept": true}.')
        cursor, notes = body.get("cursor"), body.get("notes")
        if any(v is not None and not isinstance(v, str) for v in (cursor, notes)):
            raise HTTPException(422, "'cursor' and 'notes' must be strings.")
        try:
            return store.source_set(name, cursor=cursor, notes=notes, swept=bool(body.get("swept")))
        except InboxError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.post("/api/shutdown")
    def shutdown() -> dict[str, Any]:
        server = app.state.server
        if server is not None:
            server.should_exit = True
        return {"ok": True, "stopping": server is not None}

    return app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m wcfi_tools.inbox.server")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--data-dir", type=Path, required=True)
    args = parser.parse_args(argv)

    import uvicorn

    data_dir = prepare_data_dir(args.data_dir)
    store = InboxStore(data_dir / DB_NAME)
    token = load_or_create_token(data_dir)
    hosts = {f"127.0.0.1:{args.port}", f"localhost:{args.port}"}
    app = create_app(store, token, allowed_hosts=hosts)
    server = uvicorn.Server(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=args.port,
            log_level="warning",
            timeout_graceful_shutdown=3,
        )
    )
    app.state.server = server
    state_file = data_dir / STATE_NAME
    state_file.write_text(
        json.dumps(
            {
                "pid": os.getpid(),
                "port": args.port,
                "started_at": datetime.now(UTC).isoformat(timespec="seconds"),
            }
        ),
        encoding="utf-8",
    )
    try:
        server.run()
    finally:
        store.close()
        try:
            if json.loads(state_file.read_text(encoding="utf-8")).get("pid") == os.getpid():
                state_file.unlink()
        except (OSError, ValueError):
            pass


if __name__ == "__main__":
    main()

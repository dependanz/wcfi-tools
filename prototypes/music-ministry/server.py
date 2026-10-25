"""Loopback preview with one explicit private ministry-data endpoint."""
import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent / "dist"
PRIVATE_DATA = Path.home() / ".wcfi/music-ministry-preview/private/ministry.json"
FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/styles.css": ("styles.css", "text/css; charset=utf-8"),
    "/app.mjs": ("app.mjs", "text/javascript; charset=utf-8"),
    "/songs.mjs": ("songs.mjs", "text/javascript; charset=utf-8"),
    "/chords.mjs": ("chords.mjs", "text/javascript; charset=utf-8"),
    "/hub.css": ("hub.css", "text/css; charset=utf-8"),
    "/hub.mjs": ("hub.mjs", "text/javascript; charset=utf-8"),
    "/ministry.mjs": ("ministry.mjs", "text/javascript; charset=utf-8"),
    "/ministry.css": ("ministry.css", "text/css; charset=utf-8"),
    "/lineups.mjs": ("lineups.mjs", "text/javascript; charset=utf-8"),
    "/scene.mjs": ("scene.mjs", "text/javascript; charset=utf-8"),
    "/vendor/three.module.min.js": ("vendor/three.module.min.js", "text/javascript; charset=utf-8"),
    "/vendor/three.core.min.js": ("vendor/three.core.min.js", "text/javascript; charset=utf-8"),
    "/vendor/LICENSE": ("vendor/LICENSE", "text/plain; charset=utf-8"),
    "/wcfi-logo.png": ("wcfi-logo.png", "image/png"),
    "/icon.svg": ("icon.svg", "image/svg+xml"),
}


class Handler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET(head=True)

    def do_GET(self, head=False):
        path = unquote(urlsplit(self.path).path)
        if path == "/healthz":
            body = json.dumps({"app": "wcfi-music-ministry", "status": "ok", "version": 3}).encode()
            content_type = "application/json"
        elif path == "/ministry-data.json":
            # The owner gateway proxies to loopback. Never expose a file selector,
            # cross-origin response, directory listing, or raw handoff endpoint.
            host = urlsplit("http://" + self.headers.get("Host", "")).hostname
            if host not in {"127.0.0.1", "localhost", "::1"} or self.headers.get("Sec-Fetch-Site") == "cross-site":
                self.send_error(403)
                return
            try:
                if not PRIVATE_DATA.exists():
                    payload = {"available": False}
                else:
                    if PRIVATE_DATA.stat().st_size > 1_000_000:
                        raise ValueError("Oversized snapshot")
                    payload = json.loads(PRIVATE_DATA.read_text(encoding="utf-8-sig"))
                    if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
                        raise ValueError("Invalid snapshot")
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            except (OSError, ValueError):
                self.send_error(503, "Ministry snapshot unavailable")
                return
            content_type = "application/json; charset=utf-8"
        elif path in FILES:
            filename, content_type = FILES[path]
            body = (ROOT / filename).read_bytes()
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        self.end_headers()
        if not head:
            self.wfile.write(body)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8816)
    parser.add_argument("--data-file", type=Path, default=PRIVATE_DATA)
    args = parser.parse_args()
    PRIVATE_DATA = args.data_file
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"WCFI music preview: http://127.0.0.1:{args.port}/", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()

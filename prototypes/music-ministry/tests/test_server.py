"""Synthetic checks only; never load the real private snapshot."""
import importlib.util
import json
import threading
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


def test_private_endpoint_is_bounded_and_uncacheable(tmp_path):
    spec = importlib.util.spec_from_file_location("preview_server", Path(__file__).parents[1] / "server.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.PRIVATE_DATA = tmp_path / "fixture.json"
    module.PRIVATE_DATA.write_text(json.dumps({"schemaVersion": 1, "marker": "synthetic"}))
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}"

    def request(path, headers=None, method="GET"):
        try:
            return urlopen(Request(base + path, headers=headers or {}, method=method), timeout=3)
        except HTTPError as error:
            return error

    try:
        with request("/ministry-data.json") as response:
            assert response.status == 200
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers.get("Access-Control-Allow-Origin") is None
            assert json.load(response)["marker"] == "synthetic"
        with request("/ministry-data.json", method="HEAD") as response:
            assert response.status == 200 and response.read() == b""
        for headers in ({"Host": "untrusted.example"}, {"Sec-Fetch-Site": "cross-site"}):
            with request("/ministry-data.json", headers) as response:
                assert response.status == 403
        for path in ("/private/fixture.json", "/source-handoff.txt", "/server.py", "/%2e%2e/private/fixture.json"):
            with request(path) as response:
                assert response.status == 404
        module.PRIVATE_DATA.write_text("invalid")
        with request("/ministry-data.json") as response:
            assert response.status == 503
            assert str(tmp_path).encode() not in response.read()
        module.PRIVATE_DATA.unlink()
        with request("/ministry-data.json") as response:
            assert json.load(response) == {"available": False}
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=3)

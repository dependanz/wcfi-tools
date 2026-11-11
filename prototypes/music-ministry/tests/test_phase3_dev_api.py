"""Real loopback HTTP contract tests with synthetic identities and data only."""
from __future__ import annotations

import http.client
import json
import sqlite3
import sys
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from phase3_dev_api import MAX_JSON_BYTES, SyntheticSessionAuthority, make_loopback_server
from shared_store import MusicStore


def synthetic_song():
    return {
        "title": "Original synthetic melody",
        "key": "G",
        "category": "Test",
        "sections": [{"label": "Verse 1", "lines": ["[G]Only synthetic test text", "[D]Second line"]}],
    }


def synthetic_lineup(song_id):
    return {
        "title": "Synthetic Sunday",
        "date": "2026-11-15",
        "notes": "PRIVATE rehearsal notes should not be exposed in member API",
        "songs": [{"id": song_id, "key": "G", "arrangement": ["Verse 1"]}],
    }


class Phase3HTTPTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = MusicStore(Path(temp.name) / "synthetic.sqlite")
        self.store.initialize()
        with closing(sqlite3.connect(self.store.file)) as conn, conn:
            conn.execute("INSERT INTO members VALUES(?,?,?,?)", ("fixture-admin", 1, '["admin"]', "synthetic"))
        for name, roles, approved in (
            ("fixture-viewer", {"member"}, True),
            ("fixture-editor", {"song_editor"}, True),
            ("fixture-leader", {"worship_leader"}, True),
            ("fixture-pending", {"member"}, False),
        ):
            self.store.upsert_member("fixture-admin", name, roles, approved)
        self.sessions = SyntheticSessionAuthority()
        self.identities = {name: self.sessions.issue(name) for name in (
            "fixture-admin", "fixture-viewer", "fixture-editor", "fixture-leader", "fixture-pending"
        )}
        self.server = make_loopback_server(self.store, self.sessions)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.port = self.server.server_port

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, method, path, role=None, data=None, *, header_overrides=None, csrf=True, raw=None):
        headers = {"Host": f"127.0.0.1:{self.port}"}
        if role is not None:
            token, csrf_token = self.identities[role]
            headers["Cookie"] = f"wcfi_dev_session={token}"
            if method in {"POST", "PATCH"} and csrf:
                headers["X-WCFI-CSRF"] = csrf_token
        body = None
        if method in {"POST", "PATCH"}:
            body = json.dumps(data if data is not None else {}) if raw is None else raw
            headers["Content-Type"] = "application/json"
        if header_overrides:
            headers.update(header_overrides)
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers)
            response = conn.getresponse()
            data_bytes = response.read()
            return response.status, json.loads(data_bytes), dict(response.getheaders())
        finally:
            conn.close()

    def make_approved_song(self):
        status, created, _ = self.call("POST", "/api/v1/songs", "fixture-editor", synthetic_song())
        self.assertEqual(status, 201)
        self.assertEqual(self.call("POST", f"/api/v1/songs/{created['id']}/approve", "fixture-admin")[0], 200)
        return created["id"]

    def test_anonymous_and_unapproved_are_denied_even_with_fake_subject_headers(self):
        for path in ("/api/v1/songs", "/api/v1/lineups"):
            self.assertEqual(self.call("GET", path)[0], 401)
            self.assertEqual(self.call("GET", path, "fixture-pending")[0], 401)
            self.assertEqual(self.call("GET", path, header_overrides={"X-Verified-Subject": "fixture-admin", "X-Role": "admin"})[0], 401)
        status, health, headers = self.call("GET", "/healthz")
        self.assertEqual((status, health["status"]), (200, "ok"))
        self.assertEqual(headers["Cache-Control"], "no-store")
        self.assertNotIn("Access-Control-Allow-Origin", headers)

    def test_member_can_only_see_approved_songs_and_published_lineups(self):
        status, result, _ = self.call("POST", "/api/v1/songs", "fixture-editor", synthetic_song())
        self.assertEqual(status, 201)
        song_id = result["id"]
        self.assertTrue(song_id.startswith("song-"))
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer")[1]["songs"], [])
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-editor")[1]["songs"][0]["state"], "draft")
        self.assertEqual(self.call("POST", f"/api/v1/songs/{song_id}/approve", "fixture-editor")[0], 403)
        self.assertEqual(self.call("POST", f"/api/v1/songs/{song_id}/approve", "fixture-admin")[0], 200)
        self.assertEqual(len(self.call("GET", "/api/v1/songs", "fixture-viewer")[1]["songs"]), 1)
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-viewer", synthetic_song())[0], 403)

        status, result, _ = self.call("POST", "/api/v1/lineups", "fixture-leader", synthetic_lineup(song_id))
        self.assertEqual(status, 201)
        lineup_id = result["id"]
        self.assertEqual(self.call("GET", "/api/v1/lineups", "fixture-viewer")[1]["lineups"], [])
        self.assertEqual(self.call("POST", f"/api/v1/lineups/{lineup_id}/publish", "fixture-leader")[0], 403)
        self.assertEqual(self.call("POST", f"/api/v1/lineups/{lineup_id}/publish", "fixture-admin")[0], 200)
        visible = self.call("GET", "/api/v1/lineups", "fixture-viewer")[1]["lineups"][0]
        self.assertEqual(visible["id"], lineup_id)
        self.assertEqual(visible["state"], "published")
        self.assertNotIn("notes", visible)
        self.assertNotIn("PRIVATE", json.dumps(visible))

    def test_two_independent_editor_clients_share_updates_and_conflict(self):
        session_b = self.sessions.issue("fixture-editor")
        status, created, _ = self.call("POST", "/api/v1/songs", "fixture-editor", synthetic_song())
        self.assertEqual(status, 201)
        song_id = created["id"]
        endpoint = f"/api/v1/songs/{song_id}"
        updated = synthetic_song() | {"title": "Client B edit"}
        cookie_b, csrf_b = session_b
        status, response, _ = self.call("PATCH", endpoint, data={"song": updated, "expectedRevision": 1},
                                        header_overrides={"Cookie": f"wcfi_dev_session={cookie_b}", "X-WCFI-CSRF": csrf_b})
        self.assertEqual((status, response["revision"]), (200, 2))
        self.assertEqual(self.call("PATCH", endpoint, "fixture-editor", {"song": synthetic_song(), "expectedRevision": 1})[0], 409)
        latest = self.call("GET", "/api/v1/songs", "fixture-editor")[1]["songs"][0]
        self.assertEqual((latest["title"], latest["revision"]), ("Client B edit", 2))
        self.sessions.revoke(cookie_b)
        self.assertEqual(self.call("GET", "/api/v1/songs", header_overrides={"Cookie": f"wcfi_dev_session={cookie_b}"})[0], 401)

    def test_cookie_mutations_require_valid_csrf_and_same_origin(self):
        valid = synthetic_song()
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-editor", valid, csrf=False)[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-editor", valid, header_overrides={"X-WCFI-CSRF": "wrong"})[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-editor", valid, header_overrides={"Origin": "https://evil.invalid"})[0], 403)
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer", header_overrides={"Host": "evil.invalid"})[0], 403)
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer", header_overrides={"Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.call("OPTIONS", "/api/v1/songs")[0], 405)
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer", header_overrides={"Origin": f"http://localhost:{self.port}"})[0], 200)

    def test_malformed_json_and_client_assigned_identifiers_rejected(self):
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-editor", synthetic_song() | {"id": "client-chosen"})[0], 422)
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-editor", raw='{"title":"a","title":"b"}')[0], 422)
        self.assertEqual(self.call("POST", "/api/v1/songs", "fixture-editor", raw='{"bpm":NaN}')[0], 422)
        # Test oversized Content-Length without sending a huge body. The
        # server rejects it immediately, closing the connection before upload.
        cookie, csrf_token = self.identities["fixture-editor"]
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.putrequest("POST", "/api/v1/songs")
            conn.putheader("Cookie", f"wcfi_dev_session={cookie}")
            conn.putheader("X-WCFI-CSRF", csrf_token)
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(MAX_JSON_BYTES + 100))
            conn.endheaders()
            response = conn.getresponse()
            self.assertEqual(response.status, 422)
            response.read()
        finally:
            conn.close()
        self.assertEqual(self.call("GET", "/api/v1/songs?public=true", "fixture-viewer")[0], 400)

    def test_catalogue_import_preserves_sections_and_is_not_public(self):
        envelope = {"format": "wcfi-song-catalogue", "version": 1,
                    "songs": [{**synthetic_song(), "id": "synthetic-import"}]}
        self.assertEqual(self.call("POST", "/api/v1/catalogue/import", "fixture-viewer", envelope)[0], 403)
        self.assertEqual(self.call("POST", "/api/v1/catalogue/import", "fixture-editor", envelope)[1]["imported"], 1)
        song = self.call("GET", "/api/v1/songs", "fixture-editor")[1]["songs"][0]
        self.assertEqual(song["sections"], envelope["songs"][0]["sections"])
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer")[1]["songs"], [])
        self.assertEqual(self.call("POST", "/api/v1/catalogue/import", "fixture-editor", envelope)[0], 409)

    def test_database_membership_revocation_blocks_preexisting_session(self):
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer")[0], 200)
        self.store.upsert_member("fixture-admin", "fixture-viewer", {"member"}, False)
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer")[0], 401)
        token, _ = self.sessions.issue("fixture-editor", ttl_seconds=0)
        self.assertEqual(self.call("GET", "/api/v1/songs", header_overrides={"Cookie": f"wcfi_dev_session={token}"})[0], 401)

    def test_no_synthetic_session_issuance_http_route_or_real_site_access(self):
        self.assertEqual(self.call("POST", "/api/v1/login", "fixture-admin")[0], 404)
        self.assertEqual(self.call("POST", "/api/v1/test-login", "fixture-admin")[0], 404)
        self.assertEqual(self.call("GET", "/api/v1/members", "fixture-admin")[0], 404)
        self.assertEqual(self.call("GET", "/api/v1/songs", "fixture-viewer")[0], 200)
        self.assertEqual(self.server.server_address[0], "127.0.0.1")


if __name__ == "__main__":
    unittest.main()
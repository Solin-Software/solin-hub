"""Unit tests for the ask service (ask/ask.py): meeting codes, routing, TURN and the frps plugin."""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.client
import importlib.util
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent


def load_ask(codes_file: str):
    env = {"HUB_DOMAIN": "Hub.Example.Test.", "FRP_ADMIN_PASSWORD": "pw", "FRP_TOKEN": "frp-token",
           "CODES_FILE": codes_file, "TUNNEL_PORT_MIN": "20000", "TUNNEL_PORT_MAX": "20009",
           "TURN_SECRET": "turn-secret"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("ask_under_test", ROOT / "ask" / "ask.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


class AskTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.codes_path = Path(self.dir.name) / "codes.json"
        self.ask = load_ask(str(self.codes_path))
        self.open_tunnels: dict[str, int] = {}   # what frps reports: code -> port
        patcher = mock.patch.object(self.ask.tunnels, "ports", side_effect=lambda: dict(self.open_tunnels))
        patcher.start()
        self.addCleanup(patcher.stop)

    def new_code(self, secret: str, starts_at: str = "", now: float = 100.0) -> str:
        return self.ask.codes.create(digest(secret), starts_at, now)


class CodeRegistryTests(AskTestCase):
    def test_created_codes_are_unique_and_owned_by_the_secret(self) -> None:
        codes = {self.new_code(f"s{i}") for i in range(200)}
        self.assertEqual(len(codes), 200)
        self.assertTrue(all(self.ask.MEETING_CODE.match(c) for c in codes))
        code = self.new_code("mine", "2026-11-20T19:30:00-03:00")
        self.assertTrue(self.ask.codes.owns(code, "mine"))
        self.assertFalse(self.ask.codes.owns(code, "theirs"))
        self.assertFalse(self.ask.codes.owns(code, ""))
        self.assertEqual(self.ask.codes.lookup(code)["starts_at"], "2026-11-20T19:30:00-03:00")

    def test_sync_updates_only_owned_codes(self) -> None:
        mine, theirs = self.new_code("mine"), self.new_code("theirs")
        rejected = self.ask.codes.sync([(mine, "mine", "2026-11-22T09:00:00-03:00", False),
                                        (theirs, "mine", "", False), ("zzzzzz", "mine", "", True)], 200.0)
        self.assertEqual(rejected, [theirs, "zzzzzz"])
        self.assertEqual(self.ask.codes.lookup(mine)["starts_at"], "2026-11-22T09:00:00-03:00")
        self.assertFalse(self.ask.codes.lookup(mine)["active"])
        self.assertTrue(self.ask.codes.lookup(theirs)["active"])
        # The owner can bring a code back.
        self.ask.codes.sync([(mine, "mine", "", True)], 300.0)
        self.assertTrue(self.ask.codes.lookup(mine)["active"])
        # Saved, and read back by a fresh registry.
        self.assertTrue(self.ask.Codes(str(self.codes_path)).owns(mine, "mine"))

    def test_codes_nobody_syncs_are_forgotten_after_a_long_time(self) -> None:
        old = self.new_code("old", now=100.0)
        kept = self.new_code("kept", now=100.0)
        later = 100.0 + self.ask.FORGET_AFTER_S - 10
        self.ask.codes.sync([(kept, "kept", "", True)], later)
        self.ask.codes.sync([], 100.0 + self.ask.FORGET_AFTER_S + 1)
        self.assertIsNone(self.ask.codes.lookup(old))
        self.assertIsNotNone(self.ask.codes.lookup(kept))

    def test_parse_sync(self) -> None:
        parse = self.ask.parse_sync
        self.assertEqual(parse({"codes": [{"code": "k7m4p2", "secret": "s", "startsAt": "x", "active": False}]}),
                         [("k7m4p2", "s", "x", False)])
        self.assertEqual(parse({"codes": []}), [])
        for bad in ({}, {"codes": "k7m4p2"}, {"codes": [{"code": "abc123", "secret": "s"}]},
                    {"codes": [{"code": "k7m4p2"}]},
                    {"codes": [{"code": "k7m4p2", "secret": "s", "startsAt": "x" * 41}]}):
            self.assertIsNone(parse(bad), bad)


class PluginTests(AskTestCase):
    def new_proxy(self, code: str, secret: str, name: str | None = None, kind: str = "tcp") -> dict:
        content = {"user": {"metas": {"code": code, "secret": secret}}, "proxy_name": code if name is None else name,
                   "proxy_type": kind, "remote_port": 0}
        return self.ask.plugin_decision("NewProxy", {"content": content})

    def test_the_owner_gets_a_free_port(self) -> None:
        code = self.new_code("mine")
        self.open_tunnels = {"x9x9x9": 20003}
        decision = self.new_proxy(code, "mine")
        self.assertFalse(decision["reject"])
        port = decision["content"]["remote_port"]
        self.assertTrue(20000 <= port <= 20009 and port != 20003)
        # A port just handed out is not handed out again before frps reports it.
        other = self.new_proxy(self.new_code("b"), "b")["content"]["remote_port"]
        self.assertNotEqual(other, port)

    def test_others_cannot_open_a_code(self) -> None:
        code = self.new_code("mine")
        self.assertTrue(self.new_proxy(code, "wrong")["reject"])
        self.assertTrue(self.new_proxy("zzzzzz", "mine")["reject"])
        self.assertTrue(self.new_proxy(code, "mine", name="zzzzzz")["reject"])
        self.assertTrue(self.new_proxy(code, "mine", kind="http")["reject"])

    def test_no_free_port(self) -> None:
        self.open_tunnels = {f"c{i}": 20000 + i for i in range(10)}
        self.assertTrue(self.new_proxy(self.new_code("mine"), "mine")["reject"])

    def test_other_operations_pass_through(self) -> None:
        self.assertEqual(self.ask.plugin_decision("Login", {}), {"reject": False, "unchange": True})


class HttpTests(AskTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.ask.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def get(self, path: str, headers: dict | None = None, data: bytes | None = None):
        request = urllib.request.Request(self.base + path, data=data, headers=headers or {})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, dict(response.headers), response.read()
        except urllib.error.HTTPError as error:
            with error:
                return error.code, dict(error.headers), error.read()

    def post(self, path: str, body, headers: dict | None = None) -> tuple[int, dict]:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        status, _, raw = self.get(path, {"Content-Type": "application/json", **(headers or {})}, data)
        return status, json.loads(raw)

    def route(self, uri: str):
        return self.get("/internal/route", {"X-Forwarded-Uri": uri})

    def test_create_and_sync_codes(self) -> None:
        status, body = self.post("/api/codes", {"owner": digest("mine"), "startsAt": "2026-11-20T19:30:00-03:00"})
        self.assertEqual(status, 201)
        code = body["code"]
        self.assertTrue(self.ask.codes.owns(code, "mine"))
        self.assertEqual(self.post("/api/codes", {"owner": "not-a-hash"})[0], 400)
        self.assertEqual(self.post("/api/codes", b"{not json")[0], 400)
        status, body = self.post("/api/codes/sync", {"codes": [
            {"code": code, "secret": "mine", "startsAt": "", "active": False},
            {"code": code, "secret": "wrong", "startsAt": "", "active": True}]})
        self.assertEqual((status, body), (200, {"rejected": [code]}))
        self.assertFalse(self.ask.codes.lookup(code)["active"])
        self.assertEqual(self.post("/api/codes/sync", {"codes": [{"code": "aeiou1", "secret": "s"}]})[0], 400)

    def test_new_codes_are_rate_limited_per_address(self) -> None:
        self.ask.create_limit = self.ask.RateLimit(3, 3600)
        body = {"owner": digest("x")}
        for _ in range(3):
            self.assertEqual(self.post("/api/codes", body, {"X-Forwarded-For": "203.0.113.5"})[0], 201)
        self.assertEqual(self.post("/api/codes", body, {"X-Forwarded-For": "203.0.113.5"})[0], 429)
        self.assertEqual(self.post("/api/codes", body, {"X-Forwarded-For": "198.51.100.7"})[0], 201)

    def test_turn_credentials_for_the_owner_only(self) -> None:
        code = self.new_code("mine")
        status, body = self.post("/api/turn", {"code": code, "secret": "mine"})
        self.assertEqual(status, 200)
        self.assertEqual(body["urls"], ["turn:hub.example.test:3478?transport=udp",
                                        "turn:hub.example.test:3478?transport=tcp"])
        expiry, name = body["username"].split(":")
        self.assertEqual(name, code)
        self.assertGreater(int(expiry), 1_700_000_000)
        expected = base64.b64encode(hmac.new(b"turn-secret", body["username"].encode(), hashlib.sha1).digest())
        self.assertEqual(body["credential"], expected.decode())
        self.assertEqual(self.post("/api/turn", {"code": code, "secret": "wrong"})[0], 403)
        self.ask.codes.sync([(code, "mine", "", False)], 200.0)
        self.assertEqual(self.post("/api/turn", {"code": code, "secret": "mine"})[0], 403)

    def test_hub_info(self) -> None:
        self.assertEqual(json.loads(self.get("/api/hub")[2]), {"frpPort": 7000, "frpToken": "frp-token"})

    def test_route(self) -> None:
        live = self.new_code("a")
        waiting = self.new_code("b", "2026-11-20T19:30:00-03:00")
        retired = self.new_code("c")
        self.ask.codes.sync([(retired, "c", "", False)], 100.0)
        self.open_tunnels = {live: 20004}
        for uri in (f"/{live}", f"/{live}/", f"/{live}/m", f"/{live}/ws?x=1"):
            status, headers, _ = self.route(uri)
            self.assertEqual((status, headers.get("X-Solin-Tunnel-Port")), (200, "20004"), uri)
        status, headers, body = self.route(f"/{waiting}/s")
        self.assertEqual((status, headers.get("Retry-After")), (503, "10"))
        expected = f'data-message="waiting" data-code="{waiting}" data-starts="2026-11-20T19:30:00-03:00"'
        self.assertIn(expected.encode(), body)
        self.assertNotIn("X-Solin-Tunnel-Port", headers)
        status, _, body = self.route(f"/{retired}")
        self.assertEqual(status, 410)
        self.assertIn(b'data-message="retired"', body)
        # A retired code's tunnel is not reachable either.
        self.open_tunnels[retired] = 20005
        self.assertEqual(self.route(f"/{retired}")[0], 410)
        for uri in ("/zzzzzz", "/zzzzzzz", "/", ""):
            status, headers, _ = self.route(uri)
            self.assertEqual(status, 404, uri)
            self.assertNotIn("X-Solin-Tunnel-Port", headers)

    def test_status_and_health(self) -> None:
        self.open_tunnels = {"k7m4p2": 20001}
        self.assertEqual(self.get("/healthz")[2], b"ok\n")
        self.assertIn(b"Meetings open: 1.", self.get("/status")[2])

    def test_landing_page(self) -> None:
        status, headers, body = self.get("/")
        self.assertEqual((status, headers["Content-Type"]), (200, "text/html; charset=utf-8"))
        self.assertIn(b'action="/go"', body)
        self.assertIn(b'data-message=""', body)

    def go(self, query: str) -> tuple[int, str | None, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_address[1], timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/go?" + query)
        response = conn.getresponse()
        return response.status, response.getheader("Location"), response.read()

    def test_go_redirects_a_valid_code_and_forgives_typing(self) -> None:
        self.assertEqual(self.go("code=k7m4p2")[:2], (303, "/k7m4p2"))
        self.assertEqual(self.go("code=+K7M-4P2+")[:2], (303, "/k7m4p2"))
        self.assertEqual(self.go("code=https%3A%2F%2Fmeet.solin.run%2Fk7m4p2")[:2], (303, "/k7m4p2"))

    def test_go_keeps_an_invalid_code_on_the_landing_page(self) -> None:
        status, location, body = self.go("code=abc123")   # vowels and 1 are not in codes
        self.assertEqual((status, location), (200, None))
        self.assertIn(b'data-message="invalid" data-code="abc123"', body)
        status, _, body = self.go("code=%3Cscript%3E")
        self.assertIn(b'data-code="&lt;script&gt;"', body)
        self.assertIn(b'data-message=""', self.go("code=")[2])

    def test_an_unknown_meeting_code_shows_the_landing_page(self) -> None:
        for path in ("/k7m4p2", "/k7m4p2/m", "/k7m4p2/s"):
            status, _, body = self.get(path)
            self.assertEqual(status, 404)
            self.assertIn(b'data-message="notfound" data-code="k7m4p2"', body)
        self.assertEqual(self.get("/nothere")[2], b"not found\n")

    def test_plugin_fails_closed_on_garbage(self) -> None:
        with self.assertLogs("ask", "ERROR"):
            status, _, body = self.get("/internal/frps-plugin?op=NewProxy", data=b"not json")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["reject"])


if __name__ == "__main__":
    unittest.main()

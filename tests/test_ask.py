"""Unit tests for the ask service (ask/ask.py): hall registry, routing and the frps plugin."""

from __future__ import annotations

import hashlib
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


def load_ask(halls_file: str, codes_file: str):
    env = {"HUB_DOMAIN": "Hub.Example.Test.", "FRP_ADMIN_PASSWORD": "pw", "HALLS_FILE": halls_file,
           "CODES_FILE": codes_file, "HALL_PORT_MIN": "20000", "HALL_PORT_MAX": "20099"}
    with mock.patch.dict(os.environ, env):
        spec = importlib.util.spec_from_file_location("ask_under_test", ROOT / "ask" / "ask.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    return module


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class AskTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.halls_path = Path(self.dir.name) / "halls.json"
        self.write_halls({"hall-a": {"key_sha256": digest("key-a"), "port": 20001},
                          "hall-b": {"key_sha256": digest("key-b"), "port": 20002}})
        self.codes_path = Path(self.dir.name) / "codes.json"
        self.ask = load_ask(str(self.halls_path), str(self.codes_path))
        self.online_names: set[str] = {"hall-a"}
        patcher = mock.patch.object(self.ask.online, "names", side_effect=lambda: set(self.online_names))
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_halls(self, halls: dict) -> None:
        tmp = self.halls_path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"halls": halls}), encoding="utf-8")
        os.replace(tmp, self.halls_path)
        # Same-second rewrites of the same size would keep the cached stamp: bump mtime.
        stamp = getattr(self, "_mtime", 1_000_000) + 10
        self._mtime = stamp
        os.utime(self.halls_path, (stamp, stamp))


class HallRegistryTests(AskTestCase):
    def test_keys_are_checked_against_the_hash(self) -> None:
        self.assertIsNotNone(self.ask.halls.check_key("hall-a", "key-a"))
        self.assertIsNone(self.ask.halls.check_key("hall-a", "key-b"))
        self.assertIsNone(self.ask.halls.check_key("hall-a", ""))
        self.assertIsNone(self.ask.halls.check_key("nobody", "key-a"))

    def test_invalid_entries_are_skipped(self) -> None:
        self.write_halls({
            "hall-a": {"key_sha256": digest("key-a"), "port": 20001},
            "Bad_Name": {"key_sha256": digest("x"), "port": 20003},
            "short-hash": {"key_sha256": "abc", "port": 20004},
            "out-of-range": {"key_sha256": digest("x"), "port": 30000},
            "same-port": {"key_sha256": digest("x"), "port": 20001},
            "no-port": {"key_sha256": digest("x")},
        })
        with self.assertLogs("ask", "ERROR"):
            self.assertIsNotNone(self.ask.halls.get("hall-a"))
        for name in ("bad_name", "short-hash", "out-of-range", "same-port", "no-port"):
            self.assertIsNone(self.ask.halls.get(name), name)

    def test_edits_are_picked_up_and_a_broken_file_keeps_the_old_registry(self) -> None:
        self.write_halls({"hall-c": {"key_sha256": digest("key-c"), "port": 20005}})
        self.assertIsNotNone(self.ask.halls.get("hall-c"))
        self.assertIsNone(self.ask.halls.get("hall-a"))
        self.halls_path.write_text("{not json", encoding="utf-8")
        os.utime(self.halls_path, (2_000_000, 2_000_000))
        with self.assertLogs("ask", "ERROR"):
            self.assertIsNotNone(self.ask.halls.get("hall-c"))


class CodeRegistryTests(AskTestCase):
    def test_a_hall_registers_and_retires_its_codes(self) -> None:
        codes = self.ask.codes
        self.assertEqual(codes.register("hall-a", {"k7m4p2": "2026-11-20T19:30:00-03:00", "b2c3d4": ""}, [], 100.0), [])
        self.assertEqual(codes.lookup("k7m4p2"), {"hall": "hall-a", "starts_at": "2026-11-20T19:30:00-03:00",
                                                  "retired_at": 0.0})
        # A code left out is retired; one listed as retired is recorded even if never active.
        self.assertEqual(codes.register("hall-a", {"k7m4p2": ""}, ["zzzzzz"], 200.0), [])
        self.assertEqual(codes.lookup("b2c3d4")["retired_at"], 200.0)
        self.assertEqual(codes.lookup("zzzzzz")["hall"], "hall-a")
        # Saved, and read back by a fresh registry.
        again = self.ask.Codes(str(self.codes_path))
        self.assertEqual(again.lookup("b2c3d4")["retired_at"], 200.0)

    def test_another_halls_codes_are_refused_and_nothing_changes(self) -> None:
        codes = self.ask.codes
        codes.register("hall-a", {"k7m4p2": ""}, ["b2c3d4"], 100.0)
        self.assertEqual(codes.register("hall-b", {"k7m4p2": "", "b2c3d4": "", "x9x9x9": ""}, [], 150.0),
                         ["b2c3d4", "k7m4p2"])
        self.assertIsNone(codes.lookup("x9x9x9"))
        # Another hall's code listed as retired is ignored.
        self.assertEqual(codes.register("hall-b", {}, ["k7m4p2"], 150.0), [])
        self.assertEqual(codes.lookup("k7m4p2"), {"hall": "hall-a", "starts_at": "", "retired_at": 0.0})

    def test_retired_codes_are_forgotten_after_a_long_time(self) -> None:
        codes = self.ask.codes
        codes.register("hall-a", {}, ["b2c3d4"], 100.0)
        codes.register("hall-a", {}, [], 100.0 + self.ask.RETIRED_FOR_S + 1)
        self.assertIsNone(codes.lookup("b2c3d4"))
        self.assertEqual(codes.register("hall-b", {"b2c3d4": ""}, [], 100.0 + self.ask.RETIRED_FOR_S + 2), [])

    def test_parse_codes(self) -> None:
        parse = self.ask.parse_codes
        self.assertEqual(parse({"codes": [{"code": "k7m4p2", "startsAt": "x"}], "retired": ["b2c3d4"]}),
                         ({"k7m4p2": "x"}, ["b2c3d4"]))
        self.assertEqual(parse({"codes": []}), ({}, []))
        for bad in ({}, {"codes": "k7m4p2"}, {"codes": [{"code": "abc123"}]}, {"codes": [], "retired": ["NOPE"]},
                    {"codes": [{"code": "k7m4p2", "startsAt": "x" * 41}]}):
            self.assertIsNone(parse(bad), bad)


class PluginTests(AskTestCase):
    def login(self, hall: str, key: str) -> dict:
        return self.ask.plugin_decision("Login", {"content": {"metas": {"hall": hall, "hall_key": key}}})

    def new_proxy(self, hall: str, key: str, name: str, kind: str = "tcp", port: int = 0) -> dict:
        content = {"user": {"metas": {"hall": hall, "hall_key": key}}, "proxy_name": name,
                   "proxy_type": kind, "remote_port": port}
        return self.ask.plugin_decision("NewProxy", {"content": content})

    def test_login(self) -> None:
        self.assertFalse(self.login("hall-a", "key-a")["reject"])
        self.assertTrue(self.login("hall-a", "key-b")["reject"])
        self.assertTrue(self.login("nobody", "key-a")["reject"])

    def test_new_proxy_is_pinned_to_the_registered_port(self) -> None:
        decision = self.new_proxy("hall-a", "key-a", "hall-a", port=20099)
        self.assertFalse(decision["reject"])
        self.assertFalse(decision["unchange"])
        self.assertEqual(decision["content"]["remote_port"], 20001)

    def test_a_hall_cannot_take_another_halls_name_or_open_other_proxies(self) -> None:
        self.assertTrue(self.new_proxy("hall-b", "key-b", "hall-a")["reject"])
        self.assertTrue(self.new_proxy("hall-a", "wrong", "hall-a")["reject"])
        self.assertTrue(self.new_proxy("hall-a", "key-a", "hall-a", kind="http")["reject"])

    def test_other_operations_pass_through(self) -> None:
        self.assertEqual(self.ask.plugin_decision("Ping", {}), {"reject": False, "unchange": True})


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

    def put_codes(self, hall: str, key: str, body) -> tuple[int, dict]:
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        request = urllib.request.Request(f"{self.base}/api/halls/{hall}/codes", data=data, method="PUT",
                                         headers={"Authorization": f"Bearer {key}",
                                                  "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            with error:
                return error.code, json.loads(error.read())

    def route(self, uri: str):
        return self.get("/internal/route", {"X-Forwarded-Uri": uri})

    def test_put_codes(self) -> None:
        body = {"codes": [{"code": "k7m4p2", "startsAt": "2026-11-20T19:30:00-03:00"}], "retired": []}
        self.assertEqual(self.put_codes("hall-a", "key-a", body), (200, {"ok": True}))
        self.assertEqual(self.put_codes("hall-a", "key-b", body)[0], 401)
        self.assertEqual(self.put_codes("nobody", "key-a", body)[0], 401)
        self.assertEqual(self.put_codes("hall-b", "key-b", body), (409, {"taken": ["k7m4p2"]}))
        self.assertEqual(self.put_codes("hall-a", "key-a", b"{not json")[0], 400)
        self.assertEqual(self.put_codes("hall-a", "key-a", {"codes": [{"code": "aeiou1"}]})[0], 400)

    def test_route(self) -> None:
        self.ask.codes.register("hall-a", {"k7m4p2": ""}, ["b2c3d4"], 100.0)
        self.ask.codes.register("hall-b", {"x9x9x9": "2026-11-20T19:30:00-03:00"}, [], 100.0)
        for uri in ("/k7m4p2", "/k7m4p2/", "/k7m4p2/m", "/k7m4p2/ws?x=1"):
            status, headers, _ = self.route(uri)
            self.assertEqual((status, headers.get("X-Solin-Hall-Port")), (200, "20001"), uri)
        # hall-b is offline: the waiting page, with the start time for its countdown.
        status, headers, body = self.route("/x9x9x9/s")
        self.assertEqual((status, headers.get("Retry-After")), (503, "10"))
        self.assertIn(b'data-message="waiting" data-code="x9x9x9" data-starts="2026-11-20T19:30:00-03:00"', body)
        self.assertNotIn("X-Solin-Hall-Port", headers)
        status, _, body = self.route("/b2c3d4")
        self.assertEqual(status, 410)
        self.assertIn(b'data-message="retired"', body)
        for uri in ("/zzzzzz", "/zzzzzzz", "/", ""):
            status, headers, body = self.route(uri)
            self.assertEqual(status, 404, uri)
            self.assertNotIn("X-Solin-Hall-Port", headers)
        # A hall removed from halls.json no longer serves its codes.
        self.write_halls({"hall-b": {"key_sha256": digest("key-b"), "port": 20002}})
        self.assertEqual(self.route("/k7m4p2")[0], 404)

    def test_public_endpoints(self) -> None:
        self.assertEqual(self.get("/healthz")[2], b"ok\n")
        self.assertIn(b"Halls connected: 1.", self.get("/status")[2])
        self.assertEqual(json.loads(self.get("/api/halls/hall-a")[2]), {"online": True})
        self.assertEqual(json.loads(self.get("/api/halls/hall-b")[2]), {"online": False})
        self.assertEqual(self.get("/api/halls/Bad_Name")[0], 400)

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
            status, _, body = self.get("/internal/frps-plugin?op=Login", data=b"not json")
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["reject"])


if __name__ == "__main__":
    unittest.main()

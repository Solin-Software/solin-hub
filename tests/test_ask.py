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


def load_ask(halls_file: str):
    env = {"HUB_DOMAIN": "Hub.Example.Test.", "FRP_ADMIN_PASSWORD": "pw", "HALLS_FILE": halls_file,
           "HALL_PORT_MIN": "20000", "HALL_PORT_MAX": "20099"}
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
        self.ask = load_ask(str(self.halls_path))
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


class HostTests(AskTestCase):
    def test_hall_from_host(self) -> None:
        hall = self.ask.hall_from_host
        self.assertEqual(hall("hall-a.hub.example.test"), "hall-a")
        self.assertEqual(hall("HALL-A.hub.example.test:443"), "hall-a")
        self.assertEqual(hall("hall-a.hub.example.test."), "hall-a")
        self.assertIsNone(hall("hub.example.test"))
        self.assertIsNone(hall("a.b.hub.example.test"))
        self.assertIsNone(hall("hall-a.other.test"))
        self.assertIsNone(hall("[::1]:443"))
        self.assertIsNone(hall(""))


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

    def test_tls_gate(self) -> None:
        self.assertEqual(self.get("/internal/caddy-ask?domain=hub.example.test")[0], 200)
        self.assertEqual(self.get("/internal/caddy-ask?domain=hall-a.hub.example.test")[0], 200)
        self.assertEqual(self.get("/internal/caddy-ask?domain=hall-b.hub.example.test")[0], 403)  # offline
        self.assertEqual(self.get("/internal/caddy-ask?domain=nobody.hub.example.test")[0], 403)
        self.assertEqual(self.get("/internal/caddy-ask?domain=example.com")[0], 403)

    def test_route(self) -> None:
        status, headers, _ = self.get("/internal/route", {"X-Forwarded-Host": "hall-a.hub.example.test"})
        self.assertEqual((status, headers.get("X-Solin-Hall-Port")), (200, "20001"))
        status, headers, body = self.get("/internal/route", {"X-Forwarded-Host": "hall-b.hub.example.test"})
        self.assertEqual((status, headers.get("Retry-After")), (503, "5"))
        self.assertIn(b"not connected", body)
        self.assertNotIn("X-Solin-Hall-Port", headers)
        self.assertEqual(self.get("/internal/route", {"X-Forwarded-Host": "nobody.hub.example.test"})[0], 404)

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

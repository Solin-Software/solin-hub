"""Tests for scripts/add_hall.py: registry edits and the connection code Solin parses."""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "add_hall.py"
ENV = """HUB_DOMAIN=hub.example.test
FRP_BIND_PORT=7000
FRP_TOKEN=frp-token
TURN_SECRET=turn-secret
TURN_PORT=3478
"""


class AddHallTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.halls = self.dir / "halls.json"
        self.env = self.dir / "hub.env"
        self.env.write_text(ENV, encoding="utf-8")

    def add(self, *args: str) -> str:
        return subprocess.run(
            [sys.executable, "-I", str(SCRIPT), *args, "--halls", str(self.halls), "--env", str(self.env)],
            check=True, capture_output=True, text=True,
        ).stdout

    def registry(self) -> dict:
        return json.loads(self.halls.read_text(encoding="utf-8"))["halls"]

    def test_registers_halls_on_free_ports_and_stores_only_the_hash(self) -> None:
        self.add("hall-a", "--key", "secret-a")
        self.add("hall-b", "--key", "secret-b", "--note", "Congregation B")
        halls = self.registry()
        self.assertEqual(halls["hall-a"], {"key_sha256": hashlib.sha256(b"secret-a").hexdigest(), "port": 20001})
        self.assertEqual(halls["hall-b"]["port"], 20002)
        self.assertEqual(halls["hall-b"]["note"], "Congregation B")
        self.assertNotIn("secret-a", self.halls.read_text(encoding="utf-8"))

    def test_rotating_a_key_keeps_the_port(self) -> None:
        self.add("hall-a", "--key", "old")
        self.add("hall-b", "--key", "other")
        self.add("hall-a", "--key", "new")
        halls = self.registry()
        self.assertEqual(halls["hall-a"]["port"], 20001)
        self.assertEqual(halls["hall-a"]["key_sha256"], hashlib.sha256(b"new").hexdigest())

    def test_connection_code_has_the_fields_solin_reads(self) -> None:
        out = self.add("hall-a", "--key", "secret-a")
        line = next(x for x in out.splitlines() if x.startswith("solin-hub:"))
        raw = line.removeprefix("solin-hub:")
        code = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        self.assertEqual(code, {
            "domain": "hub.example.test", "frp_addr": "hub.example.test", "frp_port": 7000,
            "frp_token": "frp-token", "hall": "hall-a", "hall_key": "secret-a",
            "turn_secret": "turn-secret", "turn_port": 3478, "turn_host": "hub.example.test",
        })

    def test_rejects_invalid_names(self) -> None:
        result = subprocess.run([sys.executable, "-I", str(SCRIPT), "Bad_Name", "--halls", str(self.halls)],
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(self.halls.exists())


if __name__ == "__main__":
    unittest.main()

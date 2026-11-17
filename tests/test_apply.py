"""Tests for scripts/apply.py: configuration checks and the deploy plan."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("apply_under_test", ROOT / "scripts" / "apply.py")
apply = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = apply
spec.loader.exec_module(apply)

GOOD_ENV = {
    "HUB_DOMAIN": "hub.solin.test", "ACME_EMAIL": "ops@solin.test", "PUBLIC_IP": "198.51.100.7",
    "FRP_BIND_PORT": "7000", "FRP_TOKEN": "a" * 64, "FRP_ADMIN_PORT": "7500", "FRP_ADMIN_PASSWORD": "b" * 64,
    "HALL_PORT_MIN": "20000", "HALL_PORT_MAX": "20999", "ASK_PORT": "7400", "TURN_SECRET": "c" * 64,
}
ALL = set(apply.SERVICES)


class EnvTests(unittest.TestCase):
    def test_good_env_passes(self) -> None:
        self.assertEqual(apply.check_env(GOOD_ENV), [])

    def test_example_values_are_refused(self) -> None:
        env = dict(GOOD_ENV, FRP_TOKEN="change-me-openssl-rand-hex-32", TURN_SECRET="short",
                   HUB_DOMAIN="hub.example.org")
        del env["PUBLIC_IP"]
        problems = "\n".join(apply.check_env(env))
        for word in ("FRP_TOKEN", "TURN_SECRET", "HUB_DOMAIN", "PUBLIC_IP"):
            self.assertIn(word, problems)

    def test_the_shipped_example_is_refused(self) -> None:
        self.assertTrue(apply.check_env(apply.read_env(ROOT / "hub.env.example")))


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for files in apply.SERVICE_FILES.values():
            for rel in files:
                (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
                (self.root / rel).write_text(rel, encoding="utf-8")
        self.defs = {name: {"image": f"{name}:1"} for name in apply.SERVICES}

    def changed(self, before: dict, after: dict) -> set[str]:
        return {name for name in apply.SERVICES if before[name] != after[name]}

    def test_a_config_file_changes_only_its_service(self) -> None:
        before = apply.fingerprints(self.defs, self.root)
        (self.root / "frps.toml").write_text("changed", encoding="utf-8")
        self.assertEqual(self.changed(before, apply.fingerprints(self.defs, self.root)), {"frps"})

    def test_a_definition_change_is_seen(self) -> None:
        before = apply.fingerprints(self.defs, self.root)
        self.defs["coturn"] = {"image": "coturn:2"}
        self.assertEqual(self.changed(before, apply.fingerprints(self.defs, self.root)), {"coturn"})


class PlanTests(unittest.TestCase):
    fps = {name: f"fp-{name}" for name in apply.SERVICES}
    defs = {name: f"def-{name}" for name in apply.SERVICES}
    deployed = {"services": fps, "definitions": defs}

    def test_first_deploy_starts_everything_and_cuts_nothing(self) -> None:
        steps = apply.plan({}, self.fps, self.defs, running=set())
        self.assertEqual(set(steps["recreate"]), ALL)
        self.assertEqual(apply.disruptive(steps, running=set()), [])

    def test_nothing_changed(self) -> None:
        steps = apply.plan(self.deployed, self.fps, self.defs, running=ALL)
        self.assertEqual((steps["recreate"], steps["reload"]), ([], []))

    def test_caddyfile_only_change_is_a_live_reload(self) -> None:
        steps = apply.plan(self.deployed, dict(self.fps, caddy="new"), self.defs, running=ALL)
        self.assertEqual((steps["recreate"], steps["reload"]), ([], ["caddy"]))
        self.assertEqual(apply.disruptive(steps, running=ALL), [])

    def test_caddy_definition_change_recreates_and_is_disruptive(self) -> None:
        steps = apply.plan(self.deployed, dict(self.fps, caddy="new"), dict(self.defs, caddy="new"), running=ALL)
        self.assertEqual(steps["recreate"], ["caddy"])
        self.assertEqual(apply.disruptive(steps, running=ALL), ["caddy"])

    def test_tunnel_and_turn_changes_are_disruptive_but_ask_is_not(self) -> None:
        steps = apply.plan(self.deployed, dict(self.fps, ask="n", frps="n", coturn="n"), self.defs, running=ALL)
        self.assertEqual(set(steps["recreate"]), {"ask", "frps", "coturn"})
        self.assertEqual(sorted(apply.disruptive(steps, running=ALL)), ["coturn", "frps"])

    def test_a_stopped_service_is_started_without_counting_as_a_cut(self) -> None:
        running = ALL - {"frps"}
        steps = apply.plan(self.deployed, self.fps, self.defs, running=running)
        self.assertEqual(steps["recreate"], ["frps"])
        self.assertEqual(apply.disruptive(steps, running=running), [])


class MainTests(unittest.TestCase):
    def test_missing_hub_env_is_a_configuration_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(apply.main(["--root", tmp, "--dry-run"]), 2)
        self.assertIn("hub.env is missing", err.getvalue())


if __name__ == "__main__":
    unittest.main()

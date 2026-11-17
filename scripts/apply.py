#!/usr/bin/env python3
"""Apply the hub bundle in this directory to the running hub (run ON the VPS, from the bundle).

    python3 scripts/apply.py [--dry-run] [--force]

Used by the deploy workflow (through scripts/deploy.sh) and by hand. It:
  1. checks hub.env (present, no placeholder secrets) and creates an empty halls.json if needed;
  2. validates the compose file, the Caddyfile and frps.toml with the pinned images;
  3. works out which services changed since the last apply (compose definition + config files);
  4. refuses to restart the tunnel server or the TURN relay while halls are connected, unless
     --force: either restart cuts every meeting that is running;
  5. builds and (re)starts what changed; a Caddyfile-only change is a live `caddy reload`, which
     keeps meetings' WebSockets open;
  6. waits until every service is running and the ask service answers, then records the new state
     in .deployed/state.json.

Exit codes: 0 applied (or nothing to do), 1 failed, 2 bad configuration, 3 refused (halls online).
Standard library only; needs docker with the compose plugin.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
STATE_DIR = ".deployed"
SERVICES = ("ask", "frps", "coturn", "caddy")
# Restarting these drops every hall's tunnel / every relayed attendee.
DISRUPTIVE = frozenset({"frps", "coturn"})
# Files each service reads at runtime (bind mounts). Changes to them are invisible to compose.
SERVICE_FILES = {
    "ask": ("ask/ask.py", "ask/Dockerfile"),
    "frps": ("frps.toml",),
    "coturn": ("turnserver.conf", "coturn-entrypoint.sh"),
    "caddy": ("Caddyfile", "sites.caddy"),
}
REQUIRED_ENV = (
    "HUB_DOMAIN", "ACME_EMAIL", "PUBLIC_IP", "FRP_BIND_PORT", "FRP_TOKEN", "FRP_ADMIN_PORT",
    "FRP_ADMIN_PASSWORD", "HALL_PORT_MIN", "HALL_PORT_MAX", "ASK_PORT", "TURN_SECRET",
)
SECRETS = ("FRP_TOKEN", "FRP_ADMIN_PASSWORD", "TURN_SECRET")
HEALTH_TIMEOUT_S = 90


class ConfigError(Exception):
    pass


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def check_env(env: dict[str, str]) -> list[str]:
    """Problems that would make the hub insecure or broken; empty when hub.env is usable."""
    problems = [f"{key} is not set" for key in REQUIRED_ENV if not env.get(key)]
    for key in SECRETS:
        value = env.get(key, "")
        if value and ("change-me" in value or len(value) < 24):
            problems.append(f"{key} is a placeholder or too short (use: openssl rand -hex 32)")
    if env.get("HUB_DOMAIN", "").endswith("example.org"):
        problems.append("HUB_DOMAIN is still the example value")
    return problems


def fingerprints(compose_services: dict[str, dict], root: Path) -> dict[str, str]:
    """One hash per service over its resolved compose definition (image, env, mounts...) and the
    content of the config files it mounts."""
    result = {}
    for name in SERVICES:
        digest = hashlib.sha256()
        digest.update(json.dumps(compose_services.get(name, {}), sort_keys=True).encode())
        for rel in SERVICE_FILES[name]:
            path = root / rel
            digest.update(rel.encode() + b"\0")
            digest.update(path.read_bytes() if path.exists() else b"<missing>")
        result[name] = digest.hexdigest()
    return result


def definition_fingerprints(compose_services: dict[str, dict]) -> dict[str, str]:
    return {
        name: hashlib.sha256(json.dumps(compose_services.get(name, {}), sort_keys=True).encode()).hexdigest()
        for name in SERVICES
    }


def plan(previous: dict, current: dict, current_defs: dict, running: set[str]) -> dict[str, list[str]]:
    """Decide what to do per service.

    previous: the state recorded by the last apply ({"services": {...}, "definitions": {...}}).
    Returns {"recreate": [...], "reload": [...], "unchanged": [...]}. A service that is not running
    is always (re)created. Caddy is reloaded in place when only its Caddyfile/sites changed.
    """
    old = previous.get("services", {})
    old_defs = previous.get("definitions", {})
    out: dict[str, list[str]] = {"recreate": [], "reload": [], "unchanged": []}
    for name in SERVICES:
        if name not in running:
            out["recreate"].append(name)
        elif old.get(name) == current[name]:
            out["unchanged"].append(name)
        elif name == "caddy" and old_defs.get(name) == current_defs[name]:
            out["reload"].append(name)
        else:
            out["recreate"].append(name)
    return out


def disruptive(steps: dict[str, list[str]], running: set[str]) -> list[str]:
    """Running services whose restart would cut meetings that are in progress."""
    hits = [n for n in steps["recreate"] if n in running and n in DISRUPTIVE]
    # Recreating Caddy (not a reload) also closes every attendee's connection.
    if "caddy" in steps["recreate"] and "caddy" in running:
        hits.append("caddy")
    return hits


def halls_online(env: dict[str, str]) -> int:
    """Hall tunnels frps reports online; 0 when frps is not answering (then nothing is connected)."""
    url = f"http://127.0.0.1:{env.get('FRP_ADMIN_PORT', '7500')}/api/proxy/tcp"
    auth = base64.b64encode(f"admin:{env.get('FRP_ADMIN_PASSWORD', '')}".encode()).decode()
    request = urllib.request.Request(url, headers={"Authorization": "Basic " + auth})
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            proxies = json.load(response).get("proxies") or []
    except Exception:  # noqa: BLE001 - frps down or not deployed yet
        return 0
    return sum(1 for p in proxies if p.get("status") == "online")


class Hub:
    def __init__(self, root: Path, dry_run: bool) -> None:
        self.root = root
        self.dry_run = dry_run

    def run(self, *args: str, capture: bool = False, mutate: bool = True) -> str:
        if mutate and self.dry_run:
            print("  (dry run) would run:", " ".join(args))
            return ""
        result = subprocess.run(args, cwd=self.root, check=True, text=True,
                                stdout=subprocess.PIPE if capture else None)
        return result.stdout or ""

    def compose(self, *args: str, capture: bool = False, mutate: bool = True) -> str:
        return self.run("docker", "compose", *args, capture=capture, mutate=mutate)

    def services(self) -> dict[str, dict]:
        return json.loads(self.compose("config", "--format", "json", capture=True, mutate=False))["services"]

    def running(self) -> set[str]:
        out = self.compose("ps", "--status", "running", "--services", capture=True, mutate=False)
        return {line.strip() for line in out.splitlines() if line.strip()}

    def validate(self) -> None:
        self.compose("config", "--quiet", mutate=False)
        self.compose("run", "--rm", "--no-deps", "-T", "--entrypoint", "caddy", "caddy",
                     "validate", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile", mutate=False)
        self.compose("run", "--rm", "--no-deps", "-T", "frps", "verify", "-c", "/etc/frp/frps.toml", mutate=False)


def wait_healthy(hub: Hub, env: dict[str, str]) -> bool:
    url = f"http://127.0.0.1:{env.get('ASK_PORT', '7400')}/healthz"
    deadline = time.monotonic() + HEALTH_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                ask_ok = response.read().strip() == b"ok"
        except Exception:  # noqa: BLE001 - not up yet
            ask_ok = False
        if ask_ok and hub.running() >= set(SERVICES):
            return True
        time.sleep(2)
    return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="validate and print the plan only")
    parser.add_argument("--force", action="store_true",
                        help="restart the tunnel server / TURN relay even while halls are connected")
    parser.add_argument("--root", type=Path, default=HERE, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with docker's output
    root: Path = args.root
    hub = Hub(root, args.dry_run)

    env_path = root / "hub.env"
    if not env_path.exists():
        print(f"error: {env_path} is missing. Create it from hub.env.example (see INSTALL.md).", file=sys.stderr)
        return 2
    env = read_env(env_path)
    problems = check_env(env)
    if problems:
        print("error: hub.env is not ready:\n  - " + "\n  - ".join(problems), file=sys.stderr)
        return 2
    halls = root / "halls.json"
    if not halls.exists() and not args.dry_run:
        halls.write_text('{"halls": {}}\n', encoding="utf-8")
        halls.chmod(0o644)  # read by the ask container's unprivileged user
        print("created an empty halls.json (register halls with scripts/add_hall.py)")

    try:
        print("== validating the configuration")
        hub.validate()
        services = hub.services()
        running = hub.running()
    except subprocess.CalledProcessError as error:
        print(f"error: validation failed: {error}", file=sys.stderr)
        return 2

    state_path = root / STATE_DIR / "state.json"
    previous = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    current = fingerprints(services, root)
    current_defs = definition_fingerprints(services)
    steps = plan(previous, current, current_defs, running)
    print("== plan")
    for action in ("recreate", "reload", "unchanged"):
        print(f"  {action:9} {', '.join(steps[action]) or '-'}")

    cutting = disruptive(steps, running)
    if cutting:
        connected = halls_online(env)
        if connected and not args.force:
            print(f"refused: {connected} hall(s) are connected right now, and restarting "
                  f"{', '.join(cutting)} would cut their meetings. Deploy again when no meeting is "
                  "running, or pass --force (deploy workflow: input 'force').", file=sys.stderr)
            return 3
        if connected:
            print(f"warning: --force: restarting {', '.join(cutting)} with {connected} hall(s) connected")

    if args.dry_run:
        print("dry run: nothing changed")
        return 0
    if not steps["recreate"] and not steps["reload"]:
        print("nothing to do")
    try:
        if steps["recreate"]:
            print("== building and starting:", ", ".join(steps["recreate"]))
            hub.compose("build", "--pull", "ask")
            hub.compose("up", "-d", "--remove-orphans", "--force-recreate", "--no-deps", *steps["recreate"])
        if "caddy" in steps["reload"]:
            print("== reloading Caddy in place (open connections are kept)")
            hub.compose("exec", "-T", "caddy", "caddy", "reload", "--config", "/etc/caddy/Caddyfile",
                        "--adapter", "caddyfile")
    except subprocess.CalledProcessError as error:
        print(f"error: {error}", file=sys.stderr)
        hub.compose("ps", mutate=False)
        return 1

    print("== waiting for the services")
    if not wait_healthy(hub, env):
        print("error: the hub did not become healthy; recent logs:", file=sys.stderr)
        hub.compose("ps", mutate=False)
        hub.compose("logs", "--no-color", "--tail", "40", mutate=False)
        return 1
    state_path.parent.mkdir(exist_ok=True)
    state = {"services": current, "definitions": current_defs,
             "commit": os.environ.get("HUB_COMMIT", ""), "applied_at": int(time.time())}
    state_path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")
    print(f"hub is up: https://{env['HUB_DOMAIN']}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())

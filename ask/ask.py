"""Solin hub control service ("ask"). Standard library only.

Every meeting is at https://<HUB_DOMAIN>/<code>: a 6-character code that the hall's Solin
drew and registered here. The ask service keeps the code registry (CODES_FILE) and tells Caddy
which hall's tunnel serves each code.

Internal endpoints (bound to 127.0.0.1, never routed publicly by Caddy):
  GET  /internal/route                    Caddy forward_auth for /<code>/...: maps the code to
                                          its hall's tunnel port (X-Solin-Hall-Port), or answers
                                          the visitor directly (unknown code, link no longer
                                          valid, meeting not open yet).
  POST /internal/frps-plugin?op=...       frps server plugin (Login, NewProxy): only registered
                                          halls with the right key may connect, each hall may
                                          only publish its own proxy, and the hub decides the
                                          proxy's remote port.
Public endpoints (served on https://<HUB_DOMAIN>/):
  GET  /                    landing page: type a meeting code to go to https://<HUB_DOMAIN>/<code>
  GET  /go?code=<code>      the same without JavaScript: redirects to /<code> (or back with an error)
  GET  /<code>[/...]        a meeting code no hall serves (yet): the landing page with a message
  GET  /status              tiny status page
  GET  /api/halls/<name>    {"online": bool}
  PUT  /api/halls/<name>/codes   the hall's codes (Authorization: Bearer <hall key>):
                            {"codes": [{"code": "k7m4p2", "startsAt": "<ISO>" | ""}], "retired": [...]}
                            200 {"ok": true}, or 409 {"taken": [...]} when another hall has some
                            of them (nothing is changed; the hall draws new ones and retries).
  GET  /healthz             "ok"

Halls are registered in halls.json (re-read automatically when the file changes):
  {"halls": {"hall-abc123": {"key_sha256": "<sha256 hex of the hall key>", "port": 20001}}}
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import re
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

log = logging.getLogger("ask")

HUB = os.environ["HUB_DOMAIN"].strip().lower().rstrip(".")
LISTEN = os.environ.get("ASK_ADDR", "127.0.0.1")
PORT = int(os.environ.get("ASK_PORT", "7400"))
HALLS_FILE = os.environ.get("HALLS_FILE", "/hub/halls.json")
FRP_ADMIN = f"http://127.0.0.1:{os.environ.get('FRP_ADMIN_PORT', '7500')}"
FRP_AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['FRP_ADMIN_PASSWORD']}".encode()).decode()
PORT_MIN = int(os.environ.get("HALL_PORT_MIN", "20000"))
PORT_MAX = int(os.environ.get("HALL_PORT_MAX", "20999"))
ONLINE_CACHE_S = 2.0

HALL_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
# Meeting codes as Solin draws them: 6 characters, no vowels (no words) and nothing easy to misread.
MEETING_CODE = re.compile(r"^[23456789bcdfghjkmnpqrstvwxyz]{6}$")
LANDING = (Path(__file__).with_name("landing.html")).read_text(encoding="utf-8")
CODES_FILE = os.environ.get("CODES_FILE", "/data/codes.json")
# A code a hall stopped using shows "no longer valid" (and is never handed to another hall) this long.
RETIRED_FOR_S = 400 * 24 * 3600
MAX_ACTIVE_CODES = 200
MAX_RETIRED_CODES = 2000
MAX_BODY = 256 * 1024
PORT_HEADER = "X-Solin-Hall-Port"
HTML = "text/html; charset=utf-8"


class Halls:
    """halls.json, reloaded when its mtime/size changes. Invalid entries are skipped (logged)."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._stamp: tuple[float, int] | None = None
        self._halls: dict[str, dict] = {}
        self._lock = threading.Lock()

    def get(self, name: str) -> dict | None:
        self._maybe_reload()
        return self._halls.get(name)

    def _maybe_reload(self) -> None:
        try:
            st = os.stat(self.path)
        except OSError:
            if self._stamp is not None:
                log.warning("halls file %s disappeared; no hall can connect", self.path)
            self._stamp, self._halls = None, {}
            return
        stamp = (st.st_mtime, st.st_size)
        if stamp == self._stamp:
            return
        with self._lock:
            if stamp == self._stamp:
                return
            try:
                with open(self.path, encoding="utf-8") as f:
                    raw = json.load(f).get("halls") or {}
            except (OSError, ValueError, AttributeError) as e:
                log.error("cannot read %s (%s); keeping the previous registry", self.path, e)
                self._stamp = stamp
                return
            halls: dict[str, dict] = {}
            ports: dict[int, str] = {}
            for name, entry in raw.items():
                name = str(name).lower()
                try:
                    digest = str(entry["key_sha256"]).lower()
                    port = int(entry["port"])
                except (KeyError, TypeError, ValueError):
                    log.error("hall %r: needs key_sha256 and port; skipped", name)
                    continue
                if not HALL_NAME.match(name):
                    log.error("hall %r: invalid name (lowercase letters, digits, '-'); skipped", name)
                elif not re.fullmatch(r"[0-9a-f]{64}", digest):
                    log.error("hall %r: key_sha256 must be 64 hex chars; skipped", name)
                elif not PORT_MIN <= port <= PORT_MAX:
                    log.error("hall %r: port %d outside %d-%d; skipped", name, port, PORT_MIN, PORT_MAX)
                elif port in ports:
                    log.error("hall %r: port %d already used by %r; skipped", name, port, ports[port])
                else:
                    ports[port] = name
                    halls[name] = {"key_sha256": digest, "port": port}
            self._halls, self._stamp = halls, stamp
            log.info("loaded %d hall(s) from %s", len(halls), self.path)

    def check_key(self, name: str, key: str) -> dict | None:
        hall = self.get(name)
        if hall is None or not key:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return hall if hmac.compare_digest(digest, hall["key_sha256"]) else None


class Online:
    """Names of tcp proxies frps reports online (admin API), cached briefly."""

    def __init__(self) -> None:
        self._at = 0.0
        self._names: set[str] = set()
        self._lock = threading.Lock()

    def names(self) -> set[str]:
        with self._lock:
            if time.monotonic() - self._at > ONLINE_CACHE_S:
                req = urllib.request.Request(FRP_ADMIN + "/api/proxy/tcp", headers={"Authorization": FRP_AUTH})
                with urllib.request.urlopen(req, timeout=3) as r:
                    proxies = json.load(r).get("proxies") or []
                self._names = {p.get("name", "") for p in proxies if p.get("status") == "online"}
                self._at = time.monotonic()
            return set(self._names)

    def is_online(self, name: str) -> bool:
        try:
            return name in self.names()
        except Exception as e:  # noqa: BLE001 - frps unreachable: treat as offline
            log.warning("frps admin API unavailable: %s", e)
            return False


class Codes:
    """The meeting-code registry: {code: {"hall", "starts_at" (ISO or ""), "retired_at" (0 = in use)}}.

    A code belongs to one hall. Codes the hall stops using are kept as retired for
    RETIRED_FOR_S, so their link says "no longer valid" and no other hall can take them.
    Saved atomically after every change.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._codes: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f).get("codes") or {}
            self._codes = {str(code): {"hall": str(e["hall"]), "starts_at": str(e.get("starts_at") or ""),
                                       "retired_at": float(e.get("retired_at") or 0)}
                           for code, e in raw.items() if MEETING_CODE.match(str(code))}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, KeyError, TypeError) as e:
            log.error("cannot read %s (%s); starting with no codes", path, e)

    def lookup(self, code: str) -> dict | None:
        with self._lock:
            entry = self._codes.get(code)
            return dict(entry) if entry else None

    def register(self, hall: str, active: dict[str, str], retired: list[str], now: float) -> list[str]:
        """Make `active` (code -> start time) the hall's codes; returns the codes other halls hold."""

        with self._lock:
            taken = sorted(c for c in active if c in self._codes and self._codes[c]["hall"] != hall)
            if taken:
                return taken
            for code, entry in self._codes.items():
                if entry["hall"] == hall and not entry["retired_at"] and code not in active:
                    entry["retired_at"] = now
            for code in retired:
                entry = self._codes.get(code)
                if code in active or (entry and entry["hall"] != hall):
                    continue
                if entry is None:
                    self._codes[code] = {"hall": hall, "starts_at": "", "retired_at": now}
                elif not entry["retired_at"]:
                    entry["retired_at"] = now
            for code, starts_at in active.items():
                self._codes[code] = {"hall": hall, "starts_at": starts_at, "retired_at": 0.0}
            self._codes = {code: e for code, e in self._codes.items()
                           if not e["retired_at"] or now - e["retired_at"] < RETIRED_FOR_S}
            self._save()
            return []

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"codes": self._codes}, f, separators=(",", ":"))
        os.replace(tmp, self.path)


halls = Halls(HALLS_FILE)
online = Online()
codes = Codes(CODES_FILE)


def parse_codes(body: dict) -> tuple[dict[str, str], list[str]] | None:
    """A hall's PUT body -> (active code -> start time, retired codes), or None when malformed."""

    items, retired = body.get("codes"), body.get("retired") or []
    if not isinstance(items, list) or not isinstance(retired, list):
        return None
    if len(items) > MAX_ACTIVE_CODES or len(retired) > MAX_RETIRED_CODES:
        return None
    active: dict[str, str] = {}
    for item in items:
        if not isinstance(item, dict) or not MEETING_CODE.match(str(item.get("code", ""))):
            return None
        starts_at = str(item.get("startsAt") or "")
        if len(starts_at) > 40:
            return None
        active[item["code"]] = starts_at
    if not all(isinstance(c, str) and MEETING_CODE.match(c) for c in retired):
        return None
    return active, retired


def plugin_decision(op: str, body: dict) -> dict:
    content = body.get("content") or {}
    if op == "Login":
        metas = content.get("metas") or {}
        name = str(metas.get("hall", "")).lower()
        if halls.check_key(name, str(metas.get("hall_key", ""))) is None:
            log.info("Login rejected: hall=%r from %s", name, content.get("client_address"))
            return {"reject": True, "reject_reason": "unknown hall or wrong hall key"}
        log.info("Login ok: hall=%r from %s", name, content.get("client_address"))
        return {"reject": False, "unchange": True}
    if op == "NewProxy":
        metas = (content.get("user") or {}).get("metas") or {}
        name = str(metas.get("hall", "")).lower()
        hall = halls.check_key(name, str(metas.get("hall_key", "")))
        if hall is None:
            return {"reject": True, "reject_reason": "unknown hall or wrong hall key"}
        if content.get("proxy_name") != name:
            log.info("NewProxy rejected: hall=%r tried proxy %r", name, content.get("proxy_name"))
            return {"reject": True, "reject_reason": f"the proxy must be named {name!r} (and frpc 'user' left empty)"}
        if content.get("proxy_type") != "tcp":
            return {"reject": True, "reject_reason": "only one proxy of type 'tcp' is allowed"}
        content["remote_port"] = hall["port"]  # the hub, not the hall, decides the port
        log.info("NewProxy ok: hall=%r -> 127.0.0.1:%d", name, hall["port"])
        return {"reject": False, "unchange": False, "content": content}
    return {"reject": False, "unchange": True}


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">{refresh}<title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}}}h1{{font-size:1.3rem}}</style>
</head><body><h1>{title}</h1><p>{text}</p></body></html>"""


def page(title: str, text: str, refresh: int | None = None) -> bytes:
    meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
    return PAGE.format(refresh=meta, title=html.escape(title), text=text).encode("utf-8")


def normalize_code(text: str) -> str:
    """What people type or paste: capitals, spaces, dashes, or the whole link."""

    return re.sub(r"[\s-]", "", text.strip().lower().rsplit("/", 1)[-1])


def landing(message: str = "", code: str = "", starts_at: str = "") -> bytes:
    """message: "" | "invalid" | "notfound" | "retired" | "waiting" (shown in the visitor's language)."""

    return (LANDING.replace("{{MESSAGE}}", html.escape(message)).replace("{{CODE}}", html.escape(code))
            .replace("{{STARTS}}", html.escape(starts_at)).encode("utf-8"))


class Handler(BaseHTTPRequestHandler):
    server_version = "solin-hub-ask"
    sys_version = ""

    def _send(self, status: int, body: bytes = b"", ctype: str = "text/plain; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, data: dict) -> None:
        self._send(status, json.dumps(data).encode(), "application/json")

    def do_HEAD(self) -> None:
        self.do_GET()

    def do_GET(self) -> None:
        url = urlparse(self.path)
        path = url.path
        if path == "/internal/route":
            self._route()
        elif path == "/healthz":
            self._send(200, b"ok\n")
        elif path == "/":
            self._send(200, landing(), HTML)
        elif path == "/go":
            code = normalize_code((parse_qs(url.query).get("code") or [""])[0])
            if MEETING_CODE.match(code):
                self._send(303, headers={"Location": f"/{quote(code)}"})
            else:
                self._send(200, landing("invalid" if code else "", code[:12]), HTML)
        elif path == "/status":
            try:
                n = len([x for x in online.names() if halls.get(x) is not None])
                text = f"The hub is running. Halls connected: {n}."
            except Exception:  # noqa: BLE001
                text = "The hub is running, but the tunnel server is not answering."
            self._send(200, page("Solin meeting hub", text), HTML)
        elif path.startswith("/api/halls/"):
            name = path[len("/api/halls/"):].strip("/").lower()
            if not HALL_NAME.match(name):
                self._json(400, {"error": "invalid hall name"})
            else:
                self._json(200, {"online": halls.get(name) is not None and online.is_online(name)})
        elif MEETING_CODE.match(path.strip("/").split("/", 1)[0]):
            # Caddy routes /<code>/... through /internal/route; only a direct request lands here.
            self._send(404, landing("notfound", path.strip("/").split("/", 1)[0]), HTML)
        else:
            self._send(404, b"not found\n")

    def _route(self) -> None:
        uri = self.headers.get("X-Forwarded-Uri") or ""
        code = urlparse(uri).path.strip("/").split("/", 1)[0].lower()
        entry = codes.lookup(code) if MEETING_CODE.match(code) else None
        hall = halls.get(entry["hall"]) if entry else None
        if entry is None or hall is None:
            self._send(404, landing("notfound", code[:12]), HTML)
        elif entry["retired_at"]:
            self._send(410, landing("retired", code), HTML)
        elif not online.is_online(entry["hall"]):
            # The hall's Solin opens its tunnel when the meeting opens; the page polls until then.
            self._send(503, landing("waiting", code, entry["starts_at"]), HTML, {"Retry-After": "10"})
        else:
            self._send(200, headers={PORT_HEADER: str(hall["port"])})

    def do_PUT(self) -> None:
        parts = urlparse(self.path).path.strip("/").split("/")
        if len(parts) != 4 or parts[:2] != ["api", "halls"] or parts[3] != "codes":
            self._send(404, b"not found\n")
            return
        name = parts[2].lower()
        auth = self.headers.get("Authorization") or ""
        key = auth[len("Bearer "):] if auth.startswith("Bearer ") else ""
        if halls.check_key(name, key) is None:
            self._json(401, {"error": "unknown hall or wrong hall key"})
            return
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            self._json(413 if length else 400, {"error": "body missing or too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            body = None
        parsed = parse_codes(body) if isinstance(body, dict) else None
        if parsed is None:
            self._json(400, {"error": "expected {codes: [{code, startsAt}], retired: [code]}"})
            return
        taken = codes.register(name, parsed[0], parsed[1], time.time())
        if taken:
            log.info("codes for hall=%r: %d taken by other halls", name, len(taken))
            self._json(409, {"taken": taken})
        else:
            log.info("codes for hall=%r: %d active, %d retired", name, len(parsed[0]), len(parsed[1]))
            self._json(200, {"ok": True})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/internal/frps-plugin":
            self._send(404, b"not found\n")
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            op = (parse_qs(url.query).get("op") or [body.get("op", "")])[0]
            self._json(200, plugin_decision(op, body))
        except Exception as e:  # noqa: BLE001 - fail closed
            log.exception("plugin error")
            self._json(200, {"reject": True, "reject_reason": f"hub plugin error: {e}"})

    def log_message(self, fmt: str, *args) -> None:  # access log is noisy (forward_auth per request)
        pass


def main() -> None:
    logging.basicConfig(level=os.environ.get("ASK_LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    halls.get("")  # load + validate at startup so errors show in the logs
    server = ThreadingHTTPServer((LISTEN, PORT), Handler)
    server.daemon_threads = True
    log.info("ask listening on %s:%d, hub domain %s, halls file %s", LISTEN, PORT, HUB, HALLS_FILE)
    server.serve_forever()


if __name__ == "__main__":
    main()

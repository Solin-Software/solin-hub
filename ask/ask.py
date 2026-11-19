"""Solin hub control service ("ask"). Standard library only.

Every meeting is at https://<HUB_DOMAIN>/<code>: a 6-character code that this service
creates. Whoever asks for a code sends the sha256 of a secret only it knows; that secret
then proves ownership of the code: updating its start time, retiring it, opening the
meeting's tunnel and getting TURN credentials for it. No account or registration: the hub
is open to every Solin.

Internal endpoints (bound to 127.0.0.1, never routed publicly by Caddy):
  GET  /internal/route                    Caddy forward_auth for /<code>/...: the port of the
                                          code's tunnel (X-Solin-Tunnel-Port), or an answer for
                                          the visitor (unknown code, link no longer valid,
                                          meeting not open yet).
  POST /internal/frps-plugin?op=...       frps server plugin (NewProxy): a tunnel is named
                                          after a code and needs that code's secret; the hub
                                          picks its port.
Public endpoints (served on https://<HUB_DOMAIN>/):
  GET  /                    landing page: type a meeting code to go to https://<HUB_DOMAIN>/<code>
  GET  /go?code=<code>      the same without JavaScript: redirects to /<code> (or back with an error)
  GET  /status              tiny status page
  GET  /healthz             "ok"
  GET  /api/hub             what Solin needs for tunnels: {"frpPort", "frpToken"}
  POST /api/codes           {"owner": "<sha256 hex of a new secret>", "startsAt": "<ISO>" | "",
                             "title": "<congregation / hall name>", "kind": "midweek" | "weekend" | ...}
                            -> 201 {"code": "k7m4p2"}  (rate-limited per client address)
  POST /api/codes/sync      {"codes": [{"code", "secret", "startsAt", "active", "title", "kind"}]}
                            -> 200 {"rejected": [codes whose secret is wrong or that are unknown]}
                            Sets each code's start time (the waiting page's countdown), the name
                            and kind of meeting the waiting page shows, and whether it is active;
                            an inactive code's link says "no longer valid".
  POST /api/turn            {"code", "secret"} -> {"urls", "username", "credential", "ttl"}
                            time-limited TURN credentials for that code's meeting
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import ipaddress
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

log = logging.getLogger("ask")

HUB = os.environ["HUB_DOMAIN"].strip().lower().rstrip(".")
LISTEN = os.environ.get("ASK_ADDR", "127.0.0.1")
PORT = int(os.environ.get("ASK_PORT", "7400"))
CODES_FILE = os.environ.get("CODES_FILE", "/data/codes.json")
FRP_ADMIN = f"http://127.0.0.1:{os.environ.get('FRP_ADMIN_PORT', '7500')}"
FRP_AUTH = "Basic " + base64.b64encode(f"admin:{os.environ['FRP_ADMIN_PASSWORD']}".encode()).decode()
FRP_TOKEN = os.environ.get("FRP_TOKEN", "")
FRP_BIND_PORT = int(os.environ.get("FRP_BIND_PORT", "7000"))
PORT_MIN = int(os.environ.get("TUNNEL_PORT_MIN", os.environ.get("HALL_PORT_MIN", "20000")))
PORT_MAX = int(os.environ.get("TUNNEL_PORT_MAX", os.environ.get("HALL_PORT_MAX", "20999")))
TURN_SECRET = os.environ.get("TURN_SECRET", "")
TURN_HOST = os.environ.get("TURN_HOST") or HUB
TURN_PORT = int(os.environ.get("TURN_PORT", "3478"))
TURN_TTL_S = 12 * 3600          # a meeting window is 3 h; generous for a late start
ONLINE_CACHE_S = 2.0

# Meeting codes: 6 characters, no vowels (no words) and nothing easy to misread (0 o 1 l i).
CODE_ALPHABET = "23456789bcdfghjkmnpqrstvwxyz"
MEETING_CODE = re.compile(f"^[{CODE_ALPHABET}]{{6}}$")
OWNER = re.compile(r"^[0-9a-f]{64}$")
# A code nobody has synced (or that was retired) this long is forgotten and may be issued again.
FORGET_AFTER_S = 400 * 24 * 3600
MAX_SYNC_CODES = 300
MAX_TITLE = 80
KINDS = ("", "midweek", "weekend", "extra", "unscheduled")   # the waiting page names each in the visitor's language
MAX_BODY = 256 * 1024
# New codes per client address (an IPv6 /64 counts as one): a congregation needs ~10 a month;
# this only stops floods. The global limit and the registry cap bound what many addresses can do.
CREATE_LIMIT = (120, 3600.0)
CREATE_LIMIT_ALL = (3000, 3600.0)
MAX_CODES = 200_000
# Codes that do not exist, per client address: a typo or two is normal; past this the address
# is probing for codes, and every code that is not an open meeting answers "too many attempts"
# (so the waiting page's name and start time cannot be harvested). Open meetings still route.
MISS_LIMIT = (20, 900.0)
LANDING = (Path(__file__).with_name("landing.html")).read_text(encoding="utf-8")
PORT_HEADER = "X-Solin-Tunnel-Port"
HTML = "text/html; charset=utf-8"


def digest(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def clean_title(value: object) -> str:
    return " ".join(str(value or "").split())[:MAX_TITLE]


def clean_kind(value: object) -> str:
    return str(value) if value in KINDS else ""


class Codes:
    """The code registry: {code: {"owner", "starts_at", "title", "kind", "active", "created_at", "seen_at"}}.

    ``owner`` is the sha256 of the code's secret. Saved atomically after every change.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._codes: dict[str, dict] = {}
        try:
            with open(path, encoding="utf-8") as f:
                raw = json.load(f).get("codes") or {}
            for code, e in raw.items():
                if MEETING_CODE.match(str(code)) and OWNER.match(str(e.get("owner", ""))):
                    self._codes[str(code)] = {
                        "owner": str(e["owner"]), "starts_at": str(e.get("starts_at") or ""),
                        "title": clean_title(e.get("title")), "kind": clean_kind(e.get("kind")),
                        "active": bool(e.get("active", True)), "created_at": float(e.get("created_at") or 0),
                        "seen_at": float(e.get("seen_at") or 0)}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as e:
            log.error("cannot read %s (%s); starting with no codes", path, e)

    def lookup(self, code: str) -> dict | None:
        with self._lock:
            entry = self._codes.get(code)
            return dict(entry) if entry else None

    def owns(self, code: str, secret: str) -> bool:
        entry = self.lookup(code)
        return bool(entry and secret and hmac.compare_digest(entry["owner"], digest(secret)))

    def create(self, owner: str, starts_at: str, now: float, title: str = "", kind: str = "") -> str | None:
        """A new code, or None when the registry is full."""

        with self._lock:
            self._forget(now)
            if len(self._codes) >= MAX_CODES:
                return None
            while True:
                code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(6))
                if code not in self._codes:
                    break
            self._codes[code] = {"owner": owner, "starts_at": starts_at, "title": title, "kind": kind,
                                 "active": True,
                                 "created_at": now, "seen_at": now}
            self._save()
            return code

    def sync(self, items: list[tuple[str, str, str, bool, str, str]], now: float) -> list[str]:
        """items: (code, secret, starts_at, active, title, kind).

        Returns the codes refused (unknown or wrong secret).
        """

        rejected: list[str] = []
        with self._lock:
            for code, secret, starts_at, active, title, kind in items:
                entry = self._codes.get(code)
                if entry is None or not hmac.compare_digest(entry["owner"], digest(secret)):
                    rejected.append(code)
                    continue
                entry.update(starts_at=starts_at, title=title, kind=kind, active=active, seen_at=now)
            self._forget(now)
            self._save()
        return rejected

    def _forget(self, now: float) -> None:
        self._codes = {code: e for code, e in self._codes.items()
                       if now - max(e["seen_at"], e["created_at"]) < FORGET_AFTER_S}

    def _save(self) -> None:
        tmp = f"{self.path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"codes": self._codes}, f, separators=(",", ":"))
        os.replace(tmp, self.path)


class Tunnels:
    """The tunnels frps has open: {name (= code): remote port}, from its admin API, cached briefly."""

    def __init__(self) -> None:
        self._at = 0.0
        self._ports: dict[str, int] = {}
        self._lock = threading.Lock()
        self._reserved: dict[int, float] = {}   # ports just handed out, until frps reports them

    def ports(self) -> dict[str, int]:
        with self._lock:
            if time.monotonic() - self._at > ONLINE_CACHE_S:
                req = urllib.request.Request(FRP_ADMIN + "/api/proxy/tcp", headers={"Authorization": FRP_AUTH})
                with urllib.request.urlopen(req, timeout=3) as r:
                    proxies = json.load(r).get("proxies") or []
                ports = {}
                for p in proxies:
                    if p.get("status") != "online":
                        continue
                    conf = p.get("conf") or {}
                    port = conf.get("remotePort") or conf.get("remote_port")
                    if p.get("name") and port:
                        ports[str(p["name"])] = int(port)
                self._ports = ports
                self._at = time.monotonic()
            return dict(self._ports)

    def port_of(self, name: str) -> int | None:
        try:
            return self.ports().get(name)
        except Exception as e:  # noqa: BLE001 - frps unreachable: treat as offline
            log.warning("frps admin API unavailable: %s", e)
            return None

    def free_port(self) -> int | None:
        try:
            used = set(self.ports().values())
        except Exception as e:  # noqa: BLE001
            log.warning("frps admin API unavailable: %s", e)
            return None
        now = time.monotonic()
        with self._lock:
            self._reserved = {p: t for p, t in self._reserved.items() if now - t < 30}
            free = [p for p in range(PORT_MIN, PORT_MAX + 1) if p not in used and p not in self._reserved]
            if not free:
                return None
            port = secrets.choice(free)
            self._reserved[port] = now
            self._at = 0.0   # the next lookup asks frps again
            return port


class RateLimit:
    def __init__(self, limit: int, window_s: float) -> None:
        self.limit, self.window_s = limit, window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def over(self, key: str, now: float) -> bool:
        """Whether ``key`` is at its limit, without counting a hit."""

        with self._lock:
            hits = self._hits.get(key)
            return bool(hits) and sum(1 for t in hits if now - t < self.window_s) >= self.limit

    def allow(self, key: str, now: float) -> bool:
        with self._lock:
            if len(self._hits) > 10_000:
                self._hits = {k: q for k, q in self._hits.items() if q and now - q[-1] < self.window_s}
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(now)
            return True


codes = Codes(CODES_FILE)
tunnels = Tunnels()
create_limit = RateLimit(*CREATE_LIMIT)
create_limit_all = RateLimit(*CREATE_LIMIT_ALL)
miss_limit = RateLimit(*MISS_LIMIT)


def client_key(address: str) -> str:
    """Rate-limit key: an IPv4 address, or an IPv6 address's /64 (one subscriber gets a whole /64)."""

    try:
        ip = ipaddress.ip_address(address.strip().strip("[]"))
    except ValueError:
        return address
    if ip.version == 4:
        return str(ip)
    if ip.ipv4_mapped is not None:
        return str(ip.ipv4_mapped)
    return str(ipaddress.ip_network(f"{ip}/64", strict=False))


def plugin_decision(op: str, body: dict) -> dict:
    """frps server plugin. Login is checked by frps itself (token); a proxy must be a code's."""

    content = body.get("content") or {}
    if op != "NewProxy":
        return {"reject": False, "unchange": True}
    metas = (content.get("user") or {}).get("metas") or {}
    code = str(metas.get("code", ""))
    entry = codes.lookup(code) if MEETING_CODE.match(code) else None
    if entry is None or not codes.owns(code, str(metas.get("secret", ""))):
        log.info("NewProxy rejected: code=%r (unknown code or wrong secret)", code)
        return {"reject": True, "reject_reason": "unknown meeting code or wrong secret"}
    if content.get("proxy_name") != code:
        return {"reject": True, "reject_reason": f"the proxy must be named {code!r} (and frpc 'user' left empty)"}
    if content.get("proxy_type") != "tcp":
        return {"reject": True, "reject_reason": "only one proxy of type 'tcp' is allowed"}
    port = tunnels.free_port()
    if port is None:
        return {"reject": True, "reject_reason": "the hub has no free tunnel port right now"}
    content["remote_port"] = port   # the hub, not the client, decides the port
    log.info("NewProxy ok: code=%s -> 127.0.0.1:%d", code, port)
    return {"reject": False, "unchange": False, "content": content}


def turn_credentials(code: str, now: float) -> dict:
    """TURN REST credentials (coturn use-auth-secret): username "<expiry>:<code>"."""

    username = f"{int(now) + TURN_TTL_S}:{code}"
    credential = base64.b64encode(hmac.new(TURN_SECRET.encode(), username.encode(), hashlib.sha1).digest()).decode()
    host = f"[{TURN_HOST}]" if ":" in TURN_HOST and not TURN_HOST.startswith("[") else TURN_HOST
    return {"urls": [f"turn:{host}:{TURN_PORT}?transport=udp", f"turn:{host}:{TURN_PORT}?transport=tcp"],
            "username": username, "credential": credential, "ttl": TURN_TTL_S}


def parse_sync(body: dict) -> list[tuple[str, str, str, bool, str, str]] | None:
    items = body.get("codes")
    if not isinstance(items, list) or len(items) > MAX_SYNC_CODES:
        return None
    parsed = []
    for item in items:
        if not isinstance(item, dict) or not MEETING_CODE.match(str(item.get("code", ""))):
            return None
        secret, starts_at = str(item.get("secret") or ""), str(item.get("startsAt") or "")
        if not 0 < len(secret) <= 200 or len(starts_at) > 40:
            return None
        parsed.append((str(item["code"]), secret, starts_at, bool(item.get("active", True)),
                       clean_title(item.get("title")), clean_kind(item.get("kind"))))
    return parsed


PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{title}</title>
<style>body{{font:16px/1.5 system-ui,sans-serif;max-width:32rem;margin:4rem auto;padding:0 1rem;color:#222}}
@media(prefers-color-scheme:dark){{body{{background:#111;color:#ddd}}}}h1{{font-size:1.3rem}}</style>
</head><body><h1>{title}</h1><p>{text}</p></body></html>"""


def page(title: str, text: str) -> bytes:
    return PAGE.format(title=html.escape(title), text=text).encode("utf-8")


def normalize_code(text: str) -> str:
    """What people type or paste: capitals, spaces, dashes, or the whole link."""

    return re.sub(r"[\s-]", "", text.strip().lower().rsplit("/", 1)[-1])


def landing(message: str = "", code: str = "", starts_at: str = "", title: str = "", kind: str = "") -> bytes:
    """message: "" | "invalid" | "notfound" | "retired" | "toomany" | "waiting" (in the visitor's language)."""

    fields = {"MESSAGE": message, "CODE": code, "STARTS": starts_at, "TITLE": title, "KIND": kind}
    page = LANDING
    for name, value in fields.items():
        page = page.replace("{{" + name + "}}", html.escape(value))
    return page.encode("utf-8")


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

    def _client(self) -> str:
        # Caddy sets X-Forwarded-For to the visitor's address (replacing any the visitor sent);
        # ask only listens on loopback.
        return client_key((self.headers.get("X-Forwarded-For") or self.client_address[0]).split(",")[0].strip())

    def _body(self) -> dict | None:
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return None
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return None
        return body if isinstance(body, dict) else None

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
                text = f"The hub is running. Meetings open: {len(tunnels.ports())}."
            except Exception:  # noqa: BLE001
                text = "The hub is running, but the tunnel server is not answering."
            self._send(200, page("Solin meeting hub", text), HTML)
        elif path == "/api/hub":
            self._json(200, {"frpPort": FRP_BIND_PORT, "frpToken": FRP_TOKEN})
        elif MEETING_CODE.match(path.strip("/").split("/", 1)[0]):
            # Caddy routes /<code>/... through /internal/route; only a direct request lands here.
            self._send(404, landing("notfound", path.strip("/").split("/", 1)[0]), HTML)
        else:
            self._send(404, b"not found\n")

    def _route(self) -> None:
        uri = self.headers.get("X-Forwarded-Uri") or ""
        code = urlparse(uri).path.strip("/").split("/", 1)[0].lower()
        entry = codes.lookup(code) if MEETING_CODE.match(code) else None
        port = tunnels.port_of(code) if entry and entry["active"] else None
        client, now = self._client(), time.monotonic()
        if port is None and (miss_limit.over(client, now) or (entry is None and not miss_limit.allow(client, now))):
            log.info("too many unknown codes from %s", client)
            self._send(429, landing("toomany"), HTML, {"Retry-After": str(int(MISS_LIMIT[1]))})
        elif entry is None:
            self._send(404, landing("notfound", code[:12]), HTML)
        elif not entry["active"]:
            self._send(410, landing("retired", code), HTML)
        elif port is None:
            # The owner's Solin opens the tunnel when the meeting opens; the page polls until then.
            self._send(503, landing("waiting", code, entry["starts_at"], entry["title"], entry["kind"]), HTML,
                       {"Retry-After": "10"})
        else:
            self._send(200, headers={PORT_HEADER: str(port)})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path == "/internal/frps-plugin":
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                op = (parse_qs(url.query).get("op") or [body.get("op", "")])[0]
                self._json(200, plugin_decision(op, body))
            except Exception as e:  # noqa: BLE001 - fail closed
                log.exception("plugin error")
                self._json(200, {"reject": True, "reject_reason": f"hub plugin error: {e}"})
            return
        if url.path not in ("/api/codes", "/api/codes/sync", "/api/turn"):
            self._send(404, b"not found\n")
            return
        body = self._body()
        if body is None:
            self._json(400, {"error": "expected a JSON object"})
        elif url.path == "/api/codes":
            owner, starts_at = str(body.get("owner") or ""), str(body.get("startsAt") or "")
            if not OWNER.match(owner) or len(starts_at) > 40:
                self._json(400, {"error": "expected {owner: <sha256 hex of a secret>, startsAt}"})
            elif not create_limit.allow(self._client(), time.monotonic()):
                self._json(429, {"error": "too many new codes from this address; try again later"})
            elif not create_limit_all.allow("*", time.monotonic()):
                log.warning("global new-code limit reached")
                self._json(503, {"error": "the hub is giving out too many codes right now; try again later"})
            else:
                code = codes.create(owner, starts_at, time.time(), clean_title(body.get("title")),
                                    clean_kind(body.get("kind")))
                if code is None:
                    log.error("code registry full (%d codes)", MAX_CODES)
                    self._json(503, {"error": "the hub cannot take new codes right now"})
                else:
                    log.info("code created: %s", code)
                    self._json(201, {"code": code})
        elif url.path == "/api/codes/sync":
            items = parse_sync(body)
            if items is None:
                self._json(400, {"error": "expected {codes: [{code, secret, startsAt, active, title, kind}]}"})
            else:
                self._json(200, {"rejected": codes.sync(items, time.time())})
        else:
            code, secret = str(body.get("code") or ""), str(body.get("secret") or "")
            entry = codes.lookup(code) if MEETING_CODE.match(code) else None
            if entry is None or not entry["active"] or not codes.owns(code, secret):
                self._json(403, {"error": "unknown meeting code or wrong secret"})
            elif not TURN_SECRET:
                self._json(404, {"error": "this hub has no TURN relay"})
            else:
                self._json(200, turn_credentials(code, time.time()))

    def log_message(self, fmt: str, *args) -> None:  # access log is noisy (forward_auth per request)
        pass


def main() -> None:
    logging.basicConfig(level=os.environ.get("ASK_LOG_LEVEL", "INFO"), stream=sys.stdout,
                        format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((LISTEN, PORT), Handler)
    server.daemon_threads = True
    log.info("ask listening on %s:%d, hub domain %s, codes file %s", LISTEN, PORT, HUB, CODES_FILE)
    server.serve_forever()


if __name__ == "__main__":
    main()

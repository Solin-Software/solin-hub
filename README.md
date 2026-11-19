# Solin meeting hub

This repository runs **https://meet.solin.run**, the one server behind Solin's built-in (Galene-based) hosted meetings. The Solin project operates it. It is not meant to be self-hosted: there are no community or third-party hubs, and Solin always uses `meet.solin.run`. Only a developer environment variable, `SOLIN_MEETING_SERVER`, points Solin at another hub, for local testing.

The hub gives every Solin that runs a meeting two things:
- a public HTTPS link for the meeting, `https://meet.solin.run/<code>`, that works even when the network of the PC running Solin is behind CGNAT;
- a TURN relay for attendees whose network blocks direct media.

The meeting itself does not run here. Galene, the lobby, passwords, tokens, attendance and media rules all live on the PC running Solin. The hub keeps only the **meeting-code registry**: which codes exist, who owns each one (the sha256 of a secret that stays in Solin), when each meeting starts, its name and kind for the waiting page, and whether its code is still valid. There is no registration and no account.

The Solin side (Solin, its meeting web server, Galene and `frpc`) lives in the [Solin repository](https://github.com/Solin-Software/Solin). How the production server is installed and operated: **[INSTALL.md](INSTALL.md)**.

```
 attendee browser                       hub (meet.solin.run, one VPS)                       PC running Solin
 ───────────────                ─────────────────────────────────────────────          ─────────────────────
 https://HUB/<code> ─TLS:443─► Caddy ─forward_auth─► ask (code registry,
   GET/POST + WSS (/ws)           │   ◄─ port 200xx ─ routing, frps plugin,
                                  │                    TURN credentials)
                                  │                      ▲ admin API
                                  └─► 127.0.0.1:200xx ── frps ◄═══ TLS tunnel :7000 ═══ frpc ─► meeting web server
                                                                   (Solin dials out,        (plain HTTP:
                                                                    one per open meeting)   join, lobby, /ws → Galene)
 media (UDP) ─────────────────► coturn :3478 UDP/TCP, relay ports ◄───────────────────────
   (only when direct fails)       time-limited credentials minted by ask (POST /api/turn)
```

## Services

| Service | Image | Role |
|---|---|---|
| `caddy` | `caddy:2.11.7-alpine` | HTTPS for `HUB_DOMAIN` (`meet.solin.run`): one certificate, every meeting under it. Routes `/<code>/...` to that meeting's tunnel and everything else to `ask`. |
| `frps` | `fatedier/frps:v0.71.1` | Tunnel server. For each open meeting, Solin's `frpc` dials out and publishes **one raw TCP proxy**, named after the meeting code: its meeting web server. |
| `ask` | built from `ask/` (Python standard library, `python:3.13.16-alpine3.24`) | The code registry (`codes.json`), code → tunnel-port routing, the frps server plugin, TURN credentials, the landing and waiting pages, and the status endpoints. |
| `coturn` | `coturn/coturn:4.18.0` | Dual-stack TURN on UDP and TCP 3478, `use-auth-secret`. Never relays to private or loopback peers. |

All four use host networking: coturn needs a large UDP port range, and everything internal binds to `127.0.0.1`.

## How it works

**Routing a meeting.** Each meeting's tunnel is a `type = "tcp"` frp proxy on a port of `127.0.0.1` that the hub picks in `TUNNEL_PORT_MIN`–`TUNNEL_PORT_MAX` (default 20000–20999). For every request to `/<code>/...`, Caddy's `forward_auth` asks `ask` (`/internal/route`) for that code's port, which `ask` reads from the frps admin API, and reverse-proxies to `127.0.0.1:<port>`. A visitor to `https://meet.solin.run/<code>` gets:
- the meeting, while its tunnel is open (Solin has the meeting open);
- the waiting page, with the meeting's name and a countdown to its start, while the tunnel is closed (503, `Retry-After`); it reloads until the meeting opens;
- "no longer valid" (410) when the code's owner has made it inactive;
- the landing page with "no such meeting" (404) for an unknown code;
- "too many attempts" (429) when the visitor's address has been probing for codes (see below).

**Meeting codes.** When Solin needs a code, it draws a random secret, keeps it, and sends only its sha256: `POST /api/codes {"owner": sha256(secret), "startsAt", "title", "kind"}` → `{"code"}`. Codes are 6 characters from `2-9` and consonants (no words, nothing easy to misread). With the secret, Solin keeps each code up to date through `POST /api/codes/sync {"codes": [{code, secret, startsAt, active, title, kind}]}` → `{"rejected": [...]}`. An inactive code's link says "no longer valid"; the owner may re-activate it. Codes nobody syncs for 400 days are forgotten and may be issued again. The registry is `codes.json` in the `ask-data` Docker volume, the hub's only state besides `hub.env` and the tunnel certificate.

**Tunnels.** Solin fetches `GET /api/hub` (`{"frpPort", "frpToken"}`), writes `client/frpc.toml.template` with the values filled in, and supervises `frpc` while the meeting is open. The frpc sends the code and its secret in `metadatas`, and the `ask` service, as an frps server plugin for `NewProxy`, decides whether to accept the proxy.

**TURN.** Solin asks for credentials per meeting, `POST /api/turn {code, secret}` → `{"urls", "username", "credential", "ttl"}`, and hands them to Galene unchanged. They are TURN REST credentials: `username = <expiry>:<code>`, `credential = base64(HMAC-SHA1(TURN_SECRET, username))`, valid for 12 h. The URLs are `turn:<HUB_DOMAIN>:3478?transport=udp` and `?transport=tcp`.

### Public endpoints

- `GET /`: landing page; visitors type a meeting code (`/go?code=` does the same without JavaScript).
- `GET /<code>`: the meeting, or one of the pages above.
- `GET /status`: number of meetings open (no codes). `GET /healthz`: `ok`.
- `GET /api/hub`, `POST /api/codes`, `POST /api/codes/sync`, `POST /api/turn`: the Solin API, documented at the top of `ask/ask.py`.

The meeting web server receives plain HTTP from `127.0.0.1`, with the path unchanged (`/<code>/...`), `Host: <HUB_DOMAIN>`, `X-Forwarded-Proto: https`, `X-Forwarded-Host` and `X-Forwarded-For: <attendee IP>`.

## Security model

- **Codes are owned by a secret.** The hub stores only `sha256(secret)` for each code and compares it in constant time. Whoever holds the secret owns the code; the hub has no other notion of identity.
- **A tunnel needs its code's secret.** `FRP_TOKEN` only lets frpc log in to frps, and the hub hands it to every Solin, so it is effectively public. The plugin accepts a proxy only when `metadatas.secret` matches the code's owner, the proxy is named exactly after the code, and it is `type = "tcp"`. It then **rewrites `remote_port`** itself, so a client cannot pick its port. frps allows one port per client (`maxPortsPerClient = 1`) and rejects a second proxy with the same name. The plugin fails closed on errors.
- **Tunnel ports are on loopback.** frps binds proxies to `127.0.0.1` (`proxyBindAddr`), within `allowPorts`. Only Caddy reaches them. The `ask` service, the frps admin API and Caddy's admin API also listen on loopback only, and Caddy answers `/internal/*` with 404. Caddy strips any client-supplied `X-Solin-Tunnel-Port`, so the routing header cannot be spoofed.
- **The tunnel is TLS only, with the hub's own certificate.** frps sets `transport.tls.force = true` with a self-signed certificate made by `scripts/tunnel_cert.sh` (`tls/tunnel.crt`, `tls/tunnel.key`, kept only on the server). Solin ships `tunnel.crt` and trusts no other, so it hands its tunnel token and meeting secrets to this hub and nothing else. Plain connections are refused.
- **Rate limits.** New codes are limited to 120 an hour per client address, where an IPv6 /64 counts as one address; a global limit of 3,000 new codes an hour and a registry cap of 200,000 codes bound what many addresses can do together. An address that asks for more than 20 unknown codes in 15 minutes is treated as probing: every code that is not an open meeting then answers "too many attempts", so meeting names and start times cannot be harvested. Open meetings still route.
- **TURN credentials stay scoped.** `TURN_SECRET` never leaves the hub. Only the owner of an active code gets credentials, and they expire after 12 h.
- **TURN never relays into private networks.** coturn denies peers in private, loopback, link-local, CGNAT, documentation and other special ranges, for IPv4 and IPv6 (`denied-peer-ip` in `turnserver.conf`). Solin and attendees are reached on their public addresses, so they are unaffected. On 1:1 NAT clouds the hub's own private address is re-allowed, for relay-to-relay between two allocations. `relay-ip` is set from `PUBLIC_IP` / `PRIVATE_IP` / `PUBLIC_IP6`, so coturn never relays from Docker bridge addresses.

## Design notes

Why raw TCP proxies plus a per-request port lookup, instead of frp's HTTP vhost:
- In the S5 spike (Solin's hosted-meetings research), frp's HTTP vhost proxy closed long-lived WebSockets after about 60 s. Raw TCP takes frp out of the HTTP layer; Caddy handles HTTP and WebSockets and has no timeout on proxied streams. `stream_close_delay 8h` keeps meetings connected through a `caddy reload`.
- `tcpmux` is not an option: Caddy cannot speak frp's HTTP CONNECT multiplexer to a plain-HTTP upstream.
- The lookup costs one loopback call per HTTP request (a WebSocket counts as one), and it lets the hub answer for every meeting that is not open instead of refusing the connection. S5 showed that a refused port is what makes attendee clients give up.

Other lessons from S5:
- `transport.heartbeatTimeout` on frps with `tcpMux` dropped every tunnel every timeout period, because frpc sends no app heartbeats when tcpMux is on. It is **not set** in `frps.toml`; do not add it.
- When a meeting's tunnel is down for more than about 25 s, clients exhaust their resume budget. The hub answers 503 instead of refusing connections, and the attendee page keeps its own rejoin loop.

## Repository layout

```
docker-compose.yml        the four services (host networking, log rotation)
Caddyfile                 production global options (ACME e-mail, timeouts) + import
sites.caddy               routing shared by production and local: /<code> → its tunnel, the rest → ask
frps.toml                 tunnel server (templated from hub.env by frps itself)
turnserver.conf           coturn template; coturn-entrypoint.sh renders it from hub.env
ask/                      control service (ask.py, landing.html, Dockerfile)
hub.env.example           all settings, documented
client/frpc.toml.template what Solin writes for each open meeting
local/                    local development variant (hub.localhost, Caddy's internal CA)
scripts/tunnel_cert.sh    makes the tunnel certificate (tls/tunnel.crt + tunnel.key)
scripts/smoke_local.sh    end-to-end test of the local variant
scripts/hubtest.py        standard-library test client: dummy meeting, HTTPS, WebSocket soak, TURN
scripts/apply.py          (on the server) validate, plan and apply the bundle; refuses cutting live meetings
scripts/deploy.sh         ship committed files to the server over SSH and run apply.py there
tests/                    unit tests (ask service, apply plan)
.github/workflows/        CI (lint, tests, config validation, smoke test) and Deploy (main → server)
INSTALL.md                operator runbook for meet.solin.run
```

## Developing and testing locally

```sh
python -m unittest discover -s tests       # unit tests, standard library only
scripts/smoke_local.sh                     # end-to-end test of a complete local hub (needs docker, ~2 min)
WS_IDLE=10 scripts/smoke_local.sh          # quicker: 10 s WebSocket idle instead of 95 s
KEEP=1 scripts/smoke_local.sh              # leave the local hub running afterwards
```

The smoke test brings up the local variant (`local/hub.env.local`: `HUB_DOMAIN=hub.localhost`, Caddy's internal CA, HTTPS on 8443 and HTTP on 8880), makes a throwaway tunnel certificate, and starts dummy meetings (`scripts/hubtest.py meeting`: HTTP + WebSocket echo, like Solin's meeting web server) behind the official `fatedier/frpc:v0.71.1` image, configured from `client/frpc.toml.template` as Solin would. It checks:
- code creation and ownership: the owner can update and retire a code, nobody else can;
- HTTPS GET and POST to `/<code>/...` through Caddy → frps → frpc → meeting, and that internal endpoints are not reachable from outside;
- a WebSocket stays open through 95 s of silence;
- hijack attempts fail: a wrong secret, and a proxy named after another code;
- the tunnel is TLS with the hub's own certificate: frpc trusting another certificate, or without TLS, does not get in;
- the waiting page, "no longer valid" and "no such meeting" pages;
- TURN: the hub's credentials work on UDP, TCP and IPv6; expired and forged ones are refused, and so is relaying to private or loopback peers;
- a meeting reconnects after its frpc restarts.

The required free ports are listed at the top of `scripts/smoke_local.sh`. CI runs the unit tests and the smoke test on every pull request, plus ruff, shellcheck, actionlint and a validation of the production configuration with example values.

To run the local hub by hand:
```sh
docker compose --env-file local/hub.env.local up -d --build
docker compose --env-file local/hub.env.local down -v
```
The local hub needs a tunnel certificate in `./tls` (`scripts/tunnel_cert.sh tls 127.0.0.1`), or `TUNNEL_TLS_DIR` pointing at one. `*.localhost` resolves to 127.0.0.1 in curl and browsers; Caddy's root certificate is in the `caddy-data` volume (`/data/caddy/pki/authorities/local/root.crt`). A development build of Solin reaches it through `SOLIN_MEETING_SERVER` (e.g. `127.0.0.1`), and checks the local tunnel certificate when `SOLIN_MEETING_SERVER_CERT` names that `tunnel.crt`; without it, the tunnel is encrypted but not verified. For `meet.solin.run`, Solin always uses its built-in copy of the production certificate.

`scripts/hubtest.py` also works against the production hub, for example `python3 scripts/hubtest.py http https://meet.solin.run/<code>/`; see INSTALL.md for the TURN checks.

The Solin side must stay compatible with this repository:
- the API (`/api/hub`, `/api/codes`, `/api/codes/sync`, `/api/turn`; see `ask/ask.py`);
- the frpc configuration (`client/frpc.toml.template`, written by Solin's `tunnel.py`) and the tunnel certificate Solin ships;
- the frp version, which must match on both sides.

## Left for later

- **`turns:` on TCP/TLS 5349:** for attendees on networks that only allow TLS. Use Caddy's certificate for `HUB_DOMAIN`, mounted read-only into coturn, plus a reload hook on renewal.
- **Metrics:** coturn and Caddy both have Prometheus endpoints, which are off. frps' dashboard is on 127.0.0.1:7500 (use an SSH tunnel).
- **Still to measure on the real server:** relay capacity, a 2.5 h soak through Caddy and the TCP tunnel, and tunnel-cut recovery.

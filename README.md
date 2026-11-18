# Solin meeting hub

The hub is a small VPS that gives every congregation running Solin hosted meetings two things:
- public HTTPS links for its meetings, `https://<HUB_DOMAIN>/<code>`, that work even when the Kingdom Hall's network is behind CGNAT;
- a TURN relay for attendees whose network blocks direct media.

The hub keeps only the **meeting-code registry**: which codes exist, who owns each one (the sha256 of a secret that stays in Solin), when each meeting starts and whether its code is still valid. Lobby, passwords, tokens, attendance and media rules all live on the PC running Solin. The hub is **open to every Solin**: there is no registration, no account and nothing to paste. One hub serves any number of congregations, all under one address.

The Solin side (Solin, its meeting web server, Galene and `frpc`) lives in the [Solin repository](https://github.com/Solin-Software/Solin). To install a hub, see **[INSTALL.md](INSTALL.md)**.

```
 attendee browser                       hub (this bundle, one VPS)                          PC running Solin
 ───────────────                ─────────────────────────────────────────────          ─────────────────────
 https://HUB/<code> ─TLS:443─► Caddy ─forward_auth─► ask (code registry,
   GET/POST + WSS (/ws)           │   ◄─ port 200xx ─ routing, frps plugin,
                                  │                    TURN credentials)
                                  │                      ▲ admin API
                                  └─► 127.0.0.1:200xx ── frps ◄═══ tunnel :7000 ═══ frpc ─► meeting web server
                                                                   (Solin dials out,        (aiohttp, plain HTTP:
                                                                    one per open meeting)   join, lobby, /ws → Galene)
 media (UDP) ─────────────────► coturn :3478 UDP/TCP, relay ports ◄───────────────────────
   (only when direct fails)       time-limited credentials minted by ask (POST /api/turn)
```

| Service | Image | Role |
|---|---|---|
| `caddy` | `caddy:2.11.7-alpine` | HTTPS for `HUB_DOMAIN`: one certificate, every meeting under it. Routes `/<code>/...` to that meeting's tunnel and everything else to `ask`. |
| `frps` | `fatedier/frps:v0.71.1` | Tunnel server. For each open meeting, Solin's `frpc` dials out and publishes **one raw TCP proxy**, named after the meeting code: its meeting web server. |
| `ask` | built from `ask/` (Python stdlib, `python:3.13.16-alpine3.24`) | The code registry (`codes.json`), code → tunnel-port routing, the frps server plugin, TURN credentials, the landing page and the status endpoints. |
| `coturn` | `coturn/coturn:4.18.0` | Dual-stack TURN on UDP and TCP 3478, `use-auth-secret`. Never relays to private or loopback peers. |

All four use host networking. coturn needs a large UDP port range, and everything internal binds to `127.0.0.1`.

## Design choices

**How Caddy reaches a meeting: raw TCP proxies, plus a per-request port lookup.**
- Each meeting's proxy is `type = "tcp"`. The hub picks a free port for it in `TUNNEL_PORT_MIN`–`TUNNEL_PORT_MAX` (default 20000–20999) on `127.0.0.1`, so it is not reachable from outside.
- For every request to `HUB_DOMAIN/<code>/...`, Caddy's `forward_auth` asks the ask service (`/internal/route`) which port that code's tunnel is on, and gets it in the header `X-Solin-Tunnel-Port`. It then reverse-proxies to `127.0.0.1:<port>`. The ask service learns which code is on which port from the frps admin API (`remotePort`). This adds a loopback call of under a millisecond per HTTP request. A WebSocket counts as one request.

Why not frp's HTTP vhost (`type = "http"`, `vhostHTTPPort`)?
- In the S5 spike, frp's HTTP vhost proxy closed long-lived WebSockets after about 60 s.
- Raw TCP takes frp out of the HTTP layer entirely. Caddy does all the HTTP and WebSocket handling, and it has no timeout on proxied streams.
- `stream_close_delay 8h` also keeps meetings connected through a `caddy reload`.

Why not `tcpmux`? Caddy cannot speak frp's HTTP CONNECT multiplexer to a plain-HTTP upstream.

What the lookup buys: the hub answers for every meeting that is not open, instead of a refused connection. S5 showed that a refused port is what makes attendee clients give up. A visitor to `HUB_DOMAIN/<code>` gets:
- the meeting, while its tunnel is open (Solin has the meeting open);
- the hub's waiting page, with a countdown to the start, while the tunnel is closed (503, `Retry-After`); it reloads until the meeting opens;
- "no longer valid" (410) when the code's owner has made it inactive;
- the landing page with "no such meeting" (404) for an unknown code.

**Meeting codes: created by the hub, owned by a secret.**
- When Solin needs a new meeting code, it draws a random secret, keeps it, and sends only its sha256: `POST /api/codes {"owner": sha256(secret), "startsAt"}` → `{"code"}`. Codes are 6 characters from `2-9` and consonants (no words, nothing easy to misread).
- From then on the secret proves ownership of that code. With it, Solin keeps the hub up to date through `POST /api/codes/sync {"codes": [{code, secret, startsAt, active}]}` → `{"rejected": [...]}`: each code's start time (the waiting page's countdown) and whether it is active. An inactive code's link says "no longer valid"; the owner may re-activate it. Codes that are unknown or whose secret is wrong come back in `rejected`.
- Codes nobody syncs for 400 days are forgotten, and may be issued again.
- The registry is `codes.json` in the `ask-data` Docker volume. It is the hub's only state.

**Tunnel authentication: public frps token + per-code secret (frps server plugin).**
- `FRP_TOKEN` only lets `frpc` log in to frps. It is effectively public: the hub hands it to every Solin (`GET /api/hub` → `{"frpPort", "frpToken"}`).
- Solin opens one frpc tunnel per open meeting, and sends the code and its secret in frpc `metadatas` (`metadatas.code`, `metadatas.secret`). The ask service implements frps' HTTP plugin protocol for `NewProxy` only:
  - the secret must match the code's owner;
  - the proxy must be named exactly after the code and be `type = "tcp"`;
  - the hub then **rewrites `remote_port`** to a free tunnel port it picks.
- So nobody can publish a meeting under a code they do not own, nor pick their own port. frps also rejects a second proxy with the same name.

**Openness and abuse.** Anyone running Solin can use the hub; the software is only useful for congregation meetings, so this is deliberate. New codes are rate-limited per client address (120 an hour; a congregation needs about ten a month). A code's owner is whoever holds its secret.

**Certificates.** One, for `HUB_DOMAIN`, obtained and renewed by Caddy as usual. DNS needs only `HUB_DOMAIN` A/AAAA records, no wildcard.

**TURN.**
- coturn uses `use-auth-secret` with `TURN_SECRET`, which **never leaves the hub**. Solin asks for credentials per meeting, `POST /api/turn {code, secret}` → `{"urls", "username", "credential", "ttl"}`, and hands them to Galene as they are. They are TURN REST credentials minted by the ask service: `username = <expiry>:<code>`, `credential = base64(HMAC-SHA1(TURN_SECRET, username))`, valid for 12 h. Only an active code's owner gets them.
- Relaying to private, loopback, link-local, CGNAT and documentation ranges (IPv4 and IPv6) is denied. Solin and attendees are reached on their public, server-reflexive addresses, so they are unaffected. On 1:1 NAT clouds, the hub's own private address is re-allowed, for relay-to-relay between two allocations.
- `relay-ip` is set explicitly from `PUBLIC_IP` / `PRIVATE_IP` / `PUBLIC_IP6`. Otherwise coturn would also relay from Docker bridge addresses that nobody can reach.

**Lessons from the S5 spike (Solin's hosted-meetings research), applied here:**
1. frp's HTTP vhost proxy closed long-lived WebSockets after about 60 s. This bundle uses `type = "tcp"` proxies only, and the smoke test proves a WebSocket survives 95 s of total silence.
2. `transport.heartbeatTimeout = 90` on frps with `tcpMux` dropped **every tunnel every 90 s**, because frpc sends no app heartbeats when tcpMux is on. It is **not set** in `frps.toml`; do not add it.
3. When a meeting's tunnel is down for more than about 25 s, clients exhaust their resume budget. The hub now answers 503 instead of refusing connections, and the attendee page must keep its own rejoin loop.

## Installing

See **[INSTALL.md](INSTALL.md)**. It covers choosing and sizing a VPS, DNS, the firewall, `hub.env`, the first deployment, how Solin uses the hub, and continuous deployment from GitHub Actions.

## Without a domain: sslip.io

sslip.io resolves `<anything>.<ip-with-dashes>.sslip.io` to that IP. For a VPS at `203.0.113.10`:
```
HUB_DOMAIN=203-0-113-10.sslip.io
```
- Meetings become `https://203-0-113-10.sslip.io/<code>`.
- Let's Encrypt issues real certificates over the HTTP challenge on port 80. No DNS setup is needed.
- Good for tests and small deployments.
- Caveats:
  - The addresses change if the IP changes.
  - Every sslip.io user may share Let's Encrypt's per-domain rate limits, so issuance can occasionally fail and be retried. Caddy retries on its own, and by default also tries ZeroSSL as a second issuer.
  - Use a real domain for the long-term production hub.
- IPv6 works too: `2001-db8--1.sslip.io`, though Solin and attendees mostly use IPv4.

## How Solin uses the hub

There is no registration step. In Solin, **Settings → Integrations → Hosted meetings → Meeting server** defaults to `meet.solin.run`. A congregation running its own hub types its address (`HUB_DOMAIN`) there instead; left empty, meetings work on the local network only.

For each meeting, Solin:
1. creates the code once, `POST https://HUB_DOMAIN/api/codes`, and keeps the secret;
2. syncs its codes' start times and active/inactive state with `POST /api/codes/sync` whenever they change;
3. while the meeting is open, fetches `GET /api/hub` (frps port and token), writes `client/frpc.toml.template` with the values filled in, and supervises `frpc -c frpc.toml`;
4. asks for TURN credentials with `POST /api/turn` and gives them to Galene.

| Variable | Value |
|---|---|
| `${HUB_ADDR}` | `HUB_DOMAIN` (or the hub's IP) |
| `${HUB_FRP_PORT}` | `frpPort` from `GET /api/hub` (`FRP_BIND_PORT`, 7000) |
| `${FRP_TOKEN}` | `frpToken` from `GET /api/hub` (`FRP_TOKEN`) |
| `${CODE}` / `${SECRET}` | the meeting code and its secret |
| `${LOCAL_PORT}` | the meeting web server's local port |

Rules the hub enforces:
- exactly one proxy per tunnel, named `${CODE}`, `type = "tcp"`;
- `metadatas.code` / `metadatas.secret` matching the code's owner;
- no frpc `user` (frp would prefix the proxy name with it);
- `remotePort` is overwritten by the hub.

`loginFailExit = false` keeps frpc retrying while the hub is unreachable.

Once frpc logs `start proxy success`, the meeting is live at `https://${HUB_DOMAIN}/<code>`. The meeting web server receives plain HTTP from `127.0.0.1`, with the path unchanged (`/<code>/...`) and these headers:
- `Host: <HUB_DOMAIN>`
- `X-Forwarded-Proto: https`
- `X-Forwarded-Host`
- `X-Forwarded-For: <attendee IP>`

It should build public URLs from `X-Forwarded-Proto` and `Host`.

TURN for Galene's `ice-servers.json`: the `urls`, `username` and `credential` from `POST /api/turn`, unchanged:
- urls: `turn:<HUB_DOMAIN>:3478?transport=udp` and `turn:<HUB_DOMAIN>:3478?transport=tcp`
- username `<expiry>:<code>`, valid 12 h

`turns:` on 5349 is not enabled yet; see "Left for later".

Public endpoints on the hub domain:
- `GET https://HUB_DOMAIN/`: landing page: visitors type a meeting code and go to `https://HUB_DOMAIN/<code>`
  (`/go?code=` does the same without JavaScript).
- `GET https://HUB_DOMAIN/<code>`: the meeting, or the waiting / "no longer valid" / "no such meeting" page (see "Design choices").
- `GET https://HUB_DOMAIN/status`: status page (number of meetings open, no codes).
- `GET https://HUB_DOMAIN/healthz`: `ok`.
- `GET /api/hub`, `POST /api/codes`, `POST /api/codes/sync`, `POST /api/turn`: the Solin API described above.

## Local development variant (`local/`)

Everything runs on one machine:
- `HUB_DOMAIN=hub.localhost`;
- Caddy's internal CA (`local_certs`);
- HTTPS on 8443 and HTTP on 8880;
- the same open API as production: create a code, then open a tunnel for it.

```sh
docker compose --env-file local/hub.env.local up -d --build
docker compose --env-file local/hub.env.local exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > /tmp/hub-ca.crt
SECRET=$(openssl rand -hex 32)
curl --cacert /tmp/hub-ca.crt -X POST https://hub.localhost:8443/api/codes \
  -d "{\"owner\": \"$(printf %s "$SECRET" | sha256sum | cut -d' ' -f1)\", \"startsAt\": \"\"}"   # -> {"code": "..."}
# point an frpc at 127.0.0.1:7000 (template above, CODE=<that code>, SECRET=$SECRET, FRP_TOKEN=local-frp-token), then:
curl --cacert /tmp/hub-ca.crt https://hub.localhost:8443/<code>/
docker compose --env-file local/hub.env.local down -v
```
`*.localhost` resolves to 127.0.0.1 in curl and browsers. Browsers need `/tmp/hub-ca.crt` trusted, or a click through the warning.

### Smoke test

```sh
scripts/smoke_local.sh               # ~2 min; WS_IDLE=10 for a quick run, KEEP=1 to leave it up
```
It brings up the local variant and starts dummy meetings (`scripts/hubtest.py hall`: HTTP + WebSocket echo, like Solin's meeting web server) behind the official `fatedier/frpc:v0.71.1` image. It checks:
- code creation and ownership: the hub creates codes owned by a secret; the owner can update and retire them, nobody else can;
- routing: HTTPS GET and POST to `/<code>/...` through Caddy → frps → frpc → meeting, with `X-Forwarded-*` headers;
- internal endpoints are not reachable from outside;
- a WebSocket stays open through **95 s of silence**;
- hijack attempts fail: a wrong secret, and a proxy named after another code;
- the waiting page for a closed meeting, "no longer valid" for a retired code, the landing page for an unknown one;
- a meeting reconnects after its frpc restarts;
- TURN: credentials issued by the hub for a code are accepted by coturn; allocation over UDP, TCP and IPv6, refusal of expired and forged credentials, and refusal to relay to private or loopback peers;
- then `docker compose down -v`.

`scripts/hubtest.py` also works against a real hub:
```sh
python3 scripts/hubtest.py http https://$HUB_DOMAIN/<code>/
python3 scripts/hubtest.py ws wss://$HUB_DOMAIN/<code>/ws --idle 300   # needs a WS echo at /<code>/ws
python3 scripts/hubtest.py turn $PUBLIC_IP --secret "$TURN_SECRET" --peer 8.8.8.8 --denied-peer 10.0.0.1
python3 scripts/hubtest.py turn $PUBLIC_IP --username '<from POST /api/turn>' --password '<credential>'
python3 scripts/hubtest.py turn $PUBLIC_IP --tcp --secret "$TURN_SECRET"
```
Run the TURN check from **outside** the VPS.

## Troubleshooting

| Symptom | Check |
|---|---|
| frpc: `token in login doesn't match` | The frpc's token differs from the hub's `FRP_TOKEN`: it was changed on the hub after Solin fetched it from `GET /api/hub`. Close and reopen the meeting in Solin. |
| frpc: `unknown meeting code or wrong secret` | The code does not exist on this hub (forgotten, or the hub's registry was lost or replaced) or the secret is wrong. Check `docker compose logs ask` (`NewProxy rejected`). |
| frpc: `the proxy must be named ...` / `only one proxy of type 'tcp'` | The frpc.toml is not the template: the proxy name must equal the code, with no `user`. |
| frpc: `the hub has no free tunnel port right now` / `port already used` | Every port in `TUNNEL_PORT_MIN`–`TUNNEL_PORT_MAX` is taken (widen the range), or the same meeting is open twice (two Solin instances). |
| Browser: TLS error / `SSL_ERROR_INTERNAL_ERROR_ALERT` | No certificate for `HUB_DOMAIN` yet: ACME failed. Check `docker compose logs caddy` (look for `tls.obtain`). DNS must point at the VPS, and ports 80 and 443 must be reachable from the internet. |
| A meeting link shows "no such meeting" | The code was never created on this hub (Solin points at another "Meeting server"?), or it was forgotten after 400 days unsynced, or the registry (`codes.json` in `ask-data`) was lost. Check `docker compose logs ask` for `code created`. |
| A meeting link says "no longer valid" | Its owner made the code inactive in Solin (sync). The owner can re-activate it. |
| A meeting link keeps showing the waiting page | The meeting's tunnel is down: the meeting is not open in Solin, or frpc cannot connect. Check `docker compose logs frps ask`. |
| Solin: `too many new codes from this address` (429) | More than 120 new codes in an hour from one address. Wait, or check for a misbehaving client. |
| WebSockets drop at regular intervals | Make sure nobody added `transport.heartbeatTimeout` to frps.toml, and that tunnels use `type = "tcp"`. A Caddy reload keeps streams for 8 h; a Caddy restart drops them. |
| TURN does not relay | UDP 3478 and the relay range must be open in **both** the cloud security group and the host firewall. On 1:1 NAT clouds set `PRIVATE_IP`. Check `docker compose logs coturn` for `relay-ip` and `401`/`403`. Remember: peers in private ranges are refused by design. |
| Which meetings are open? | `curl -u admin:$FRP_ADMIN_PASSWORD http://127.0.0.1:7500/api/proxy/tcp` on the VPS (proxy name = code). |

## Files

```
docker-compose.yml        the four services (host networking, log rotation)
Caddyfile                 production global options (ACME e-mail, timeouts) + import
sites.caddy               routing shared by production and local: /<code> → its tunnel, the rest → ask
frps.toml                 tunnel server (templated from hub.env by frps itself)
turnserver.conf           coturn template; coturn-entrypoint.sh renders it from hub.env
ask/                      control service (ask.py, landing.html, Dockerfile): code registry, routing, frps plugin, TURN credentials, landing page
hub.env.example           all settings, documented
client/frpc.toml.template what Solin writes for each open meeting
local/                    local development variant (hub.localhost, internal CA)
scripts/smoke_local.sh    end-to-end test of the local variant
scripts/hubtest.py        stdlib test client: dummy meeting, HTTPS, WebSocket soak, TURN
scripts/apply.py          (on the VPS) validate, plan and apply the bundle; refuses cutting live meetings
scripts/deploy.sh         ship committed files to the VPS over SSH and run apply.py there
tests/                    unit tests (ask service, apply plan)
.github/workflows/        CI (lint, tests, config validation, smoke test) and Deploy (main → VPS)
INSTALL.md                VPS installation and continuous deployment
```

## Development

```sh
python3 -m unittest discover -s tests       # unit tests, standard library only
WS_IDLE=10 scripts/smoke_local.sh           # end-to-end test of a complete local hub (needs docker)
```
CI runs both on every pull request, plus ruff, shellcheck, actionlint and a validation of the production configuration with example values. Merges to `main` are deployed by the Deploy workflow once it is configured (INSTALL.md, step 8).

The Solin side must stay compatible with this repository:
- the API (`/api/hub`, `/api/codes`, `/api/codes/sync`, `/api/turn`; see `ask/ask.py`);
- the frpc configuration (`client/frpc.toml.template`, written by Solin's `tunnel.py`);
- the frp version, which must match on both sides.

## Left for later

- **`turns:` on TCP/TLS 5349:** for attendees on networks that only allow TLS. Use Caddy's certificate for `HUB_DOMAIN`, mounted read-only into coturn, plus a reload hook on renewal.
- **Metrics:** coturn and Caddy both have Prometheus endpoints, which are off. frps' dashboard is on 127.0.0.1:7500 (use an SSH tunnel).
- **Still to measure on the real VPS:** the S5 pending items (relay capacity, a 2.5 h soak through Caddy and the TCP tunnel, tunnel-cut recovery) on the production provider.

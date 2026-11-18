# Installing the Solin hub on a VPS

This guide takes a fresh VPS to a running hub, then sets up continuous deployment, so that every
change merged to `main` reaches the VPS on its own. For what the hub is and how it works, see
[README.md](README.md).

Steps:
1. [Choose a VPS](#1-choose-a-vps)
2. [DNS](#2-dns)
3. [Prepare the server](#3-prepare-the-server)
4. [Configure the hub](#4-configure-the-hub-hubenv)
5. [First deployment](#5-first-deployment)
6. [Check that it works](#6-check-that-it-works)
7. [Point Solin at the hub](#7-point-solin-at-the-hub)
8. [Continuous deployment](#8-continuous-deployment-github-actions)
9. [Day-to-day operations](#9-day-to-day-operations)

---

## 1. Choose a VPS

The hub does not carry meetings: Galene and all video encoding run on the computer running Solin at each Kingdom Hall. The hub does three things:
- gives each meeting a public HTTPS address, `https://HUB_DOMAIN/<code>`;
- tunnels the meeting page and its signalling to Solin;
- relays media (TURN) for attendees who cannot connect to Solin directly.

So **bandwidth matters more than CPU**.

| Congregations served | vCPU | RAM | Port | Traffic per month (each direction) |
|---|---|---|---|---|
| 1–5 | 2 | 2 GB | 100 Mbps+ | 0.2–1 TB |
| 5–20 | 2–4 | 4 GB | 1 Gbps | 1–4 TB |
| ~200 (one of two hubs, see below) | 4–8 | 8 GB | 1 Gbps, unmetered | 8–20 TB |

These are estimates; the hub has not been load-tested yet. Measure on a pilot before buying for many congregations. For reference, a relayed attendee at 360p uses about 0.5 Mbps. A 2-hour meeting with all 50 attendees relayed is about 22 GB in and 22 GB out. In practice many attendees connect directly, especially over IPv6, and never touch the relay.

What else to look for:
- **Location:** close to the congregations (for Brazil, São Paulo). Relayed media goes Solin → hub → attendee.
- **Public IPv4 and IPv6.** TURN must work over both.
- **Inbound UDP allowed.** Some cheap providers filter it.
- **Traffic:** an unmetered port or a large allowance. Clouds that charge per GB of outbound traffic get expensive at scale.
- **OS:** Debian 12/13 or Ubuntu 22.04/24.04, amd64 or arm64.
- **Disk:** 20–40 GB is plenty. The hub stores no recordings or meeting data.

**Many congregations:**
- Run **two hubs** on different providers, not one large machine. One hub failing on a Sunday morning should not take every congregation's meetings offline.
- Each Solin uses the hub named in its "Meeting server" setting, so congregations are split simply by which address they type there. Meeting codes belong to the hub that created them.
- Size each hub so it can carry the realistic peak alone.

## 2. DNS

**With a domain (production).** Create these records, pointing to the VPS. No wildcard record is needed:

| Name | Type | Value |
|---|---|---|
| `hub.example.org` | A | the VPS IPv4 |
| `hub.example.org` | AAAA | the VPS IPv6 |

`hub.example.org` becomes `HUB_DOMAIN`. Meetings are served at `https://hub.example.org/<code>`. One certificate, for that name, comes from Let's Encrypt automatically.

**Without a domain (tests, small setups).** Use sslip.io: `HUB_DOMAIN=203-0-113-10.sslip.io` for a VPS at `203.0.113.10`. No DNS setup is needed. The addresses change if the IP changes; see the README for the caveats.

## 3. Prepare the server

Log in as root, or a sudo user, on the fresh VPS.

```sh
# Updates, tools and automatic security updates
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get install -y rsync python3 curl ufw unattended-upgrades

# Docker Engine + compose plugin (official convenience script; or follow docs.docker.com/engine/install)
curl -fsSL https://get.docker.com | sudo sh

# Deploy user: owns the bundle directory and runs docker. Membership of the docker group is
# root-equivalent, so this account's SSH key must be guarded like a root key.
sudo adduser --disabled-password --gecos "Solin hub" solin-hub
sudo usermod -aG docker solin-hub
# 755: deployments set this mode on the directory anyway (hub.env stays 600)
sudo install -d -o solin-hub -g solin-hub -m 755 /opt/solin-hub
```

**Firewall.** Open the same ports in the provider's cloud firewall or security group, if it has one:

```sh
sudo ufw default deny incoming
sudo ufw allow 22/tcp                         # SSH (or your custom port)
sudo ufw allow 80/tcp                         # ACME challenge + redirect to HTTPS
sudo ufw allow 443/tcp                        # HTTPS: landing page, API and meetings
sudo ufw allow 443/udp                        # HTTP/3 (optional)
sudo ufw allow 7000/tcp                       # tunnels from Solin (FRP_BIND_PORT)
sudo ufw allow 3478                           # TURN, UDP and TCP (TURN_PORT)
sudo ufw allow 49160:49999/udp                # TURN relay ports (TURN_MIN_PORT-TURN_MAX_PORT)
sudo ufw enable
```

Nothing else needs to be open. The ask service (7400), the frps admin API (7500), the Caddy admin API (2019) and the meeting tunnel ports (`TUNNEL_PORT_MIN`–`TUNNEL_PORT_MAX`, 20000–20999) listen on 127.0.0.1 only.

Oracle Cloud's Ubuntu images also ship iptables REJECT rules. Add ACCEPTs before them:
```sh
sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443,7000,3478 -j ACCEPT
sudo iptables -I INPUT 6 -p udp -m multiport --dports 443,3478,49160:49999 -j ACCEPT
sudo netfilter-persistent save
```

**SSH access for deployments.** Generate the key on your own computer, never on the server:
```sh
ssh-keygen -t ed25519 -N "" -C "solin-hub deploy" -f ./hub_deploy
```
Then install the public key for the deploy user on the VPS:
```sh
sudo install -d -o solin-hub -g solin-hub -m 700 /home/solin-hub/.ssh
echo "<contents of hub_deploy.pub>" | sudo tee /home/solin-hub/.ssh/authorized_keys
sudo chown solin-hub:solin-hub /home/solin-hub/.ssh/authorized_keys
sudo chmod 600 /home/solin-hub/.ssh/authorized_keys
```
Keep the private key `hub_deploy`. It goes into GitHub in step 8, or you use it yourself with `scripts/deploy.sh`.

## 4. Configure the hub (`hub.env`)

`hub.env` exists **only on the VPS**. It is not in git, and deployments never overwrite it. The hub's only other state, the meeting-code registry, lives in the `ask-data` Docker volume.

As the deploy user (`sudo -iu solin-hub`):

```sh
cd /opt/solin-hub
curl -fsSL https://raw.githubusercontent.com/Solin-Software/solin-hub/main/hub.env.example -o hub.env
chmod 600 hub.env
openssl rand -hex 32   # run three times: FRP_TOKEN, FRP_ADMIN_PASSWORD, TURN_SECRET
nano hub.env
```

Set at least the following:

| Setting | Value |
|---|---|
| `HUB_DOMAIN` | the domain from step 2 |
| `ACME_EMAIL` | where Let's Encrypt sends expiry notices |
| `PUBLIC_IP` | the VPS public IPv4 |
| `PRIVATE_IP` | **only** on clouds with 1:1 NAT (Google Cloud, AWS, Oracle): the address shown by `ip -4 addr`. Leave it empty on ordinary VPSs. |
| `PUBLIC_IP6` | the VPS IPv6, from `ip -6 addr show scope global`. Recommended. |
| `FRP_TOKEN`, `FRP_ADMIN_PASSWORD`, `TURN_SECRET` | the three fresh secrets |

Every setting is documented inside the file.

The deploy refuses to start a hub that still has an example domain or placeholder secrets, or secrets shorter than 24 characters.

**Treat `hub.env` like a password file.** `TURN_SECRET` never leaves the hub: it mints the time-limited TURN credentials Solin asks for, and changing it only invalidates credentials already handed out. `FRP_TOKEN` is handed to every Solin (`GET /api/hub`) and only lets frpc log in; each meeting's tunnel is protected by its code's secret. Solin fetches it from the hub, so changing it means nothing to redistribute (only an frps restart, see step 8). `FRP_ADMIN_PASSWORD` must stay private.

## 5. First deployment

Choose one way to ship the code. All three ship the same committed files and run the same `scripts/apply.py` on the VPS.

**a) GitHub Actions (recommended).** Finish step 8, then run the **Deploy** workflow (Actions → Deploy → Run workflow).

**b) From your computer:**
```sh
git clone https://github.com/Solin-Software/solin-hub && cd solin-hub
ssh-keyscan -t ed25519 <vps> > hub_known_hosts   # compare with the fingerprint in the provider's console
HUB_HOST=<vps> HUB_SSH_KEY_FILE=./hub_deploy HUB_KNOWN_HOSTS=./hub_known_hosts scripts/deploy.sh
```

**c) Entirely on the VPS** (no deploy pipeline; as `solin-hub`):
```sh
git clone https://github.com/Solin-Software/solin-hub /tmp/solin-hub-src
rsync -a --exclude .git /tmp/solin-hub-src/ /opt/solin-hub/ && rm -rf /tmp/solin-hub-src
cd /opt/solin-hub && python3 scripts/apply.py
```

What `apply.py` does:
1. Checks `hub.env`.
2. Validates the compose file, the Caddyfile and `frps.toml` with the pinned images.
3. Builds the `ask` image and starts the four services.
4. Waits until the services are healthy.

Its exit codes:

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | Failed (it prints the recent logs) |
| 2 | Configuration error |
| 3 | Refused because meetings are open; see step 8 |

`python3 scripts/apply.py --dry-run` validates and shows the plan without changing anything.

**Do not keep other files in `/opt/solin-hub`.** Each deployment replaces the directory's contents with the repository's files. Only `hub.env` and `.deployed/` are kept (the code registry is in a Docker volume, not in the directory).

## 6. Check that it works

On the VPS:
```sh
cd /opt/solin-hub
docker compose ps                         # four services, all "running"
docker compose logs --tail 50 caddy       # certificate for HUB_DOMAIN obtained
curl -s https://<HUB_DOMAIN>/healthz      # ok
curl -s https://<HUB_DOMAIN>/status       # "The hub is running. Meetings open: 0."
curl -s https://<HUB_DOMAIN>/api/hub      # {"frpPort": 7000, "frpToken": "..."}
```

TURN has to be checked **from another machine**, outside the VPS:
```sh
python3 scripts/hubtest.py turn <PUBLIC_IP> --secret '<TURN_SECRET>' --peer 8.8.8.8 --denied-peer 10.0.0.1
python3 scripts/hubtest.py turn <PUBLIC_IP> --tcp --secret '<TURN_SECRET>'
```
To check the credentials the hub hands to Solin, create a code, ask for TURN credentials for it, and pass them with `--username` / `--password` instead of `--secret`:
```sh
SECRET=$(openssl rand -hex 32)
OWNER=$(printf %s "$SECRET" | sha256sum | cut -d' ' -f1)
curl -s -X POST https://<HUB_DOMAIN>/api/codes -d "{\"owner\": \"$OWNER\", \"startsAt\": \"\"}"        # {"code": "..."}
curl -s -X POST https://<HUB_DOMAIN>/api/turn -d "{\"code\": \"<code>\", \"secret\": \"$SECRET\"}"     # urls, username, credential
python3 scripts/hubtest.py turn <PUBLIC_IP> --username '<username>' --password '<credential>'
```
The test code stays in the registry until it is forgotten (400 days); it is harmless.

## 7. Point Solin at the hub

There is nothing to register: the hub is open to every Solin, with no accounts and no codes to hand out.

In Solin, go to **Settings → Integrations → Hosted meetings → Meeting server**. It defaults to `meet.solin.run`; a congregation using its own hub types its address (`HUB_DOMAIN`) there. Left empty, meetings work on the local network only.

From then on Solin does everything itself:
- asks the hub for a code for each meeting (`POST /api/codes`) and keeps the code's secret, which proves it owns the code;
- keeps each code's start time and active/inactive state up to date (`POST /api/codes/sync`);
- opens a tunnel for each meeting while it is open, so it is at `https://<HUB_DOMAIN>/<code>`;
- gets TURN credentials for each meeting from the hub (`POST /api/turn`).

- **Abuse limits:** new codes are limited to 120 an hour per client address. A code's owner is whoever holds its secret; the hub has no other notion of who a congregation is.
- **Unused codes** are forgotten after 400 days without a sync. Nothing needs cleaning up by hand.

## 8. Continuous deployment (GitHub Actions)

Both workflows are in `.github/workflows/`:
- **CI** runs on pull requests and branches: lint, unit tests, configuration validation, and an end-to-end smoke test of a complete local hub.
- **Deploy** runs on every push to `main`. It runs CI first, then ships `main` to the VPS with `scripts/deploy.sh`, applies it, and checks `https://<HUB_DOMAIN>/healthz`.

**Until it is configured, the deploy job ships nothing.** It succeeds with a notice until the `HUB_HOST` variable exists.

To turn it on, go to the repository's **Settings → Environments → New environment** and create `production`:

| Kind | Name | Value |
|---|---|---|
| Variable | `HUB_HOST` | the VPS address (IP or host name) |
| Variable | `HUB_DOMAIN` | optional; enables the public health check and the environment URL |
| Variable | `HUB_SSH_USER` | optional, default `solin-hub` |
| Variable | `HUB_SSH_PORT` | optional, default `22` |
| Variable | `HUB_DIR` | optional, default `/opt/solin-hub` |
| Secret | `HUB_SSH_KEY` | the private key `hub_deploy` from step 3 |
| Secret | `HUB_SSH_KNOWN_HOSTS` | the output of `ssh-keyscan -t ed25519 <vps>`, checked against the provider's console |

Recommended environment protection:
- **Required reviewers**, so a deploy waits for approval;
- **Deployment branches: main only.**

**Deploying while meetings are running.**
- Restarting the tunnel server (frps) or the TURN relay (coturn) cuts every meeting in progress. If a change needs either restart and any meeting is open, the deploy **stops with exit code 3** and changes nothing.
- When that happens, run the Deploy workflow again when no meeting is running. Or run it manually with **force**, accepting that meetings are cut.
- These changes never need a restart:
  - Caddyfile and `sites.caddy`: Caddy reloads in place and keeps open connections.
  - The `ask` service: it restarts in about a second.
  - Meeting codes: they are created and updated live through the API.

**Workflow inputs** (Actions → Deploy → Run workflow):
- `dry_run`: validate and show the plan on the VPS without changing anything;
- `force`: see above.

**Rolling back:** revert the commit on `main`. The next deploy ships the previous configuration. In an emergency, run the Deploy workflow from an older tag or branch ("Use workflow from").

## 9. Day-to-day operations

```sh
cd /opt/solin-hub
docker compose ps
docker compose logs -f --tail 100 ask frps        # codes created, refused tunnels, routing
cat .deployed/state.json                          # deployed commit and time
curl -s -u admin:<FRP_ADMIN_PASSWORD> http://127.0.0.1:7500/api/proxy/tcp   # open meetings (proxy name = code)
```

- **Updating images** (Caddy, frp, coturn, Python): change the pinned tags in `docker-compose.yml` or `ask/Dockerfile` in a pull request. Once it is merged, the pipeline deploys it. An frps or coturn bump counts as a restart and waits for a meeting-free moment, as described in step 8. Keep Solin's frpc version (`FRP_VERSION` in Solin's `scripts/fetch_hosted_meeting_helpers.py`) in step with frps.
- **Back up:** `hub.env`, the `ask-data` Docker volume (the code registry, `codes.json`) and the `caddy-data` Docker volume, which holds the certificates and the ACME account:
  ```sh
  tar -czf hub-backup-$(date +%F).tgz hub.env
  docker run --rm -v solin-hub_ask-data:/data -v "$PWD":/out alpine tar -czf /out/ask-data.tgz -C /data .
  docker run --rm -v solin-hub_caddy-data:/data -v "$PWD":/out alpine tar -czf /out/caddy-data.tgz -C /data .
  ```
  Store the backup off the VPS; it contains secrets.
- **Moving to a new VPS:** prepare the new server (step 3) and restore `hub.env` and the code registry (`docker compose cp ask:/data/codes.json .` on the old server; after the first deploy on the new one, `docker compose cp codes.json ask:/data/codes.json` and `docker compose restart ask`). Without the registry every existing meeting link says "no such meeting" and Solin can no longer open tunnels for those codes. Update `PUBLIC_IP` and `PRIVATE_IP`/`PUBLIC_IP6`, point DNS at the new address, then deploy. Meeting links keep working as long as `HUB_DOMAIN` stays the same.
- **Operating system updates:** `unattended-upgrades` installs security fixes. Schedule reboots outside meeting times; Docker restarts the hub on boot.
- **Troubleshooting:** see the table in [README.md](README.md#troubleshooting).

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
7. [Register halls](#7-register-halls)
8. [Continuous deployment](#8-continuous-deployment-github-actions)
9. [Day-to-day operations](#9-day-to-day-operations)

---

## 1. Choose a VPS

The hub does not carry meetings: Galene and all video encoding run on each hall's own computer. The hub does three things:
- gives each hall a public HTTPS address;
- tunnels the meeting page and its signalling to the hall;
- relays media (TURN) for attendees who cannot connect to the hall directly.

So **bandwidth matters more than CPU**.

| Halls served | vCPU | RAM | Port | Traffic per month (each direction) |
|---|---|---|---|---|
| 1–5 | 2 | 2 GB | 100 Mbps+ | 0.2–1 TB |
| 5–20 | 2–4 | 4 GB | 1 Gbps | 1–4 TB |
| ~200 (one of two hubs, see below) | 4–8 | 8 GB | 1 Gbps, unmetered | 8–20 TB |

These are estimates; the hub has not been load-tested yet. Measure on a pilot before buying for many halls. For reference, a relayed attendee at 360p uses about 0.5 Mbps. A 2-hour meeting with all 50 attendees relayed is about 22 GB in and 22 GB out. In practice many attendees connect directly, especially over IPv6, and never touch the relay.

What else to look for:
- **Location:** close to the halls (for Brazil, São Paulo). Relayed media goes hall → hub → attendee.
- **Public IPv4 and IPv6.** TURN must work over both.
- **Inbound UDP allowed.** Some cheap providers filter it.
- **Traffic:** an unmetered port or a large allowance. Clouds that charge per GB of outbound traffic get expensive at scale.
- **OS:** Debian 12/13 or Ubuntu 22.04/24.04, amd64 or arm64.
- **Disk:** 20–40 GB is plenty. The hub stores no recordings or meeting data.

**Many halls:**
- Run **two hubs** on different providers, not one large machine. One hub failing on a Sunday morning should not take every hall offline.
- Each hall's connection code names its hub, so halls are split simply by registering them on one hub or the other.
- Size each hub so it can carry the realistic peak alone.

## 2. DNS

**With a domain (production).** Create these records, all pointing to the VPS:

| Name | Type | Value |
|---|---|---|
| `hub.example.org` | A | the VPS IPv4 |
| `hub.example.org` | AAAA | the VPS IPv6 |
| `*.hub.example.org` | A | the VPS IPv4 |
| `*.hub.example.org` | AAAA | the VPS IPv6 |

`hub.example.org` becomes `HUB_DOMAIN`. Halls are served at `https://<hall>.hub.example.org/`. Certificates come from Let's Encrypt automatically.

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
# 755: the ask container runs as an unprivileged user and must read halls.json (hub.env stays 600)
sudo install -d -o solin-hub -g solin-hub -m 755 /opt/solin-hub
```

**Firewall.** Open the same ports in the provider's cloud firewall or security group, if it has one:

```sh
sudo ufw default deny incoming
sudo ufw allow 22/tcp                         # SSH (or your custom port)
sudo ufw allow 80/tcp                         # ACME challenge + redirect to HTTPS
sudo ufw allow 443/tcp                        # HTTPS: hub and halls
sudo ufw allow 443/udp                        # HTTP/3 (optional)
sudo ufw allow 7000/tcp                       # tunnels from hall PCs (FRP_BIND_PORT)
sudo ufw allow 3478                           # TURN, UDP and TCP (TURN_PORT)
sudo ufw allow 49160:49999/udp                # TURN relay ports (TURN_MIN_PORT-TURN_MAX_PORT)
sudo ufw enable
```

Nothing else needs to be open. The ask service (7400), the frps admin API (7500), the Caddy admin API (2019) and the hall tunnel ports (20000–20999) listen on 127.0.0.1 only.

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

`hub.env` and `halls.json` exist **only on the VPS**. They are not in git, and deployments never overwrite them.

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

**Treat `hub.env` like a password file.** `FRP_TOKEN` and `TURN_SECRET` go into every hall's connection code. Changing either one means re-issuing codes to all halls.

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
1. Checks `hub.env`, and creates an empty `halls.json` if there is none.
2. Validates the compose file, the Caddyfile and `frps.toml` with the pinned images.
3. Builds the `ask` image and starts the four services.
4. Waits until the services are healthy.

Its exit codes:

| Code | Meaning |
|---|---|
| 0 | Done |
| 1 | Failed (it prints the recent logs) |
| 2 | Configuration error |
| 3 | Refused because halls are connected; see step 8 |

`python3 scripts/apply.py --dry-run` validates and shows the plan without changing anything.

**Do not keep other files in `/opt/solin-hub`.** Each deployment replaces the directory's contents with the repository's files. Only `hub.env`, `halls.json` and `.deployed/` are kept.

## 6. Check that it works

On the VPS:
```sh
cd /opt/solin-hub
docker compose ps                         # four services, all "running"
docker compose logs --tail 50 caddy       # certificate for HUB_DOMAIN obtained
curl -s https://<HUB_DOMAIN>/healthz      # ok
curl -s https://<HUB_DOMAIN>/             # "The hub is running. Halls connected: 0."
```

TURN has to be checked **from another machine**, outside the VPS:
```sh
python3 scripts/hubtest.py turn <PUBLIC_IP> --secret '<TURN_SECRET>' --peer 8.8.8.8 --denied-peer 10.0.0.1
python3 scripts/hubtest.py turn <PUBLIC_IP> --tcp --secret '<TURN_SECRET>'
```

## 7. Register halls

On the VPS, as `solin-hub`:
```sh
cd /opt/solin-hub
python3 scripts/add_hall.py hall-abc123 --note "Congregation X, contact ..."
```

It prints:
- the hall's key, shown once;
- a **connection code**: `solin-hub:...`.

In Solin on the hall's computer, paste the connection code into **Settings → Integrations → Hosted meetings → Hub connection code**.

The hall is then online at `https://hall-abc123.<HUB_DOMAIN>/` whenever Solin is running. The first visit takes a few seconds while its certificate is issued.

- **Hall names** are part of the public address. Use something not easy to guess, like `hall-abc123`.
- **Rotating a key:** run the same command again. The hall keeps its port; give it the new connection code.
- **Removing a hall:** delete its entry from `halls.json`. The change is picked up live; no restart is needed.

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
- Restarting the tunnel server (frps) or the TURN relay (coturn) cuts every meeting in progress. If a change needs either restart and any hall is connected, the deploy **stops with exit code 3** and changes nothing.
- When that happens, run the Deploy workflow again when no meeting is running. Or run it manually with **force**, accepting that meetings are cut.
- These changes never need a restart:
  - Caddyfile and `sites.caddy`: Caddy reloads in place and keeps open connections.
  - The `ask` service: it restarts in about a second.
  - Hall registrations: they are picked up live.

**Workflow inputs** (Actions → Deploy → Run workflow):
- `dry_run`: validate and show the plan on the VPS without changing anything;
- `force`: see above.

**Rolling back:** revert the commit on `main`. The next deploy ships the previous configuration. In an emergency, run the Deploy workflow from an older tag or branch ("Use workflow from").

## 9. Day-to-day operations

```sh
cd /opt/solin-hub
docker compose ps
docker compose logs -f --tail 100 ask frps        # logins, refused halls, routing
cat .deployed/state.json                          # deployed commit and time
curl -s -u admin:<FRP_ADMIN_PASSWORD> http://127.0.0.1:7500/api/proxy/tcp   # connected halls
```

- **Updating images** (Caddy, frp, coturn, Python): change the pinned tags in `docker-compose.yml` or `ask/Dockerfile` in a pull request. Once it is merged, the pipeline deploys it. An frps or coturn bump counts as a restart and waits for a meeting-free moment, as described in step 8. Keep the hall side's frpc version (`FRP_VERSION` in Solin's `scripts/fetch_hosted_meeting_helpers.py`) in step with frps.
- **Back up:** `hub.env`, `halls.json` and the `caddy-data` Docker volume, which holds the certificates and the ACME account:
  ```sh
  tar -czf hub-backup-$(date +%F).tgz hub.env halls.json
  docker run --rm -v solin-hub_caddy-data:/data -v "$PWD":/out alpine tar -czf /out/caddy-data.tgz -C /data .
  ```
  Store the backup off the VPS; it contains secrets.
- **Moving to a new VPS:** prepare the new server (step 3) and restore `hub.env` and `halls.json`. Update `PUBLIC_IP` and `PRIVATE_IP`/`PUBLIC_IP6`, point DNS at the new address, then deploy. Halls keep their connection codes as long as `HUB_DOMAIN` and the secrets stay the same.
- **Operating system updates:** `unattended-upgrades` installs security fixes. Schedule reboots outside meeting times; Docker restarts the hub on boot.
- **Troubleshooting:** see the table in [README.md](README.md#troubleshooting).

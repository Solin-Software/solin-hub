# Operating meet.solin.run

This is the runbook for the one production hub, `meet.solin.run`, run by the Solin project. It covers building the server from a fresh VPS, deploying, and day-to-day operation. For what the hub is and how it works, see [README.md](README.md).

Steps:
1. [The VPS](#1-the-vps)
2. [DNS](#2-dns)
3. [Prepare the server](#3-prepare-the-server)
4. [Configure the hub (`hub.env`)](#4-configure-the-hub-hubenv)
5. [The tunnel certificate](#5-the-tunnel-certificate)
6. [Deploying](#6-deploying)
7. [Check that it works](#7-check-that-it-works)
8. [Host hardening](#8-host-hardening)
9. [Day-to-day operations](#9-day-to-day-operations)
10. [Troubleshooting](#10-troubleshooting)

---

## 1. The VPS

The hub does not carry meetings: Galene and all video encoding run on the PC running Solin. The hub gives each meeting its public address, tunnels the meeting page and its signalling to Solin, and relays media (TURN) for attendees who cannot connect to Solin directly. So **bandwidth matters more than CPU**.

For reference, a relayed attendee at 360p uses about 0.5 Mbps, and a 2-hour meeting with all 50 attendees relayed is about 22 GB in and 22 GB out. Many attendees connect directly and never touch the relay. The hub has not been load-tested yet, so watch traffic and size up from real numbers.

What the server needs:
- **Public IPv4 and IPv6.** TURN must work over both.
- **Inbound UDP allowed.** Some cheap providers filter it.
- **Traffic:** an unmetered port or a large allowance; per-GB egress pricing gets expensive.
- **Location:** close to the users. Relayed media goes Solin → hub → attendee.
- **OS:** Debian 12/13 or Ubuntu 22.04/24.04, amd64 or arm64.
- **Disk:** 20–40 GB is plenty. The hub stores no recordings or meeting data.

## 2. DNS

`meet.solin.run` needs an A record (the VPS IPv4) and an AAAA record (the VPS IPv6). No wildcard is needed: every meeting is a path under that one name, and Caddy gets its one certificate from Let's Encrypt over port 80.

## 3. Prepare the server

Log in as root, or a sudo user, on the fresh VPS.

```sh
# Updates, tools and automatic security updates
sudo apt-get update && sudo apt-get -y full-upgrade
sudo apt-get install -y rsync python3 curl openssl ufw unattended-upgrades

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
sudo ufw allow 22/tcp                         # SSH
sudo ufw allow 80/tcp                         # ACME challenge + redirect to HTTPS
sudo ufw allow 443/tcp                        # HTTPS: landing page, API and meetings
sudo ufw allow 443/udp                        # HTTP/3 (optional)
sudo ufw allow 7000/tcp                       # TLS tunnels from Solin (FRP_BIND_PORT)
sudo ufw allow 3478                           # TURN, UDP and TCP (TURN_PORT)
sudo ufw allow 49160:49999/udp                # TURN relay ports (TURN_MIN_PORT-TURN_MAX_PORT)
sudo ufw enable
```

Nothing else may be open. The `ask` service (`ASK_PORT`, 7400), the frps admin API (`FRP_ADMIN_PORT`, 7500), the Caddy admin API (2019) and the meeting tunnel ports (`TUNNEL_PORT_MIN`–`TUNNEL_PORT_MAX`, 20000–20999) listen on 127.0.0.1 only.

On clouds whose images ship their own iptables REJECT rules (Oracle Cloud's Ubuntu, for example), add ACCEPTs before them:
```sh
sudo iptables -I INPUT 6 -p tcp -m multiport --dports 80,443,7000,3478 -j ACCEPT
sudo iptables -I INPUT 6 -p udp -m multiport --dports 443,3478,49160:49999 -j ACCEPT
sudo netfilter-persistent save
```

**SSH key for deployments.** Generate it on your own computer, never on the server:
```sh
ssh-keygen -t ed25519 -N "" -C "solin-hub deploy" -f ./hub_deploy
```
Install the public key for the deploy user on the VPS:
```sh
sudo install -d -o solin-hub -g solin-hub -m 700 /home/solin-hub/.ssh
echo "<contents of hub_deploy.pub>" | sudo tee /home/solin-hub/.ssh/authorized_keys
sudo chown solin-hub:solin-hub /home/solin-hub/.ssh/authorized_keys
sudo chmod 600 /home/solin-hub/.ssh/authorized_keys
```
The private key `hub_deploy` goes into GitHub (step 6), or you use it yourself with `scripts/deploy.sh`.

## 4. Configure the hub (`hub.env`)

`hub.env` exists **only on the server**. It is not in git, and deployments never touch it.

As the deploy user (`sudo -iu solin-hub`):

```sh
cd /opt/solin-hub
curl -fsSL https://raw.githubusercontent.com/Solin-Software/solin-hub/main/hub.env.example -o hub.env
chmod 600 hub.env
openssl rand -hex 32   # run three times: FRP_TOKEN, FRP_ADMIN_PASSWORD, TURN_SECRET
nano hub.env
```

| Setting | Value |
|---|---|
| `HUB_DOMAIN` | `meet.solin.run` |
| `ACME_EMAIL` | where Let's Encrypt sends expiry notices |
| `PUBLIC_IP` | the VPS public IPv4 |
| `PRIVATE_IP` | **only** on clouds with 1:1 NAT (Google Cloud, AWS, Oracle): the address shown by `ip -4 addr`. Leave it empty on ordinary VPSs. |
| `PUBLIC_IP6` | the VPS IPv6, from `ip -6 addr show scope global` |
| `FRP_TOKEN`, `FRP_ADMIN_PASSWORD`, `TURN_SECRET` | the three fresh secrets |

The ports and ranges keep their defaults unless there is a reason to change them; every setting is documented in the file. `apply.py` refuses to start with the example domain, placeholder secrets, or secrets shorter than 24 characters.

**Treat `hub.env` like a password file.**
- `TURN_SECRET` never leaves the hub. Changing it only invalidates TURN credentials already handed out (they last 12 h).
- `FRP_TOKEN` is handed to every Solin (`GET /api/hub`) and only lets frpc log in; each meeting's tunnel is protected by its code's secret. Solin fetches it from the hub, so changing it needs no Solin release, only an frps restart.
- `FRP_ADMIN_PASSWORD` must stay private.

## 5. The tunnel certificate

frps accepts only TLS connections, with the hub's own self-signed certificate. Solin ships that certificate and trusts no other, so this is what makes a Solin hand its tunnel token and meeting secrets to this hub and nothing else.

Create it once, as `solin-hub` in `/opt/solin-hub`:
```sh
scripts/tunnel_cert.sh tls meet.solin.run
```
It writes `tls/tunnel.key` (private, mode 600) and `tls/tunnel.crt` (public), valid for 20 years, and prints the certificate's SHA-256 fingerprint. It does nothing if both already exist. `tls/` is not in git and deployments never touch it. `apply.py` refuses to deploy without it.

`tls/tunnel.crt` is copied into Solin as `src/solin/resources/hosted_meeting/hub_tunnel.crt` and ships with every release.

**Back up `tls/tunnel.key`**, off the server, together with `hub.env`. If it is lost, or the certificate is rotated (`--rotate`), no released Solin can open a meeting until a new Solin release ships the new certificate and users install it. When moving to a new server, restore the existing `tls/` instead of making a new one.

The certificate is part of frps' configuration: changing it restarts frps, which waits for a moment with no meetings open (step 6).

## 6. Deploying

Every deployment ships the repository's committed files and runs `scripts/apply.py` on the server. Only committed files are shipped (`git archive`), so local edits and secrets never leave the machine you deploy from. On the server, `hub.env`, `tls/` and `.deployed/` are kept; every other file in `/opt/solin-hub` is replaced by the repository's version, and files deleted from the repository are deleted. **Do not keep other files there.** The code registry is in the `ask-data` Docker volume, not in the directory.

### GitHub Actions (the normal way)

`.github/workflows/deploy.yml` runs on every push to `main` and can be run by hand (Actions → Deploy → Run workflow). It runs CI first, then ships `main` with `scripts/deploy.sh`, applies it, and checks `https://meet.solin.run/healthz`. One deploy runs at a time; a newer push waits.

It is configured in the repository's **Settings → Environments → `production`**:

| Kind | Name | Value |
|---|---|---|
| Variable | `HUB_HOST` | the server address (IP or host name); without it the deploy job ships nothing and ends green with a notice |
| Variable | `HUB_DOMAIN` | `meet.solin.run`; enables the public health check and the environment URL |
| Variable | `HUB_SSH_USER` | optional, default `solin-hub` |
| Variable | `HUB_SSH_PORT` | optional, default `22` |
| Variable | `HUB_DIR` | optional, default `/opt/solin-hub` |
| Secret | `HUB_SSH_KEY` | the private key `hub_deploy` from step 3 |
| Secret | `HUB_SSH_KNOWN_HOSTS` | the output of `ssh-keyscan -t ed25519 <server>`, checked against the provider's console |

Recommended protection on the environment: required reviewers, and deployment branches limited to `main`.

Inputs when run by hand:
- `dry_run`: validate and show the plan on the server without changing anything;
- `force`: restart the tunnel server or TURN relay even while meetings are open (see below).

### From a computer

```sh
git clone https://github.com/Solin-Software/solin-hub && cd solin-hub
ssh-keyscan -t ed25519 <server> > hub_known_hosts   # compare with the fingerprint in the provider's console
HUB_HOST=<server> HUB_SSH_KEY_FILE=./hub_deploy HUB_KNOWN_HOSTS=./hub_known_hosts scripts/deploy.sh [--dry-run] [--force]
```
`HUB_REF` picks another git ref (default `HEAD`). The host key is always checked.

### What `apply.py` does

1. Checks `hub.env` (required settings, no placeholder secrets) and that `tls/tunnel.crt` and `tls/tunnel.key` exist.
2. Validates the compose file, the Caddyfile and `frps.toml` with the pinned images.
3. Works out which services changed since the last apply, from each service's compose definition and the files it mounts.
4. **Refuses to restart frps or coturn, or to recreate Caddy, while any meeting is open**, unless `--force`: those restarts cut every meeting in progress.
5. Builds and (re)starts what changed. A change to only the Caddyfile or `sites.caddy` is a live `caddy reload`, which keeps open connections. The `ask` service restarts in about a second without cutting meetings.
6. Waits until every service is running and `ask` answers, then records the state in `.deployed/state.json`.

| Exit code | Meaning |
|---|---|
| 0 | Applied, or nothing to do |
| 1 | Failed (it prints the recent logs) |
| 2 | Configuration error |
| 3 | Refused because meetings are open |

On exit code 3, nothing changed: run the deploy again when no meeting is running, or with `force`, accepting that meetings are cut. `python3 scripts/apply.py --dry-run` on the server validates and shows the plan without changing anything.

**Rolling back:** revert the commit on `main`; the next deploy ships the previous configuration. In an emergency, run the Deploy workflow from an older tag or branch ("Use workflow from").

## 7. Check that it works

On the server:
```sh
cd /opt/solin-hub
docker compose ps                              # four services, all running
docker compose logs --tail 50 caddy            # certificate for meet.solin.run obtained
curl -s https://meet.solin.run/healthz         # ok
curl -s https://meet.solin.run/status          # "The hub is running. Meetings open: 0."
curl -s https://meet.solin.run/api/hub         # {"frpPort": 7000, "frpToken": "..."}
```

TURN has to be checked **from another machine**, outside the server:
```sh
python3 scripts/hubtest.py turn <PUBLIC_IP> --secret '<TURN_SECRET>' --peer 8.8.8.8 --denied-peer 10.0.0.1
python3 scripts/hubtest.py turn <PUBLIC_IP> --tcp --secret '<TURN_SECRET>'
```
To check the credentials the hub hands to Solin, create a code, ask for TURN credentials for it, and pass them with `--username` / `--password`:
```sh
SECRET=$(openssl rand -hex 32)
OWNER=$(printf %s "$SECRET" | sha256sum | cut -d' ' -f1)
curl -s -X POST https://meet.solin.run/api/codes -d "{\"owner\": \"$OWNER\", \"startsAt\": \"\"}"        # {"code": "..."}
curl -s -X POST https://meet.solin.run/api/turn -d "{\"code\": \"<code>\", \"secret\": \"$SECRET\"}"     # urls, username, credential
python3 scripts/hubtest.py turn <PUBLIC_IP> --username '<username>' --password '<credential>'
```
The test code is forgotten after 400 days; it is harmless.

To check a tunnel end to end, open a meeting in Solin and load `https://meet.solin.run/<code>`.

## 8. Host hardening

Already in place on the production server:
- **fail2ban** with an `sshd` jail and incremental bans: repeat offenders are banned for longer each time. Check it with `sudo fail2ban-client status sshd`.
- **Root SSH by key only.** Password logins for root over SSH are refused (`sudo sshd -T | grep -i permitrootlogin` shows `prohibit-password` or `without-password`).
- **Password login is kept for the provider account**, so the provider's web console still works as a way in if SSH keys are lost or SSH is broken. Keep that password strong and stored safely.
- **The firewall** from step 3, and `unattended-upgrades` installing security fixes.

Keep these in place when rebuilding the server.

## 9. Day-to-day operations

```sh
cd /opt/solin-hub
docker compose ps
docker compose logs -f --tail 100 ask frps        # codes created, refused tunnels, routing
cat .deployed/state.json                          # deployed commit and time
curl -s -u admin:<FRP_ADMIN_PASSWORD> http://127.0.0.1:7500/api/proxy/tcp   # open meetings (proxy name = code)
```

- **Updating images** (Caddy, frp, coturn, Python): change the pinned tags in `docker-compose.yml` or `ask/Dockerfile` in a pull request. Once merged, the pipeline deploys it. An frps or coturn bump counts as a restart and waits for a moment with no meetings open. Keep Solin's frpc version (`FRP_VERSION` in Solin's `scripts/fetch_hosted_meeting_helpers.py`) in step with frps.
- **Back up** `hub.env`, `tls/` (above all `tls/tunnel.key`), the `ask-data` volume (the code registry, `codes.json`) and the `caddy-data` volume (certificates and the ACME account):
  ```sh
  tar -czf hub-backup-$(date +%F).tgz hub.env tls
  docker run --rm -v solin-hub_ask-data:/data -v "$PWD":/out alpine tar -czf /out/ask-data.tgz -C /data .
  docker run --rm -v solin-hub_caddy-data:/data -v "$PWD":/out alpine tar -czf /out/caddy-data.tgz -C /data .
  ```
  Store the backup off the server; it contains secrets.
- **Moving to a new server:** prepare it (step 3), restore `hub.env` and `tls/` (do not make a new tunnel certificate), and update `PUBLIC_IP`, `PRIVATE_IP` and `PUBLIC_IP6`. Copy the code registry (`docker compose cp ask:/data/codes.json .` on the old server; after the first deploy on the new one, `docker compose cp codes.json ask:/data/codes.json` and `docker compose restart ask`). Without the registry every existing meeting link says "no such meeting" and Solin can no longer open tunnels for those codes. Point DNS at the new address, then deploy.
- **Operating system updates:** schedule reboots outside meeting times; Docker restarts the hub on boot.

## 10. Troubleshooting

| Symptom | Check |
|---|---|
| frpc: TLS or certificate error | The `tls/tunnel.crt` on the server is not the one the Solin release ships (rotated, or regenerated on a new server). Restore the backed-up `tls/`; otherwise a Solin release with the new certificate is needed. |
| frpc: `token in login doesn't match` | `FRP_TOKEN` was changed on the hub after Solin fetched it. Close and reopen the meeting in Solin. |
| frpc: `unknown meeting code or wrong secret` | The code does not exist on the hub (forgotten, or the registry was lost) or the secret is wrong. Check `docker compose logs ask` (`NewProxy rejected`). |
| frpc: `the proxy must be named ...` / `only one proxy of type 'tcp'` | The frpc.toml does not follow the template: the proxy name must equal the code, with no `user`. |
| frpc: `the hub has no free tunnel port right now` / `port already used` | Every port in `TUNNEL_PORT_MIN`–`TUNNEL_PORT_MAX` is taken (widen the range), or the same meeting is open twice (two Solin instances). |
| Deploy: `tls/tunnel.crt or tls/tunnel.key is missing` | Restore `tls/` from the backup, or create it (step 5) only if this is a brand-new hub. |
| Deploy: exit code 3, `meeting(s) are open right now` | A change needs an frps or coturn restart (or a Caddy recreate). Deploy again when no meeting is running, or with `force`. |
| Browser: TLS error / `SSL_ERROR_INTERNAL_ERROR_ALERT` | No certificate for `meet.solin.run` yet: ACME failed. Check `docker compose logs caddy` (look for `tls.obtain`). DNS must point at the server, and ports 80 and 443 must be reachable. |
| A meeting link shows "no such meeting" | The code was never created on this hub (a development Solin with `SOLIN_MEETING_SERVER` set?), was forgotten after 400 days unsynced, or the registry was lost. Check `docker compose logs ask` for `code created`. |
| A meeting link says "no longer valid" | Its owner made the code inactive in Solin. The owner can re-activate it. |
| A meeting link says "too many attempts" | That address asked for more than 20 unknown codes in 15 minutes. It clears on its own. |
| A meeting link keeps showing the waiting page | The meeting's tunnel is down: the meeting is not open in Solin, or frpc cannot connect. Check `docker compose logs frps ask`. |
| Solin: `too many new codes from this address` (429) | More than 120 new codes in an hour from one address (or IPv6 /64). Wait, or look for a misbehaving client. |
| Solin: `the hub is giving out too many codes right now` / `cannot take new codes` (503) | The global limit (3,000 new codes an hour) or the registry cap (200,000 codes) was reached. Check `docker compose logs ask`. |
| WebSockets drop at regular intervals | Make sure nobody added `transport.heartbeatTimeout` to `frps.toml`, and that tunnels use `type = "tcp"`. A Caddy reload keeps streams for 8 h; a Caddy restart drops them. |
| TURN does not relay | UDP 3478 and the relay range must be open in **both** the cloud firewall and the host firewall. On 1:1 NAT clouds set `PRIVATE_IP`. Check `docker compose logs coturn` for `relay-ip` and `401`/`403`. Peers in private ranges are refused by design. |
| Which meetings are open? | `curl -u admin:$FRP_ADMIN_PASSWORD http://127.0.0.1:7500/api/proxy/tcp` on the server (proxy name = code). |

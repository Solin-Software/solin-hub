#!/usr/bin/env bash
# Ship this repository's committed files to the hub VPS and apply them (scripts/apply.py there).
#
#   HUB_HOST=hub.example.org scripts/deploy.sh [--dry-run] [--force]
#
# Settings (environment):
#   HUB_HOST         VPS host name or IP (required)
#   HUB_SSH_USER     deploy user on the VPS (default: solin-hub)
#   HUB_SSH_PORT     SSH port (default: 22)
#   HUB_DIR          bundle directory on the VPS (default: /opt/solin-hub)
#   HUB_KNOWN_HOSTS  known_hosts file holding the VPS host key (default: ~/.ssh/known_hosts)
#   HUB_SSH_KEY_FILE private key to use (default: ssh-agent / ~/.ssh defaults)
#   HUB_REF          git ref to ship (default: HEAD)
#
# Only committed files are shipped (git archive), so local edits and secrets never leave this
# machine. On the VPS, hub.env and .deployed/ are never touched; every other file in
# HUB_DIR is replaced by the repository's version, and files deleted from the repository are deleted.
# The host key is always checked (StrictHostKeyChecking=yes).
set -euo pipefail

: "${HUB_HOST:?set HUB_HOST to the VPS host name or IP}"
HUB_SSH_USER="${HUB_SSH_USER:-solin-hub}"
HUB_SSH_PORT="${HUB_SSH_PORT:-22}"
HUB_DIR="${HUB_DIR:-/opt/solin-hub}"
HUB_KNOWN_HOSTS="${HUB_KNOWN_HOSTS:-$HOME/.ssh/known_hosts}"
HUB_REF="${HUB_REF:-HEAD}"

args=()
for arg in "$@"; do
    case "$arg" in
        --dry-run|--force) args+=("$arg") ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."
commit="$(git rev-parse --verify "$HUB_REF^{commit}")"
stage="$(mktemp -d)"
trap 'rm -rf "$stage"' EXIT
chmod 755 "$stage"  # rsync copies this mode onto HUB_DIR itself
git archive "$commit" | tar -x -C "$stage"

ssh_opts=(-p "$HUB_SSH_PORT" -o BatchMode=yes -o StrictHostKeyChecking=yes -o "UserKnownHostsFile=$HUB_KNOWN_HOSTS")
if [ -n "${HUB_SSH_KEY_FILE:-}" ]; then ssh_opts+=(-i "$HUB_SSH_KEY_FILE" -o IdentitiesOnly=yes); fi
target="$HUB_SSH_USER@$HUB_HOST"

echo "== shipping ${commit:0:12} to $target:$HUB_DIR"
# P = protect from --delete: the hub's own configuration and state live only on the VPS.
rsync -rlptz --delete \
    --filter='P /hub.env' --filter='P /.deployed/' \
    -e "ssh ${ssh_opts[*]}" "$stage/" "$target:$HUB_DIR/"

echo "== applying"
# shellcheck disable=SC2029 # expanded on purpose, on this side
ssh "${ssh_opts[@]}" "$target" "cd '$HUB_DIR' && HUB_COMMIT='$commit' python3 scripts/apply.py ${args[*]}"

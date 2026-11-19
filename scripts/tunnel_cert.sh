#!/usr/bin/env bash
# The tunnel server's (frps) TLS certificate: self-signed, for HUB_DOMAIN, valid 20 years.
# Solin ships this certificate and accepts no other, so a Solin cannot be talked into handing its
# tunnel token and meeting secrets to anything but this hub.
#
#   scripts/tunnel_cert.sh <dir> <hub-domain> [--rotate]
#
# Writes <dir>/tunnel.key (private, stays on the hub) and <dir>/tunnel.crt (public: copy it into
# Solin, src/solin/resources/hosted_meeting/hub_tunnel.crt). Does nothing if both exist, unless
# --rotate. Rotating means every Solin needs an update with the new certificate before it can
# open meetings again; back up tunnel.key instead of rotating when moving the hub.
set -euo pipefail

dir="${1:?usage: tunnel_cert.sh <dir> <hub-domain> [--rotate]}"
domain="${2:?usage: tunnel_cert.sh <dir> <hub-domain> [--rotate]}"
if [ -s "$dir/tunnel.key" ] && [ -s "$dir/tunnel.crt" ] && [ "${3:-}" != "--rotate" ]; then
    echo "$dir/tunnel.crt already exists (pass --rotate to replace it)"
    exit 0
fi
# An IP address (local tests) needs an IP entry; a name, a DNS entry.
if [[ "$domain" =~ ^[0-9.]+$ || "$domain" == *:* ]]; then san="IP:$domain"; else san="DNS:$domain"; fi
mkdir -p "$dir"
chmod 700 "$dir"
umask 077
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 7300 \
    -subj "/CN=$domain" -addext "subjectAltName=$san" \
    -addext "basicConstraints=critical,CA:FALSE" -addext "keyUsage=critical,digitalSignature" \
    -addext "extendedKeyUsage=serverAuth" \
    -keyout "$dir/tunnel.key" -out "$dir/tunnel.crt" 2>/dev/null
chmod 600 "$dir/tunnel.key"
chmod 644 "$dir/tunnel.crt"
echo "wrote $dir/tunnel.crt (SHA-256 $(openssl x509 -in "$dir/tunnel.crt" -noout -fingerprint -sha256 | cut -d= -f2))"

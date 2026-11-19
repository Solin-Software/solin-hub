#!/usr/bin/env bash
# End-to-end smoke test of the hub on one machine (local variant: hub.localhost, Caddy internal CA).
#
#   scripts/smoke_local.sh            # full run (~2 min: includes a 95 s idle WebSocket)
#   WS_IDLE=10 scripts/smoke_local.sh # quicker
#   KEEP=1 scripts/smoke_local.sh     # leave the hub running afterwards
#
# Needs: docker with the compose plugin, python3 (standard library only), curl, free ports
# 8880, 8443, 7000, 7400, 7500, 3478 (+ UDP 49160-49260), 18081-18083, 20000-20099.
#
# Brings up Caddy + frps + ask + coturn, starts dummy meetings (HTTP + WebSocket echo, like Solin's
# meeting web server) behind the official frpc image, exactly as Solin would, and checks:
#   - the hub creates meeting codes (POST /api/codes) owned by a secret; the owner can update and
#     retire them, nobody else can
#   - HTTPS GET/POST to https://HUB_DOMAIN/<code>/... reaches that meeting's tunnel
#     (Caddy -> frps -> frpc -> meeting), with X-Forwarded-* headers
#   - a WebSocket through the hub survives WS_IDLE seconds (default 95) of total silence
#   - a tunnel for a code needs that code's secret, and must be named after the code
#   - a closed meeting's code gets the hub's waiting page; a retired one "no longer valid";
#     an unknown one the landing page; a restarted frpc gets its meeting back
#   - TURN credentials come from the hub per code; coturn allocates with them (UDP, TCP, IPv6),
#     refuses expired/forged ones, and refuses relaying to private/loopback peers
set -euo pipefail

HERE="$(cd "$(dirname "$0")/.." && pwd)"
cd "$HERE"
ENVFILE=local/hub.env.local
set -a
# shellcheck disable=SC1090
. "./$ENVFILE"
set +a
WS_IDLE="${WS_IDLE:-95}"
FRPC_IMAGE=fatedier/frpc:v0.71.1
PY="python3 -I $HERE/scripts/hubtest.py"
WORK="$(mktemp -d)"
COMPOSE=(docker compose --env-file "$ENVFILE")
HUB_URL="https://${HUB_DOMAIN}:${HTTPS_PORT}"
CONNECT="127.0.0.1:${HTTPS_PORT}"
PIDS=()
FAILS=0

pass() { printf '\033[32mPASS\033[0m %s\n' "$*"; }
fail() { printf '\033[31mFAIL\033[0m %s\n' "$*"; FAILS=$((FAILS + 1)); }
step() { printf '\n\033[1m== %s\033[0m\n' "$*"; }
url() { printf '%s/%s%s' "$HUB_URL" "$1" "${2:-}"; }   # code [path]

cleanup() {
    set +e
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    docker rm -f smoke-frpc-a smoke-frpc-b smoke-frpc-evil smoke-frpc-steal >/dev/null 2>&1
    if [ "${KEEP:-0}" != 1 ]; then
        "${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1
    else
        echo "KEEP=1: hub left running (docker compose --env-file $ENVFILE down -v to stop)"
    fi
    rm -rf "$WORK"
}
trap cleanup EXIT

# frpc config for a meeting, rendered from client/frpc.toml.template exactly as Solin would.
frpc_conf() { # code secret local_port [proxy_name_override] [ca_file]
    sed -e "s|\${TUNNEL_CA}|/etc/frp/ca.crt|g" -e "s|\${HUB_ADDR}|127.0.0.1|g" -e "s|\${HUB_FRP_PORT}|${FRP_BIND_PORT}|g" \
        -e "s|\${FRP_TOKEN}|${FRP_TOKEN}|g" -e "s|\${CODE}|$1|g" -e "s|\${SECRET}|$2|g" \
        -e "s|\${LOCAL_PORT}|$3|g" client/frpc.toml.template > "$WORK/frpc-$1-$3.toml"
    if [ -n "${4:-}" ]; then sed -i "s|^name = \"$1\"|name = \"$4\"|" "$WORK/frpc-$1-$3.toml"; fi
    echo "$WORK/frpc-$1-$3.toml"
}
start_frpc() { # container conf [ca_file]: trusts the hub's tunnel certificate unless told otherwise
    docker run -d --name "$1" --network host -v "$2:/etc/frp/frpc.toml:ro" \
        -v "${3:-$WORK/tls/tunnel.crt}:/etc/frp/ca.crt:ro" "$FRPC_IMAGE" -c /etc/frp/frpc.toml >/dev/null
}
start_meeting() { # name port
    $PY meeting --name "$1" --port "$2" > "$WORK/meeting-$1.log" 2>&1 &
    PIDS+=($!)
}
api() { # method path [json] -> body in $WORK/api.out, prints the HTTP status
    curl -s -o "$WORK/api.out" -w '%{http_code}' --cacert "$WORK/ca.crt" \
        --resolve "${HUB_DOMAIN}:${HTTPS_PORT}:127.0.0.1" -X "$1" -H 'Content-Type: application/json' \
        ${3:+--data "$3"} "$HUB_URL$2"
}
sha256() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
new_code() { # secret starts_at -> prints the code
    api POST /api/codes "{\"owner\":\"$(sha256 "$1")\",\"startsAt\":\"$2\"}" >/dev/null
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["code"])' "$WORK/api.out"
}
open_now() { # code: frps reports its tunnel, so the hub routes it
    [ "$(curl -s -o /dev/null -w '%{http_code}' -H "X-Forwarded-Uri: /$1" "http://127.0.0.1:${ASK_PORT}/internal/route")" = 200 ]
}
wait_for() { # seconds description command...
    local t=$1 what=$2; shift 2
    for _ in $(seq "$t"); do "$@" && return 0; sleep 1; done
    echo "timed out waiting for $what" >&2; return 1
}

step "Bring up the local hub"
# The tunnel server's certificate, as on the VPS (frpc below dials 127.0.0.1).
scripts/tunnel_cert.sh "$WORK/tls" 127.0.0.1 >/dev/null
scripts/tunnel_cert.sh "$WORK/other-tls" 127.0.0.1 >/dev/null
chmod 755 "$WORK/tls"   # read by the frps container
export TUNNEL_TLS_DIR="$WORK/tls"
"${COMPOSE[@]}" down -v --remove-orphans >/dev/null 2>&1 || true
"${COMPOSE[@]}" up -d --build --quiet-pull 2>&1 | grep -vE '^\s*$' | tail -3
wait_for 30 "ask" bash -c "[ \"\$(curl -s http://127.0.0.1:${ASK_PORT}/healthz)\" = ok ]"
wait_for 30 "caddy" bash -c "curl -sk -o /dev/null -w '%{http_code}' --resolve ${HUB_DOMAIN}:${HTTPS_PORT}:127.0.0.1 https://${HUB_DOMAIN}:${HTTPS_PORT}/healthz | grep -q 200"
"${COMPOSE[@]}" exec -T caddy cat /data/caddy/pki/authorities/local/root.crt > "$WORK/ca.crt"
CA=(--cacert "$WORK/ca.crt" --connect "$CONNECT")
"${COMPOSE[@]}" ps --format '  {{.Service}}: {{.Status}}'

step "Meeting codes (created by the hub, owned by a secret)"
CODE_A="$(new_code secret-a "")" && CODE_B="$(new_code secret-b 2030-01-01T19:30:00Z)" \
    && CODE_C="$(new_code secret-c "")" && pass "codes created: $CODE_A $CODE_B $CODE_C" || fail "creating codes"
[ "$(api POST /api/hub)" = 404 ] && [ "$(api GET /api/hub)" = 200 ] && grep -q "$FRP_TOKEN" "$WORK/api.out" \
    && pass "GET /api/hub hands out the tunnel token" || fail "/api/hub: $(cat "$WORK/api.out")"
[ "$(api POST /api/codes/sync "{\"codes\":[{\"code\":\"$CODE_A\",\"secret\":\"secret-b\",\"active\":false}]}")" = 200 ] \
    && grep -q "$CODE_A" "$WORK/api.out" && pass "another secret cannot retire $CODE_A" || fail "foreign sync: $(cat "$WORK/api.out")"
out="$($PY http "$(url zzzzzz)" "${CA[@]}" --expect 404 --contains 'data-message="notfound"' 2>&1)" \
    && pass "unknown code: landing page with 'no such meeting'" || fail "unknown code: $out"
out="$($PY http "$HUB_URL/" "${CA[@]}" --expect 200 --contains 'action="/go"' 2>&1)" \
    && pass "landing page at /" || fail "landing: $out"

step "Meetings open their tunnels (frpc, official image) to dummy meeting web servers"
start_meeting a 18081
start_meeting b 18082
start_meeting evil 18083
start_frpc smoke-frpc-a "$(frpc_conf "$CODE_A" secret-a 18081)"
start_frpc smoke-frpc-b "$(frpc_conf "$CODE_B" secret-b 18082)"
if wait_for 20 "$CODE_A open" open_now "$CODE_A" && wait_for 20 "$CODE_B open" open_now "$CODE_B"; then
    pass "both meetings open; the hub routes their codes"
else
    fail "meetings did not open"; docker logs smoke-frpc-a 2>&1 | tail -5
fi

step "HTTPS through Caddy -> frps -> frpc -> meeting"
out="$($PY http "$(url "$CODE_A" /join/m1)" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"meeting": "a"' && echo "$out" | grep -q '"x-forwarded-proto": "https"' \
    && pass "GET /$CODE_A/join/m1 reaches meeting a: $out" || fail "GET $CODE_A: $out"
out="$($PY http "$(url "$CODE_A" /api/lobby)" "${CA[@]}" --post '{"name":"Ana"}' --expect 200 2>&1)" \
    && echo "$out" | grep -q 'Ana' && pass "POST body echoed: $out" || fail "POST: $out"
out="$($PY http "$(url "$CODE_B")" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"meeting": "b"' && pass "GET /$CODE_B reaches meeting b" || fail "GET $CODE_B: $out"
out="$($PY http "$HUB_URL/internal/route" "${CA[@]}" --expect 404 2>&1)" \
    && pass "internal endpoints not reachable from outside" || fail "internal endpoint exposed: $out"

step "Long-lived WebSocket (${WS_IDLE} s idle) through the hub, in the background"
$PY ws "$(url "$CODE_A" /ws)" "${CA[@]}" --idle "$WS_IDLE" > "$WORK/ws.log" 2>&1 &
WS_PID=$!
sleep 2
head -1 "$WORK/ws.log"

step "Hijack attempts on $CODE_A"
start_frpc smoke-frpc-evil "$(frpc_conf "$CODE_A" wrong-secret 18083)"
start_frpc smoke-frpc-steal "$(frpc_conf "$CODE_C" secret-c 18083 "$CODE_A")"
sleep 4
docker logs smoke-frpc-evil 2>&1 | grep -q "wrong secret" \
    && pass "a tunnel for $CODE_A with a wrong secret: refused" || { fail "wrong-secret frpc not refused"; docker logs smoke-frpc-evil 2>&1 | tail -3; }
docker logs smoke-frpc-steal 2>&1 | grep -q "must be named '$CODE_C'" \
    && pass "the owner of $CODE_C publishing a tunnel named $CODE_A: refused" || { fail "tunnel theft not refused"; docker logs smoke-frpc-steal 2>&1 | tail -3; }
out="$($PY http "$(url "$CODE_A" /after-hijack)" "${CA[@]}" --expect 200 2>&1)" \
    && echo "$out" | grep -q '"meeting": "a"' && pass "$CODE_A still served by its meeting" || fail "$CODE_A after hijack: $out"
docker rm -f smoke-frpc-evil smoke-frpc-steal >/dev/null

step "The tunnel is TLS with the hub's own certificate"
start_frpc smoke-frpc-evil "$(frpc_conf "$CODE_C" secret-c 18083)" "$WORK/other-tls/tunnel.crt"
sed -e 's/^transport.tls.enable = true/transport.tls.enable = false/' "$(frpc_conf "$CODE_C" secret-c 18084)" > "$WORK/frpc-plain.toml"
start_frpc smoke-frpc-steal "$WORK/frpc-plain.toml"
sleep 4
# frpc only says "session shutdown" when the hub's certificate is not the one it trusts.
logs="$(docker logs smoke-frpc-evil 2>&1)"
if echo "$logs" | grep -q "connect to server error" && ! echo "$logs" | grep -q "login to server success"; then
    pass "frpc trusting another certificate does not connect"
else
    fail "frpc accepted a certificate it does not trust"; echo "$logs" | tail -3
fi
docker logs smoke-frpc-steal 2>&1 | grep -q "start proxy success" \
    && { fail "frpc without TLS was let in"; docker logs smoke-frpc-steal 2>&1 | tail -3; } || pass "frpc without TLS is refused"
docker rm -f smoke-frpc-evil smoke-frpc-steal >/dev/null

step "Closed meeting"
docker rm -f smoke-frpc-b >/dev/null
wait_for 10 "$CODE_B closed" bash -c "! curl -s -o /dev/null -w '%{http_code}' -H 'X-Forwarded-Uri: /$CODE_B' http://127.0.0.1:${ASK_PORT}/internal/route | grep -q 200" || true
out="$($PY http "$(url "$CODE_B" /s)" "${CA[@]}" --expect 503 --contains 'data-message="waiting"' 2>&1)" \
    && pass "$CODE_B closed: the hub's waiting page (503)" || fail "closed $CODE_B: $out"

step "Retired code"
[ "$(api POST /api/codes/sync "{\"codes\":[{\"code\":\"$CODE_C\",\"secret\":\"secret-c\",\"active\":false}]}")" = 200 ] \
    && grep -q '"rejected": \[\]' "$WORK/api.out" || fail "retiring $CODE_C: $(cat "$WORK/api.out")"
out="$($PY http "$(url "$CODE_C")" "${CA[@]}" --expect 410 --contains 'data-message="retired"' 2>&1)" \
    && pass "$CODE_C retired by its owner: 'no longer valid' (410)" || fail "retired code: $out"

step "TURN credentials from the hub"
[ "$(api POST /api/turn "{\"code\":\"$CODE_A\",\"secret\":\"wrong\"}")" = 403 ] \
    && pass "no TURN credentials without the code's secret" || fail "TURN credentials leaked"
[ "$(api POST /api/turn "{\"code\":\"$CODE_A\",\"secret\":\"secret-a\"}")" = 200 ] || fail "TURN credentials: $(cat "$WORK/api.out")"
TURN_USER="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["username"])' "$WORK/api.out")"
TURN_PASS="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["credential"])' "$WORK/api.out")"
if $PY turn 127.0.0.1 --port "$TURN_PORT" --username "$TURN_USER" --password "$TURN_PASS" --peer 8.8.8.8; then
    pass "coturn accepts the hub's credentials for $CODE_A"
else
    fail "coturn refused the hub's credentials"
fi

step "TURN (coturn) with time-limited credentials"
if $PY turn 127.0.0.1 --port "$TURN_PORT" --secret "$TURN_SECRET" --peer 8.8.8.8 \
        --denied-peer 10.1.2.3 --denied-peer 192.168.1.1 --denied-peer 172.16.0.1 --denied-peer 127.0.0.1 \
        --denied-peer 100.64.0.1 --denied-peer 169.254.169.254; then :; else fail "TURN over UDP"; fi
if $PY turn 127.0.0.1 --tcp --port "$TURN_PORT" --secret "$TURN_SECRET" --peer 1.1.1.1 --denied-peer 10.0.0.1; then :
else fail "TURN over TCP"; fi
if python3 -c 'import socket; s=socket.socket(socket.AF_INET6, socket.SOCK_DGRAM); s.connect(("::1", 9))' 2>/dev/null; then
    if $PY turn ::1 --port "$TURN_PORT" --relay-family 6 --secret "$TURN_SECRET" \
            --peer 2606:4700:4700::1111 --denied-peer fd00::1 --denied-peer fe80::1 --denied-peer ::1; then :
    else fail "TURN over IPv6"; fi
else
    echo "(no IPv6 loopback here: IPv6 TURN check skipped)"
fi

step "Waiting for the WebSocket soak to finish"
if wait "$WS_PID"; then pass "$(tail -1 "$WORK/ws.log" | sed 's/^PASS: //')"; else fail "WebSocket: $(cat "$WORK/ws.log")"; fi

step "A meeting reconnects after its tunnel drops"
docker restart smoke-frpc-a >/dev/null
if wait_for 20 "$CODE_A open again" open_now "$CODE_A" \
        && out="$($PY http "$(url "$CODE_A" /back)" "${CA[@]}" --expect 200 2>&1)"; then
    pass "$CODE_A served again after an frpc restart"
else
    fail "$CODE_A did not come back: ${out:-}"
fi

step "Result"
if [ "$FAILS" -eq 0 ]; then
    printf '\033[32mALL CHECKS PASSED\033[0m\n'
else
    printf '\033[31m%d CHECK(S) FAILED\033[0m\n' "$FAILS"
    "${COMPOSE[@]}" logs --no-color --tail 30 ask frps caddy 2>&1 | tail -60
    exit 1
fi

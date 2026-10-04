#!/usr/bin/env bash
# Provision AndroidC2 as two systemd units: gunicorn behind Caddy.
#
#   sudo ./deploy/install.sh
#
# Creates a dedicated `androidc2` user, a venv, /etc/androidc2/env with the
# generated secrets, and enables both services.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="${APP_DIR:-/opt/androidc2}"
SVC_USER="${SVC_USER:-androidc2}"
DATA_DIR="${APP_DIR}/data"
ENV_FILE="/etc/androidc2/env"
TLS_HOST="${TLS_HOST:-}"

die() { echo "error: $*" >&2; exit 1; }
say() { printf '  %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run as root"

command -v python3 >/dev/null || die "python3 not found"
command -v caddy  >/dev/null || die "caddy not found (https://caddyserver.com/docs/install)"

if [[ -z "$TLS_HOST" ]]; then
    die "set TLS_HOST to the hostname agents will reach, e.g.
    TLS_HOST=panel.example.com sudo -E ./deploy/install.sh"
fi

# ---------------------------------------------------------------- layout

id -u "$SVC_USER" >/dev/null 2>&1 || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin "$SVC_USER"
say "user      $SVC_USER"

install -d -m 0755 "$APP_DIR"
if [[ "$REPO_DIR" != "$APP_DIR" ]]; then
    say "syncing   $REPO_DIR -> $APP_DIR"
    rsync -a --delete \
        --exclude '.git' --exclude 'data' --exclude '__pycache__' \
        --exclude '.venv' --exclude 'c2.db*' --exclude 'static/uploads' \
        "$REPO_DIR"/ "$APP_DIR"/
fi

install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$DATA_DIR"
install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$APP_DIR/deploy"

# -------------------------------------------------------------- secrets

python3 - "$ENV_FILE" <<'PY'
import os, pathlib, secrets, sys

path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)

# Reuse what the repo already generated so the agent key does not rotate
# silently under you — that would break every deployed APK.
def reuse(name, env_name):
    env = os.environ.get(env_name)
    if env:
        return env.strip()
    repo = pathlib.Path(__file__).resolve().parent if False else "."
    for candidate in (pathlib.Path("data") / f"{env_name.lower()}.secret",
                      pathlib.Path(f"/opt/androidc2/data/{env_name.lower()}.secret")):
        try:
            if candidate.is_file():
                value = candidate.read_text().strip()
                if value:
                    return value
        except OSError:
            pass
    return secrets.token_urlsafe(32)

values = {
    "C2_API_KEY":         reuse("api_key", "C2_API_KEY"),
    "C2_SECRET":          reuse("secret", "C2_SECRET"),
    "C2_OPERATOR_PASSWORD": os.environ.get("C2_OPERATOR_PASSWORD")
                            or reuse("operator", "C2_OPERATOR_PASSWORD"),
}
body = "\n".join(f"{k}={v}" for k, v in values.items()) + "\n"
path.write_text(body)
path.chmod(0o600)
for k, v in values.items():
    import hashlib
    print(f"  {k:22} {hashlib.sha256(v.encode()).hexdigest()[:8]}  ({len(v)} chars)")
PY
chown "$SVC_USER:$SVC_USER" "$ENV_FILE"
say "secrets   $ENV_FILE (0600, reused existing values where present)"

# ----------------------------------------------------------------- venv

su -s /bin/sh -c "cd '$APP_DIR' && python3 -m venv .venv" "$SVC_USER"
su -s /bin/sh -c "'$APP_DIR/.venv/bin/pip' install -q --upgrade pip" "$SVC_USER"
su -s /bin/sh -c "'$APP_DIR/.venv/bin/pip' install -q -r '$APP_DIR/requirements.txt'" "$SVC_USER"
say "venv      $APP_DIR/.venv"

# --------------------------------------------------------------- config

Caddyfile="$APP_DIR/deploy/Caddyfile"
if [[ -f "$Caddyfile" ]] && ! grep -q "$TLS_HOST" "$Caddyfile"; then
    sed -i "s|^[a-z0-9.-]*\.[a-z]\\{2,\\} {$|$TLS_HOST {|" "$Caddyfile"
    say "caddy     host set to $TLS_HOST"
fi

# -------------------------------------------------------------- systemd

install -m 0644 "$APP_DIR/deploy/androidc2.service" /etc/systemd/system/androidc2.service
install -m 0644 "$APP_DIR/deploy/caddy.service"     /etc/systemd/system/caddy.service
sed -i "s|/opt/androidc2|$APP_DIR|g" /etc/systemd/system/androidc2.service

systemctl daemon-reload
systemctl enable --now androidc2.service
systemctl reload-or-restart caddy.service

sleep 2
echo
systemctl --no-pager --lines=0 status androidc2.service || true
echo
cat <<EOF

  done.

  dashboard   https://$TLS_HOST
  password    sudo cat /etc/androidc2/env | grep C2_OPERATOR_PASSWORD
  logs        journalctl -u androidc2 -f
  agent URL   C2_URL=https://$TLS_HOST
  bridge      C2_BASE=https://$TLS_HOST

  Rebuild the APK with C2_URL above before testing.
EOF
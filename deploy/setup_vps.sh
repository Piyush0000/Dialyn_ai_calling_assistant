#!/usr/bin/env bash
# One-time Dialyn setup on a VPS that may also host other projects. Safe to re-run.
#
# Everything lives in /opt/dialyn and runs as its own "dialyn" user. Other projects'
# files, sites and services are never modified. Outside /opt/dialyn this script only:
#   - installs git / curl / Docker / Nginx / Certbot if they are missing
#   - creates the "dialyn" user (in the docker group) and a deploy SSH key for CI
#   - adds ONE Nginx site file for your domain; reloads Nginx only if `nginx -t` passes
#   - requests an HTTPS certificate for your domain only (Let's Encrypt)
#   - opens UDP 40000-40199 in UFW if UFW is active (audio for browser calls)
#
# Usage, as root on the VPS:
#   curl -fsSL https://raw.githubusercontent.com/Piyush0000/Dialyn_ai_calling_assistant/main/deploy/setup_vps.sh -o setup_vps.sh
#   DEEPGRAM_API_KEY=... GROQ_API_KEY=... bash setup_vps.sh [domain] [email]
# domain defaults to <server-ip-with-dashes>.sslip.io (free, no DNS setup needed).
set -euo pipefail

REPO_URL=https://github.com/Piyush0000/Dialyn_ai_calling_assistant.git
APP_DIR=/opt/dialyn
APP_USER=dialyn
UDP_FIRST=40000
UDP_LAST=40199
MARKER="managed by deploy/setup_vps.sh"

step() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
as_app() { runuser -u "$APP_USER" -- "$@"; }

[ "$(id -u)" = 0 ] || die "run as root (sudo bash $0 ...)"

PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
DOMAIN=${1:-${PUBLIC_IP//./-}.sslip.io}
EMAIL=${2:-}
step "Installing Dialyn for https://$DOMAIN into $APP_DIR (server IP $PUBLIC_IP)"

# ------------------------------------------------------------------ packages
missing=()
command -v git >/dev/null || missing+=(git)
command -v curl >/dev/null || missing+=(curl)
if ((${#missing[@]})); then
  step "Installing ${missing[*]}"
  apt-get update -qq && apt-get install -y -qq "${missing[@]}"
fi
if ! command -v docker >/dev/null; then
  step "Installing Docker (official get.docker.com script)"
  curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || die "the Docker Compose plugin is missing"

# ---------------------------------------------------------------------- user
if ! id "$APP_USER" >/dev/null 2>&1; then
  step "Creating system user $APP_USER"
  useradd --system --create-home --shell /bin/bash "$APP_USER"
fi
usermod -aG docker "$APP_USER"
APP_HOME=$(getent passwd "$APP_USER" | cut -d: -f6)

# ---------------------------------------------------------------------- code
if [ -d "$APP_DIR/.git" ]; then
  step "Updating code in $APP_DIR"
  as_app git -C "$APP_DIR" fetch -q --depth 1 origin main
  as_app git -C "$APP_DIR" reset -q --hard origin/main
elif [ -e "$APP_DIR" ]; then
  die "$APP_DIR already exists and is not Dialyn; not touching it"
else
  step "Cloning code into $APP_DIR"
  install -d -o "$APP_USER" -g "$APP_USER" "$APP_DIR"
  as_app git clone -q --depth 1 --branch main "$REPO_URL" "$APP_DIR"
fi

# ---------------------------------------------------------------- app config
ENV_FILE=$APP_DIR/agent/.env
set_env() {
  if grep -q "^$1=" "$ENV_FILE"; then sed -i "s|^$1=.*|$1=$2|" "$ENV_FILE"; else echo "$1=$2" >>"$ENV_FILE"; fi
}
secret() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c 40; }
if [ ! -f "$ENV_FILE" ]; then
  step "Creating $ENV_FILE"
  cp "$APP_DIR/agent/.env.example" "$ENV_FILE"
  set_env API_KEY "$(secret)"
  set_env STREAM_SIGNING_SECRET "$(secret)"
  set_env DATABASE_URL "sqlite+aiosqlite:///./data/calls.db"
  set_env RECORDINGS_DIR "./data/recordings"
  set_env DEFAULT_AGENT_ID free
  set_env TWILIO_PHONE_NUMBER ""
fi
set_env PUBLIC_HOST "$DOMAIN"
set_env WEBRTC_UDP_PORTS "$UDP_FIRST-$UDP_LAST"
for key in DEEPGRAM_API_KEY GROQ_API_KEY SARVAM_API_KEY OPENAI_API_KEY ELEVENLABS_API_KEY CARTESIA_API_KEY \
  TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN PLIVO_AUTH_ID PLIVO_AUTH_TOKEN; do
  if [ -n "${!key:-}" ]; then set_env "$key" "${!key}"; echo "    set $key from the environment"; fi
done
chown "$APP_USER:$APP_USER" "$ENV_FILE"
chmod 600 "$ENV_FILE"

# Pick a free local port once (the other projects keep theirs).
PORT_FILE=$APP_DIR/deploy/.env
if [ ! -f "$PORT_FILE" ]; then
  PORT=7860
  while ss -ltnH "( sport = :$PORT )" | grep -q .; do PORT=$((PORT + 1)); done
  echo "DIALYN_PORT=$PORT" >"$PORT_FILE"
  chown "$APP_USER:$APP_USER" "$PORT_FILE"
fi
PORT=$(cut -d= -f2 "$PORT_FILE")
step "Dialyn will listen on 127.0.0.1:$PORT (private; Nginx publishes it)"

# -------------------------------------------------------------------- start
step "Building and starting Dialyn (first build takes a few minutes)"
as_app bash "$APP_DIR/deploy/deploy.sh"

# -------------------------------------------------------------------- nginx
if ! command -v nginx >/dev/null; then
  if ss -ltnH '( sport = :80 or sport = :443 )' | grep -q .; then
    die "ports 80/443 are used by a web server that is not Nginx; point $DOMAIN at 127.0.0.1:$PORT there"
  fi
  step "Installing Nginx"
  apt-get update -qq && apt-get install -y -qq nginx
fi
if [ -d /etc/nginx/sites-available ]; then
  SITE=/etc/nginx/sites-available/dialyn.conf
  LINK=/etc/nginx/sites-enabled/dialyn.conf
else
  SITE=/etc/nginx/conf.d/dialyn.conf
  LINK=""
fi
if [ -f "$SITE" ] && ! grep -q "$MARKER" "$SITE"; then
  die "$SITE exists but was not created by Dialyn; not touching it"
fi
if [ ! -f "$SITE" ]; then
  step "Adding Nginx site $SITE for $DOMAIN"
  sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$PORT/g" "$APP_DIR/deploy/nginx.conf.template" >"$SITE"
  [ -n "$LINK" ] && ln -sf "$SITE" "$LINK"
  if nginx -t; then
    systemctl reload nginx
  else
    rm -f "$SITE" ${LINK:+"$LINK"}
    die "nginx -t failed, so Dialyn's site file was removed and Nginx was NOT reloaded"
  fi
fi

# ---------------------------------------------------------------------- https
if ! grep -q "listen 443" "$SITE"; then
  step "Requesting an HTTPS certificate for $DOMAIN"
  command -v certbot >/dev/null || { apt-get update -qq && apt-get install -y -qq certbot python3-certbot-nginx; }
  args=(--nginx -d "$DOMAIN" --non-interactive --agree-tos --redirect)
  if [ -n "$EMAIL" ]; then args+=(-m "$EMAIL"); else args+=(--register-unsafely-without-email); fi
  certbot "${args[@]}"
fi

# -------------------------------------------------------------------- firewall
if command -v ufw >/dev/null && ufw status | grep -q "Status: active"; then
  step "Allowing UDP $UDP_FIRST-$UDP_LAST in UFW (browser call audio)"
  ufw allow "$UDP_FIRST:$UDP_LAST/udp" comment "Dialyn WebRTC audio"
  ufw status | grep -qE "^(80|443|Nginx)" || echo "    note: make sure ports 80 and 443 are open in UFW"
fi

# ------------------------------------------------------------------ CI deploy key
KEY=$APP_HOME/.ssh/github_actions
if [ ! -f "$KEY" ]; then
  step "Creating an SSH key GitHub Actions uses to deploy (user $APP_USER only)"
  as_app mkdir -p "$APP_HOME/.ssh"
  as_app ssh-keygen -q -t ed25519 -N "" -C "dialyn-github-actions" -f "$KEY"
  as_app sh -c "cat '$KEY.pub' >> '$APP_HOME/.ssh/authorized_keys'"
  chmod 700 "$APP_HOME/.ssh"
  chmod 600 "$APP_HOME/.ssh/authorized_keys"
fi

cat <<EOF

============================================================
 Dialyn is running:  https://$DOMAIN/dashboard
============================================================
 App folder:   $APP_DIR          (runs as user "$APP_USER")
 Settings:     $ENV_FILE
 Admin key:    grep ^API_KEY= $ENV_FILE

 Add missing provider keys (e.g. DEEPGRAM_API_KEY, GROQ_API_KEY) to the
 settings file, then restart:  runuser -u $APP_USER -- bash $APP_DIR/deploy/deploy.sh

 GitHub Actions (auto-deploy on every push to main): in the GitHub repo go to
 Settings -> Secrets and variables -> Actions and add
   VPS_HOST     = $PUBLIC_IP
   VPS_USER     = $APP_USER
   VPS_SSH_KEY  = the output of:  cat $KEY
EOF

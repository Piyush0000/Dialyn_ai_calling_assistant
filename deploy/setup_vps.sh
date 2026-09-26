#!/usr/bin/env bash
# One-time Dialyn setup on a VPS that also hosts other projects. Safe to re-run.
#
# Dialyn lives in /var/www/dialyn and runs as the PM2 app "dialyn" next to the
# existing PM2 apps. Other projects' files, sites and PM2 apps are never modified.
# This script only:
#   - installs uv (Python package manager) for root if missing
#   - clones the code into /var/www/dialyn and creates its settings file
#   - starts the PM2 app "dialyn" on 127.0.0.1 (a free local port)
#   - adds ONE Nginx site file for the domain; reloads Nginx only if `nginx -t` passes
# Security-sensitive steps (firewall, HTTPS terms, CI key) are printed at the end for
# the server owner to run.
#
# Usage, as root:  DEEPGRAM_API_KEY=... GROQ_API_KEY=... bash setup_vps.sh [domain]
# domain defaults to <server-ip-with-dashes>.sslip.io (free, no DNS setup needed).
set -euo pipefail

REPO_URL=https://github.com/Piyush0000/Dialyn_ai_calling_assistant.git
APP_DIR=/var/www/dialyn
UDP_RANGE=40000-40199
MARKER="managed by deploy/setup_vps.sh"

step() { printf '\n==> %s\n' "$*"; }
die() { printf '\nERROR: %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"
command -v pm2 >/dev/null || die "pm2 not found (expected the existing PM2 setup)"
command -v nginx >/dev/null || die "nginx not found"

PUBLIC_IP=$(curl -fsS --max-time 10 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')
DOMAIN=${1:-${PUBLIC_IP//./-}.sslip.io}
step "Installing Dialyn for $DOMAIN into $APP_DIR"

# ------------------------------------------------------------------------ uv
export PATH="$HOME/.local/bin:$PATH"
if ! command -v uv >/dev/null; then
  step "Installing uv"
  curl -LsSf https://astral.sh/uv/install.sh | sh
fi

# ---------------------------------------------------------------------- code
if [ -d "$APP_DIR/.git" ]; then
  step "Updating code"
  git -C "$APP_DIR" fetch -q --depth 1 origin main
  git -C "$APP_DIR" reset -q --hard origin/main
elif [ -e "$APP_DIR" ]; then
  die "$APP_DIR exists and is not Dialyn; not touching it"
else
  step "Cloning code"
  git clone -q --depth 1 --branch main "$REPO_URL" "$APP_DIR"
fi

# ---------------------------------------------------------------- settings
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
set_env WEBRTC_UDP_PORTS "$UDP_RANGE"
for key in DEEPGRAM_API_KEY GROQ_API_KEY SARVAM_API_KEY OPENAI_API_KEY ELEVENLABS_API_KEY CARTESIA_API_KEY \
  TWILIO_ACCOUNT_SID TWILIO_AUTH_TOKEN PLIVO_AUTH_ID PLIVO_AUTH_TOKEN; do
  if [ -n "${!key:-}" ]; then set_env "$key" "${!key}"; echo "    set $key"; fi
done
chmod 600 "$ENV_FILE"

PORT_FILE=$APP_DIR/deploy/.env
if [ ! -f "$PORT_FILE" ]; then
  PORT=7860
  while ss -ltnH "( sport = :$PORT )" | grep -q .; do PORT=$((PORT + 1)); done
  echo "DIALYN_PORT=$PORT" >"$PORT_FILE"
fi
PORT=$(cut -d= -f2 "$PORT_FILE")

# ---------------------------------------------------------------- start app
step "Installing dependencies and starting PM2 app 'dialyn' on 127.0.0.1:$PORT"
bash "$APP_DIR/deploy/deploy.sh"

# -------------------------------------------------------------------- nginx
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
  step "Adding Nginx site $SITE"
  sed -e "s/__DOMAIN__/$DOMAIN/g" -e "s/__PORT__/$PORT/g" "$APP_DIR/deploy/nginx.conf.template" >"$SITE"
  [ -n "$LINK" ] && ln -sf "$SITE" "$LINK"
  if nginx -t; then
    systemctl reload nginx
  else
    rm -f "$SITE" ${LINK:+"$LINK"}
    die "nginx -t failed; Dialyn's site file was removed and Nginx was NOT reloaded"
  fi
fi

cat <<EOF

============================================================
 Dialyn is running under PM2 as "dialyn" (pm2 list).
 http://$DOMAIN/dashboard  (HTTPS after step 2 below)
============================================================
 Folder:    $APP_DIR
 Settings:  $ENV_FILE   (admin key: grep ^API_KEY= $ENV_FILE)
 Logs:      pm2 logs dialyn

 Finish as the server owner (these change security settings):

 1) Allow audio for browser calls through the firewall:
      ufw allow ${UDP_RANGE/-/:}/udp comment 'Dialyn WebRTC audio'

 2) HTTPS certificate for $DOMAIN (you accept Let's Encrypt's terms):
      certbot --nginx -d $DOMAIN --redirect

 3) Auto-deploy from GitHub: create a key that can ONLY run the deploy script:
      ssh-keygen -q -t ed25519 -N "" -C dialyn-github-actions -f /root/.ssh/dialyn_github_actions
      echo "command=\"bash $APP_DIR/deploy/deploy.sh\",no-port-forwarding,no-agent-forwarding,no-X11-forwarding,no-pty \$(cat /root/.ssh/dialyn_github_actions.pub)" >> /root/.ssh/authorized_keys
    then add GitHub repo secrets (Settings -> Secrets and variables -> Actions):
      VPS_HOST=$PUBLIC_IP   VPS_USER=root   VPS_SSH_KEY=<output of: cat /root/.ssh/dialyn_github_actions>
EOF

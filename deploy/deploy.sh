#!/usr/bin/env bash
# Pull the latest main and (re)start Dialyn under PM2. Run on the VPS by GitHub Actions
# or by hand. Touches only /var/www/dialyn and the PM2 app named "dialyn".
set -euo pipefail

APP_DIR=/var/www/dialyn
export PATH="$HOME/.local/bin:$PATH"  # uv
export NVM_DIR="$HOME/.nvm"
# shellcheck disable=SC1091
[ -s "$NVM_DIR/nvm.sh" ] && . "$NVM_DIR/nvm.sh"  # pm2 installed through nvm

cd "$APP_DIR"
echo "==> Updating code"
git fetch --depth 1 -q origin main
git reset -q --hard origin/main
git log -1 --oneline

echo "==> Installing Python dependencies"
(cd agent && uv sync --frozen --no-dev --quiet)

PORT=$(grep -E '^DIALYN_PORT=' deploy/.env 2>/dev/null | cut -d= -f2 || true)
PORT=${PORT:-7860}

echo "==> Restarting PM2 app 'dialyn' (port $PORT)"
pm2 startOrReload deploy/ecosystem.config.js --update-env
pm2 save >/dev/null

echo "==> Waiting for health check"
for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
    echo "Dialyn is up."
    exit 0
  fi
  sleep 2
done
echo "Dialyn did not become healthy; recent logs:" >&2
pm2 logs dialyn --lines 60 --nostream >&2
exit 1

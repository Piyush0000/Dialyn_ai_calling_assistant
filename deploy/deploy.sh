#!/usr/bin/env bash
# Pull the latest main and restart Dialyn. Run on the VPS (by GitHub Actions or by hand)
# as the "dialyn" user. Touches nothing outside /opt/dialyn and Dialyn's containers.
set -euo pipefail

APP_DIR=/opt/dialyn
cd "$APP_DIR"

echo "==> Updating code"
git fetch --depth 1 origin main
git reset --hard origin/main
git log -1 --oneline

PORT=$(grep -E '^DIALYN_PORT=' deploy/.env 2>/dev/null | cut -d= -f2 || true)
PORT=${PORT:-7860}

echo "==> Building and restarting (port $PORT)"
docker compose -f deploy/docker-compose.yml --env-file deploy/.env up -d --build --remove-orphans

echo "==> Waiting for health check"
for _ in $(seq 1 60); do
  if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null; then
    echo "Dialyn is up."
    docker image prune -f >/dev/null || true
    exit 0
  fi
  sleep 2
done

echo "Dialyn did not become healthy; recent logs:" >&2
docker compose -f deploy/docker-compose.yml --env-file deploy/.env logs --tail 80 agent >&2
exit 1

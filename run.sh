#!/bin/bash
set -euo pipefail

echo "=== JobSeeker Backend ==="

# Restore database
if [ ! -f /app/pb_data/data.db ]; then
  echo "[*] Restoring database from S3..."
  if /app/litestream restore -config /app/litestream.yml /app/pb_data/data.db; then
    echo "[✓] Restore successful"
  else
    echo "[!] Restore failed, continuing with fresh database"
  fi
else
  echo "[✓] Local database exists, skipping restore"
fi

# Start Litestream replication
echo "[*] Starting Litestream replication..."
/app/litestream replicate -config /app/litestream.yml &

# Start PocketBase in background
echo "[*] Starting PocketBase on port 8090"
/app/pocketbase serve --http=0.0.0.0:8090 &
PB_PID=$!

sleep 3

# Worker environment. Credentials must be supplied by the deployment secret store.
export POCKETBASE_URL="http://localhost:8090"
: "${PB_ADMIN_TOKEN:?PB_ADMIN_TOKEN secret is required}"
: "${MISTRAL_API_KEY:?MISTRAL_API_KEY secret is required}"
export POCKETBASE_ADMIN_TOKEN="${PB_ADMIN_TOKEN}"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

echo "[*] Starting JobSeeker AI supervisor..."
exec /app/worker-venv/bin/python -u /app/supervisor.py

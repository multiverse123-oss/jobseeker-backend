#!/bin/bash
set -e

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

# Worker setup
export POCKETBASE_URL="http://localhost:8090"
export POCKETBASE_ADMIN_TOKEN="${POCKETBASE_ADMIN_TOKEN:-eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJleHAiOjE3ODU4MDYwMTAsImlkIjoidzFtN3pna2Z2dG5xbmg5IiwidHlwZSI6ImFkbWluIn0.H0IuaaTm9BUTunuIafPbpF6VBlQzQ_2qihAXfFEcKEI}"
export MISTRAL_API_KEY="${MISTRAL_API_KEY:-7Y9YTSfAfqL4MEHL6YHPH5BEOlONfVU2}"
export PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1
export PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH=/usr/bin/chromium

echo "[*] Starting JobSeeker AI worker..."
/app/worker-venv/bin/python /app/worker.py &
WORKER_PID=$!

# Wait for both to keep the container alive (simple wait works in all shells)
wait

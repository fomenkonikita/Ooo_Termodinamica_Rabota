#!/usr/bin/env bash
# preflight.sh — проверка старта бота и потребления памяти (< 480 MB)
# Запускать из корня репозитория: bash preflight.sh
# Требования: python3, pip install -r requirements.txt

set -e
PY=python3

# Заглушки для CI — соединения с Telegram и Google не устанавливаются,
# потому что DISABLE_HEAVY_JOBS=1 пропускает polling и scheduler
export BOT_TOKEN="${BOT_TOKEN:-test:0000000000:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA}"
export GOOGLE_CLIENT_ID="${GOOGLE_CLIENT_ID:-ci-fake}"
export GOOGLE_CLIENT_SECRET="${GOOGLE_CLIENT_SECRET:-ci-fake}"
export GOOGLE_DRIVE_REFRESH_TOKEN="${GOOGLE_DRIVE_REFRESH_TOKEN:-ci-fake}"
export SPREADSHEET_ID="${SPREADSHEET_ID:-ci-fake}"
export TZ_OFFSET="${TZ_OFFSET:-5}"
export PORT="18080"
export DISABLE_HEAVY_JOBS=1

echo ">>> Запуск бота в режиме preflight (порт $PORT)..."
$PY bot.py &
APP_PID=$!
sleep 4

# Smoke test: health endpoint должен вернуть 200
echo ">>> Smoke test: GET http://127.0.0.1:$PORT/ ..."
$PY - <<'PY'
import requests, sys, os
port = os.environ.get("PORT", "18080")
try:
    r = requests.get(f"http://127.0.0.1:{port}/", timeout=5)
    assert r.status_code == 200, f"HTTP {r.status_code}"
    print("SMOKE OK:", r.text.strip())
except Exception as e:
    print("SMOKE FAIL:", e)
    sys.exit(2)
PY

# Проверка памяти: RSS < 480 MB (лимит Render free tier — 512 MB)
if command -v ps >/dev/null 2>&1; then
    RSS_KB=$(ps -o rss= -p $APP_PID 2>/dev/null | awk '{print $1}' || echo 0)
    RSS_MB=$((RSS_KB / 1024))
    echo ">>> Память: ${RSS_MB} MB (лимит: 480 MB)"
    if [ "$RSS_MB" -gt 480 ]; then
        echo "MEMORY CHECK FAILED: ${RSS_MB} MB > 480 MB"
        kill $APP_PID 2>/dev/null || true
        exit 3
    fi
fi

kill $APP_PID 2>/dev/null || true
echo "PREFLIGHT PASSED"
exit 0

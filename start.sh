#!/bin/bash
# Railway 啟動腳本：先在背景啟動，等 5 秒後檢查 /health
# 如果崩潰，輸出錯誤訊息到 stderr

set -e

echo "=== MHC WebApp Starting ==="
echo "Python: $(python3 --version)"
echo "DATABASE_URL: ${DATABASE_URL:0:30}..."
echo "SECRET_KEY: ${SECRET_KEY:0:8}..."

# 啟動 uvicorn（捕捉錯誤）
exec uv run uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}" 2>&1

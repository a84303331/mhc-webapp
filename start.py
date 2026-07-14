"""Railway 啟動腳本：包裹 main:app，完整捕捉啟動錯誤"""
import sys
import traceback

try:
    from main import app
    import uvicorn
    import os
    port = int(os.environ.get("PORT", 8000))
    print(f"Starting MHC WebApp on port {port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)
except Exception as e:
    print("=" * 60, flush=True)
    print("FATAL STARTUP ERROR:", flush=True)
    traceback.print_exc()
    print("=" * 60, flush=True)
    sys.exit(1)

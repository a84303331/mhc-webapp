"""MHC WebApp — PC Backend 健康監控

使用 APScheduler 定時 ping PC Backend /health 端點。
記錄最後一次 ping 的狀態與時間，供 Railway /api/health/pc-status 查詢。
"""

import os
import logging
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Optional

import httpx
from apscheduler.schedulers.asyncio import AsyncIOScheduler

logger = logging.getLogger("mhc-health-monitor")

MHC_API_URL = os.getenv("MHC_API_URL", "https://api.summer-hsia.com")
MHC_API_TOKEN = os.getenv("MHC_API_TOKEN", "")
PING_INTERVAL_MINUTES = int(os.getenv("HEALTH_PING_INTERVAL", "5"))


@dataclass
class PCStatus:
    """PC Backend 健康狀態快照"""
    online: bool = False
    last_check: Optional[datetime] = None
    last_ok: Optional[datetime] = None
    last_error: Optional[str] = None
    uptime_seconds: float = 0
    vault_accessible: bool = False
    consecutive_failures: int = 0


# 全域狀態
_status = PCStatus()


def get_status() -> PCStatus:
    return _status


async def _ping_pc_backend():
    """執行一次 PC Backend 健康檢查"""
    global _status
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"{MHC_API_URL}/health",
                headers={"Authorization": f"Bearer {MHC_API_TOKEN}"},
            )
            if resp.status_code == 200:
                data = resp.json()
                _status.online = True
                _status.last_ok = datetime.now(timezone.utc)
                _status.last_error = None
                _status.uptime_seconds = data.get("uptime_seconds", 0)
                _status.vault_accessible = data.get("vault_accessible", False)
                _status.consecutive_failures = 0
                logger.debug(f"pc_backend_health_ok uptime={_status.uptime_seconds}s vault={_status.vault_accessible}")
            else:
                raise Exception(f"HTTP {resp.status_code}")
    except Exception as e:
        _status.online = False
        _status.consecutive_failures += 1
        _status.last_error = str(e)
        logger.error(
            f"pc_backend_health_fail error={e} consecutive={_status.consecutive_failures}"
        )
    finally:
        _status.last_check = datetime.now(timezone.utc)


def start_scheduler():
    """啟動背景定時健康檢查"""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _ping_pc_backend,
        "interval",
        minutes=PING_INTERVAL_MINUTES,
        id="ping_pc_backend",
        name="PC Backend Health Check",
        next_run_time=datetime.now(timezone.utc),  # 立即執行首次 ping
    )
    scheduler.start()
    logger.info(
        f"health_monitor_started target={MHC_API_URL} interval_minutes={PING_INTERVAL_MINUTES}"
    )
    return scheduler

"""MHC WebApp — PC API Client

透過 Cloudflare Tunnel 呼叫 PC 端的 MHC Backend API。
"""

import os
import logging
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

MHC_API_URL = os.getenv("MHC_API_URL", "https://api.summer-hsia.com")
MHC_API_TOKEN = os.getenv("MHC_API_TOKEN", "")


class MHCClientError(Exception):
    """MHC API 呼叫錯誤"""
    pass


class MHCOfflineError(MHCClientError):
    """PC 端離線"""
    pass


class MHCBusyError(MHCClientError):
    """PC 端忙碌（LLM unavailable）"""
    pass


async def ask_mhc(question: str, user_name: str, timeout: int = 120) -> dict:
    """呼叫 PC 端 MHC Backend /ask

    Returns:
        {"html": "...", "hcs_used": [...], "biases_detected": [...]}

    Raises:
        MHCOfflineError: PC 端無回應
        MHCBusyError: LLM 忙碌中
        MHCClientError: 其他錯誤
    """
    url = f"{MHC_API_URL}/ask"

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post(
                url,
                json={"question": question, "user_name": user_name},
                headers={
                    "Authorization": f"Bearer {MHC_API_TOKEN}",
                    "Content-Type": "application/json",
                },
            )

            if response.status_code == 200:
                return response.json()

            elif response.status_code == 401:
                logger.error(f"MHC API auth failed: {response.text}")
                raise MHCClientError("內部 API 驗證失敗，請聯繫管理員")

            elif response.status_code == 503:
                logger.warning("MHC API returned busy signal")
                raise MHCBusyError("分析引擎忙碌中，請 5 分鐘後再試")

            else:
                data = response.json()
                error_msg = data.get("message", data.get("detail", "未知錯誤"))
                logger.error(f"MHC API error {response.status_code}: {error_msg}")

                if error_msg == "LLM_UNAVAILABLE" or "忙碌" in error_msg:
                    raise MHCBusyError("分析引擎忙碌中，請 5 分鐘後再試")
                else:
                    raise MHCClientError(f"MHC API 錯誤: {error_msg}")

    except httpx.TimeoutException:
        logger.error(f"MHC API timeout after {timeout}s")
        raise MHCOfflineError("分析引擎無回應，請稍後再試")

    except (httpx.ConnectError, httpx.ConnectTimeout):
        logger.error(f"MHC API connection failed: {url}")
        raise MHCOfflineError("分析引擎目前離線，請稍後再試")


async def check_mhc_health(timeout: int = 10) -> dict:
    """檢查 PC 端 MHC Backend 健康狀態"""
    url = f"{MHC_API_URL}/health"
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(url)
            return response.json()
    except Exception as e:
        return {"status": "unreachable", "error": str(e)}

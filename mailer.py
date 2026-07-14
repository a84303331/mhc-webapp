"""MHC WebApp — 郵件模組

使用 Gmail API + OAuth 寄送系統通知信。
共用 Hermes 既有的 Gmail OAuth token。
"""

import os
import base64
import logging
from email.mime.text import MIMEText
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# Gmail OAuth 設定（從 Hermes config 共用）
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "hsiachisheng@gmail.com")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REFRESH_TOKEN = os.getenv("GOOGLE_REFRESH_TOKEN", "")


async def _get_access_token() -> str:
    """用 refresh token 換 access token"""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "refresh_token": GOOGLE_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
        )
        data = resp.json()
        return data["access_token"]


async def send_email(to: str, subject: str, body_html: str) -> bool:
    """透過 Gmail API 寄送 HTML 郵件

    Returns:
        True if sent successfully
    """
    if not all([GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN]):
        logger.warning("Gmail OAuth not configured, skipping email")
        return False

    try:
        access_token = await _get_access_token()

        message = MIMEText(body_html, "html", "utf-8")
        message["To"] = to
        message["From"] = GMAIL_ADDRESS
        message["Subject"] = subject

        raw = base64.urlsafe_b64encode(message.as_bytes()).decode()

        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json={"raw": raw},
            )

            if resp.status_code == 200:
                logger.info(f"Email sent to {to}: {subject}")
                return True
            else:
                logger.error(f"Gmail API error: {resp.status_code} {resp.text}")
                return False

    except Exception as e:
        logger.error(f"Failed to send email to {to}: {e}")
        return False


async def send_verification_email(to: str, name: str, token: str) -> bool:
    """寄送郵箱驗證信"""
    verify_url = f"https://mhc.summer-hsia.com/verify-email?token={token}"
    html = f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;color:#e0e0e0;background:#1a1a2e;padding:2rem;border-radius:8px;">
        <h2 style="color:#7c3aed;">🧠 MHC 郵箱驗證</h2>
        <p>{name} 你好，</p>
        <p>感謝註冊 Minerva HC Toolbox。</p>
        <p>請點擊下方按鈕完成郵箱驗證（24 小時內有效）：</p>
        <p style="text-align:center;margin:2rem 0;">
            <a href="{verify_url}" style="background:#7c3aed;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:16px;">
                驗證郵箱
            </a>
        </p>
        <p style="color:#a0a0b0;font-size:12px;">如果按鈕無效，請複製此連結：<br>{verify_url}</p>
    </div>
    """
    return await send_email(to, "[MHC] 請驗證你的郵箱", html)


async def send_password_reset_email(to: str, name: str, token: str) -> bool:
    """寄送密碼重設信"""
    reset_url = f"https://mhc.summer-hsia.com/reset-password?token={token}"
    html = f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif;color:#e0e0e0;background:#1a1a2e;padding:2rem;border-radius:8px;">
        <h2 style="color:#7c3aed;">🔑 MHC 密碼重設</h2>
        <p>{name} 你好，</p>
        <p>我們收到你的密碼重設請求。</p>
        <p>請點擊下方按鈕重設密碼（1 小時內有效）：</p>
        <p style="text-align:center;margin:2rem 0;">
            <a href="{reset_url}" style="background:#7c3aed;color:white;padding:12px 24px;border-radius:6px;text-decoration:none;font-size:16px;">
                重設密碼
            </a>
        </p>
        <p style="color:#a0a0b0;font-size:12px;">如果你沒有要求重設密碼，請忽略此信。</p>
    </div>
    """
    return await send_email(to, "[MHC] 密碼重設請求", html)


async def send_analysis_email(to: str, name: str, question: str, html_result: str) -> bool:
    """寄送分析結果郵件（簡版純文字）"""
    # 純文字簡版
    text_body = f"""
{name} 你好，

你的 MHC 分析已完成：

問題：{question[:100]}{'...' if len(question) > 100 else ''}

完整分析請至 MHC 網站查看：https://mhc.summer-hsia.com

---
Minerva HC Toolbox
"""
    return await send_email(to, "[MHC] 你的分析結果已就緒", text_body)

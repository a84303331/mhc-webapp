"""MHC WebApp — FastAPI 主程式

Railway 端 Web App：會員註冊/登入、問答介面、管理後台。
透過 Cloudflare Tunnel 呼叫 PC 端 MHC Backend。
"""

import os
import logging
from datetime import datetime, timedelta, date
from typing import Optional

from fastapi import FastAPI, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from dotenv import load_dotenv
import structlog

from database import get_db, init_db
from models import User, DailyQuestionCount, Feedback
from auth import (
    register_user, authenticate_user, verify_email,
    create_access_token, create_refresh_token,
    get_current_user, validate_password_strength, hash_password,
)
from mhc_client import ask_mhc, MHCOfflineError, MHCBusyError
from mailer import send_verification_email, send_password_reset_email, send_analysis_email
import httpx
from health_monitor import start_scheduler, get_status

# ── 新增：安全層 imports ────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

load_dotenv()

# ── Logging ─────────────────────────────────
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.JSONRenderer(),
    ]
)
logger = structlog.get_logger()

# ── App ─────────────────────────────────────
app = FastAPI(title="MHC WebApp", version="0.1.0")

# ── Rate Limiter ────────────────────────────
limiter = Limiter(key_func=get_remote_address)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

# ── Static files ────────────────────────────
app.mount("/static", StaticFiles(directory="static"), name="static")

# ── XSS Filter ──────────────────────────────
ALLOWED_TAGS = [
    "div", "section", "h1", "h2", "h3", "h4", "p", "span", "br", "hr",
    "ul", "ol", "li", "a", "strong", "em", "code", "pre", "blockquote",
    "table", "thead", "tbody", "tr", "th", "td",
    "img", "style", "header", "footer", "main", "nav",
    "body", "html", "head", "meta", "title", "link",
]
ALLOWED_ATTRS = {
    "*": ["class", "id", "style"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "width", "height"],
}


def sanitize_html(html: str) -> str:
    """過濾 LLM 產出 HTML，只移除 <script> 和事件處理器，保留所有結構標籤"""
    import re
    # 移除 <script>...</script>
    html = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    # 移除 on* 事件屬性
    html = re.sub(r'\s+on\w+\s*=\s*["\'][^"\']*["\']', '', html, flags=re.IGNORECASE)
    html = re.sub(r"\s+on\w+\s*=\s*['][^']*[']", '', html, flags=re.IGNORECASE)
    return html


# ── Turnstile 驗證 ──────────────────────────
TURNSTILE_VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"


async def verify_turnstile(token: str) -> bool:
    """驗證 Cloudflare Turnstile token，成功回傳 True"""
    if not token:
        return False
    secret = os.getenv("TURNSTILE_SECRET_KEY", "")
    if not secret:
        logger.error("turnstile_secret_not_configured")
        return False
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                TURNSTILE_VERIFY_URL,
                data={"secret": secret, "response": token},
                timeout=10.0,
            )
            data = resp.json()
            return data.get("success", False)
    except Exception as e:
        logger.error("turnstile_verify_failed", error=str(e))
        return False


# ── CSP Middleware ──────────────────────────
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' https://challenges.cloudflare.com; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "frame-src 'self' blob: https://challenges.cloudflare.com; "
        "frame-ancestors 'self';"
    )
    return response


# ── 全域錯誤捕捉 ────────────────────────────
@app.exception_handler(500)
async def internal_error_handler(request: Request, exc: Exception):
    logger.error("internal_error", error=str(exc), path=str(request.url))
    return JSONResponse(
        status_code=500,
        content={"error": "Internal server error"},
    )


# ── 常用 helper ─────────────────────────────
async def get_daily_usage(user: User, db: AsyncSession) -> int:
    """取得使用者今日提問次數"""
    today = date.today()
    result = await db.execute(
        select(DailyQuestionCount).where(
            DailyQuestionCount.user_id == user.id,
            DailyQuestionCount.date == today,
        )
    )
    row = result.scalar_one_or_none()
    return row.count if row else 0


async def increment_daily_usage(user: User, db: AsyncSession):
    """增加使用者今日提問次數"""
    today = date.today()
    result = await db.execute(
        select(DailyQuestionCount).where(
            DailyQuestionCount.user_id == user.id,
            DailyQuestionCount.date == today,
        )
    )
    row = result.scalar_one_or_none()
    if row:
        row.count += 1
        row.last_question_at = datetime.utcnow()
    else:
        row = DailyQuestionCount(user_id=user.id, date=today, count=1, last_question_at=datetime.utcnow())
        db.add(row)
    await db.commit()


# ── 基礎 CSS（inline，減少外部請求）─────────
BASE_CSS = """
:root {
    --bg: #0f0f1a; --card-bg: #1a1a2e; --text: #e0e0e0;
    --text-secondary: #a0a0b0; --accent: #7c3aed; --border: #2a2a3e;
    --success: #10b981; --danger: #ef4444; --warning: #f59e0b;
}
* { margin:0; padding:0; box-sizing:border-box; }
body { font-family:-apple-system,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; }
.container { max-width:800px; margin:0 auto; padding:2rem 1rem; }
.card { background:var(--card-bg); border:1px solid var(--border); border-radius:12px; padding:2rem; margin:2rem 0; }
h1 { color:var(--accent); text-align:center; margin-bottom:1rem; }
h2 { color:var(--accent); margin-bottom:1rem; }
a { color:var(--accent); text-decoration:none; }
a:hover { text-decoration:underline; }
input, textarea { width:100%; padding:0.75rem; margin:0.5rem 0; background:var(--bg); color:var(--text); border:1px solid var(--border); border-radius:8px; font-size:16px; }
textarea { min-height:140px; resize:vertical; font-size:16px; border:2px solid var(--border); transition:border-color 0.2s; }
textarea:focus { border-color:var(--accent); outline:none; box-shadow:0 0 0 3px var(--accent-glow); }
button, .btn { display:inline-block; padding:0.75rem 2rem; background:var(--accent); color:white; border:none; border-radius:8px; font-size:16px; cursor:pointer; text-align:center; }
button:hover, .btn:hover { opacity:0.9; text-decoration:none; }
button:disabled { opacity:0.5; cursor:not-allowed; }
.nav { display:flex; justify-content:space-between; align-items:center; padding:1rem 2rem; background:var(--card-bg); border-bottom:1px solid var(--border); }
.nav .logo { font-size:1.2rem; color:var(--accent); font-weight:bold; }
.nav .user-info { color:var(--text-secondary); }
.alert { padding:1rem; border-radius:8px; margin:1rem 0; }
.alert-error { background:rgba(239,68,68,0.1); border:1px solid var(--danger); color:var(--danger); }
.alert-success { background:rgba(16,185,129,0.1); border:1px solid var(--success); color:var(--success); }
.alert-info { background:rgba(124,58,237,0.1); border:1px solid var(--accent); color:var(--accent); }
.form-group { margin-bottom:1rem; }
.form-group label { display:block; margin-bottom:0.25rem; color:var(--text-secondary); font-size:0.9rem; }
.text-center { text-align:center; }
.mt-2 { margin-top:2rem; }
.mb-1 { margin-bottom:1rem; }
.pw-requirements { font-size:0.8rem; color:var(--text-secondary); margin-top:0.25rem; }
.pw-requirements .met { color:var(--success); }
.pw-requirements .unmet { color:var(--danger); }
.prompt-intro { color:var(--text-secondary); line-height:1.8; margin-bottom:1.5rem; }
.prompt-intro p { margin-bottom:0.75rem; }
.chat-warning { color:var(--warning) !important; font-weight:bold; border-left:3px solid var(--warning); padding-left:0.75rem; }
.feedback-section { margin-top:2rem; padding:1.5rem; background:var(--card-bg); border:1px solid var(--border); border-radius:12px; }
.feedback-section h3 { color:var(--accent); margin-bottom:0.75rem; font-size:1rem; }
.feedback-row { display:flex; justify-content:space-between; align-items:center; padding:0.4rem 0; border-bottom:1px solid rgba(255,255,255,0.05); }
.feedback-row:last-child { border-bottom:none; }
.feedback-label { color:var(--text-secondary); font-size:0.85rem; min-width:100px; }
.star-rating { display:inline-flex; gap:4px; }
.star-rating .star { font-size:1.3rem; cursor:pointer; color:#555; transition:all 0.15s; display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; user-select:none; -webkit-user-select:none; -webkit-tap-highlight-color:transparent; }
.star-rating .star.active, .star-rating .star:hover { color:#f59e0b; transform:scale(1.15); }
.feedback-submit { margin-top:1rem; text-align:right; }
.feedback-thanks { display:none; color:var(--success); text-align:center; padding:0.5rem; }
.feedback-msg { display:none; }
"""


# ── Routes ──────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/login")


# ── 隱私政策 ────────────────────────────────
@app.get("/privacy", response_class=HTMLResponse)
async def privacy_page():
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>隱私政策 — MHC</title><style>{BASE_CSS}</style></head>
    <body><div class="container" style="max-width:750px;"><div class="card">
    <h1>🔒 隱私政策</h1>
    <p style="color:var(--text-secondary);">最後更新：2026 年 7 月 14 日</p>

    <h2>1. 我們收集的資料</h2>
    <ul><li><strong>帳號資訊</strong>：姓名、電子郵件地址（註冊時提供）</li>
    <li><strong>使用記錄</strong>：提問內容、分析結果、每日使用次數</li>
    <li><strong>反饋資料</strong>：你對分析結果的星級評分</li></ul>

    <h2>2. 資料用途</h2>
    <ul><li>提供 MHC 思維分析服務</li>
    <li>改善分析品質與使用者體驗</li>
    <li>每日使用量統計（僅管理員可見）</li>
    <li>系統安全與濫用防護</li></ul>

    <h2>3. 資料儲存與保護</h2>
    <ul><li>所有資料儲存在 Railway（美國東部）的 PostgreSQL 資料庫</li>
    <li>密碼使用 bcrypt 單向雜湊，無法反向還原</li>
    <li>傳輸過程使用 HTTPS 加密</li>
    <li>分析引擎運行於本地 PC，提問內容離開 Railway 後僅在本地處理</li></ul>

    <h2>4. 第三方服務</h2>
    <p>MHC 使用以下第三方服務：</p>
    <ul><li><strong>Cloudflare Turnstile</strong>：註冊時的人機驗證</li>
    <li><strong>DeepSeek API</strong>：AI 分析引擎（提問內容會傳送至 DeepSeek 進行分析）</li>
    <li><strong>Gmail API</strong>：系統通知郵件（郵箱驗證、密碼重設）</li></ul>

    <h2>5. 你的權利</h2>
    <ul><li>你可以隨時要求刪除帳號及所有相關資料</li>
    <li>你可以要求匯出你的個人資料</li>
    <li>請透過 MHC 網站管理員聯繫我們</li></ul>

    <h2>6. Cookie</h2>
    <p>MHC 使用必要的 session cookie（access_token / refresh_token）來維持登入狀態。不使用追蹤型或廣告型 cookie。</p>

    <h2>7. 政策更新</h2>
    <p>本政策可能隨服務更新而調整，重大變更時將透過電子郵件通知。</p>

    <p class="text-center mt-2"><a href="/login">返回登入頁</a></p>
    </div></div></body></html>""")


# ── 登入 ────────────────────────────────────
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, reset: str = ""):
    msg = ""
    if reset:
        msg = '<div class="alert alert-success">密碼已重設，請使用新密碼登入</div>'
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>MHC 登入</title><style>{BASE_CSS}</style></head>
    <body>
    <div class="container">
        <h1>🧠 Minerva HC Toolbox</h1>
        <div class="card">
            <h2>登入</h2>
            {msg}
            <form method="POST" action="/api/auth/login">
                <div class="form-group">
                    <label>郵箱</label>
                    <input type="email" name="email" required>
                </div>
                <div class="form-group">
                    <label>密碼</label>
                    <input type="password" name="password" required>
                </div>
                <button type="submit" style="width:100%">登入</button>
            </form>
            <p class="text-center mt-2">
                <a href="/register">註冊新帳號</a> ·
                <a href="/forgot-password">忘記密碼？</a> ·
                <a href="/privacy">隱私政策</a>
            </p>
        </div>
    </div>
    </body></html>
    """)


@app.post("/api/auth/login")
@limiter.limit("5/minute")
async def login(
    request: Request,
    email: str = Form(...),
    password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    user = await authenticate_user(db, email, password)
    if not user:
        return HTMLResponse("""
        <!DOCTYPE html><html lang="zh-TW">
        <head><meta charset="UTF-8"><style>""" + BASE_CSS + """</style></head>
        <body><div class="container"><div class="card">
        <h2>登入失敗</h2>
        <div class="alert alert-error">帳號或密碼錯誤</div>
        <a href="/login">返回登入</a>
        </div></div></body></html>
        """, status_code=401)

    if not user.is_active:
        return HTMLResponse(f"""
        <!DOCTYPE html><html lang="zh-TW">
        <head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card">
        <h2>帳號已停用</h2>
        <div class="alert alert-error">此帳號已被管理員停用，請聯繫 hsiachisheng@gmail.com</div>
        <a href="/login">返回登入</a>
        </div></div></body></html>
        """, status_code=403)

    # 產生 token
    access_token = create_access_token(user.id, user.email)
    refresh_token = create_refresh_token()

    # 儲存 refresh token
    user.refresh_token = refresh_token
    await db.commit()

    resp = RedirectResponse(url="/ask", status_code=303)
    resp.set_cookie("access_token", access_token, httponly=True, samesite="lax", max_age=900)
    resp.set_cookie("refresh_token", refresh_token, httponly=True, samesite="lax", max_age=604800)
    return resp


# ── 註冊 ────────────────────────────────────
@app.get("/register", response_class=HTMLResponse)
async def register_page():
    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>MHC 註冊</title>
    <style>{BASE_CSS}</style>
    <script src="https://challenges.cloudflare.com/turnstile/v0/api.js" async defer></script>
    </head>
    <body>
    <div class="container">
        <h1>🧠 註冊 MHC</h1>
        <p style="text-align:center;color:var(--text-secondary);font-size:0.9rem;margin-bottom:1rem;">如果您担心隱私問題，可以使用匿名以及用一個別人不知是您的郵箱，只要您能收到確認郵件即可。</p>
        <div class="card">
            <form method="POST" action="/api/auth/register" id="register-form">
                <div class="form-group">
                    <label>姓名</label>
                    <input type="text" name="name" required>
                </div>
                <div class="form-group">
                    <label>郵箱</label>
                    <input type="email" name="email" required>
                </div>
                <div class="form-group">
                    <label>密碼</label>
                    <input type="password" name="password" id="password" required
                           oninput="checkPassword()">
                    <div class="pw-requirements">
                        <span id="req-length" class="unmet">至少 8 字元</span> ·
                        <span id="req-upper" class="unmet">至少 1 大寫</span> ·
                        <span id="req-digit" class="unmet">至少 1 數字</span>
                    </div>
                </div>
                <div class="form-group">
                    <label>確認密碼</label>
                    <input type="password" name="confirm_password" required>
                </div>
                <div class="cf-turnstile" data-sitekey="{os.getenv('TURNSTILE_SITE_KEY', '')}"></div>
                <button type="submit" style="width:100%;margin-top:1rem">註冊</button>
            </form>
            <p class="text-center mt-2"><a href="/login">已有帳號？登入</a></p>
        </div>
    </div>
    <script>
    function checkPassword() {{
        var p = document.getElementById('password').value;
        document.getElementById('req-length').className = p.length >= 8 ? 'met' : 'unmet';
        document.getElementById('req-upper').className = /[A-Z]/.test(p) ? 'met' : 'unmet';
        document.getElementById('req-digit').className = /[0-9]/.test(p) ? 'met' : 'unmet';
    }}
    </script>
    </body></html>
    """)


@app.post("/api/auth/register")
@limiter.limit("3/minute")
async def register(
    request: Request,
    name: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    cf_turnstile_response: str = Form("", alias="cf-turnstile-response"),
    db: AsyncSession = Depends(get_db),
):
    # Turnstile 驗證（必須先過）
    if not await verify_turnstile(cf_turnstile_response):
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>註冊失敗</h2>
        <div class="alert alert-error">安全驗證失敗，請重新整理頁面後再試</div><a href="/register">返回</a></div></div></body></html>""", status_code=400)

    if password != confirm_password:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>註冊失敗</h2>
        <div class="alert alert-error">兩次密碼不一致</div><a href="/register">返回</a></div></div></body></html>""", status_code=400)

    pw_error = validate_password_strength(password)
    if pw_error:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>註冊失敗</h2>
        <div class="alert alert-error">{pw_error}</div><a href="/register">返回</a></div></div></body></html>""", status_code=400)

    try:
        user = await register_user(db, name, email, password)
        # 寄送驗證信
        await send_verification_email(email, name, user.email_verify_token)
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>註冊成功 🎉</h2>
        <div class="alert alert-success">驗證信已寄至 {email}，請在 24 小時內點擊連結完成驗證（郵件可能會滙入到垃圾桶）</div>
        <p class="text-center mt-2"><a href="/login">前往登入</a></p></div></div></body></html>""")
    except HTTPException as e:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>註冊失敗</h2>
        <div class="alert alert-error">{e.detail}</div><a href="/register">返回</a></div></div></body></html>""", status_code=400)


# ── 郵箱驗證 ────────────────────────────────
@app.get("/verify-email", response_class=HTMLResponse)
async def verify_email_page(token: str, db: AsyncSession = Depends(get_db)):
    success = await verify_email(db, token)
    if success:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>驗證成功 ✅</h2>
        <div class="alert alert-success">郵箱驗證完成，現在可以登入了</div>
        <p class="text-center mt-2"><a href="/login">前往登入</a></p></div></div></body></html>""")
    else:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>驗證失敗 ❌</h2>
        <div class="alert alert-error">驗證連結已失效或無效</div>
        <a href="/login">返回登入</a></div></div></body></html>""")


# ── 忘記密碼 ────────────────────────────────
@app.get("/forgot-password", response_class=HTMLResponse)
async def forgot_password_page():
    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
    <body><div class="container"><div class="card"><h2>忘記密碼</h2>
    <form method="POST" action="/api/auth/forgot-password">
        <div class="form-group"><label>註冊郵箱</label><input type="email" name="email" required></div>
        <button type="submit" style="width:100%">寄送重設連結</button>
    </form>
    <p class="text-center mt-2"><a href="/login">返回登入</a></p></div></div></body></html>""")


@app.post("/api/auth/forgot-password")
@limiter.limit("3/minute")
async def forgot_password(
    request: Request,
    email: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    # 安全考量：不論信箱是否存在，顯示相同訊息
    if user:
        import uuid
        token = uuid.uuid4().hex
        user.password_reset_token = token
        user.password_reset_token_expires = datetime.utcnow() + timedelta(hours=1)
        await db.commit()
        await send_password_reset_email(email, user.name, token)

    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
    <body><div class="container"><div class="card"><h2>重設連結已寄出</h2>
    <div class="alert alert-info">若此信箱已註冊，重設連結已寄出（1 小時內有效）</div>
    <a href="/login">返回登入</a></div></div></body></html>""")


# ── 重設密碼 ────────────────────────────────
@app.get("/reset-password", response_class=HTMLResponse)
async def reset_password_page(token: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(User).where(
            User.password_reset_token == token,
            User.password_reset_token_expires > datetime.utcnow(),
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>連結已失效</h2>
        <div class="alert alert-error">重設連結已過期，請重新申請</div>
        <a href="/forgot-password">重新申請</a></div></div></body></html>""")

    return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
    <body><div class="container"><div class="card"><h2>重設密碼</h2>
    <form method="POST" action="/api/auth/reset-password">
        <input type="hidden" name="token" value="{token}">
        <div class="form-group"><label>新密碼</label><input type="password" name="password" required></div>
        <div class="form-group"><label>確認密碼</label><input type="password" name="confirm_password" required></div>
        <button type="submit" style="width:100%">重設密碼</button>
    </form></div></div></body></html>""")


@app.post("/api/auth/reset-password")
async def reset_password(
    token: str = Form(...),
    password: str = Form(...),
    confirm_password: str = Form(...),
    db: AsyncSession = Depends(get_db),
):
    if password != confirm_password:
        return HTMLResponse(f"""...密碼不一致...""", status_code=400)

    pw_error = validate_password_strength(password)
    if pw_error:
        return HTMLResponse(f"...{pw_error}...", status_code=400)

    result = await db.execute(
        select(User).where(
            User.password_reset_token == token,
            User.password_reset_token_expires > datetime.utcnow(),
        )
    )
    user = result.scalar_one_or_none()
    if not user:
        return HTMLResponse(f"""...連結已失效...""", status_code=400)

    user.password_hash = hash_password(password)
    user.password_reset_token = None
    user.password_reset_token_expires = None
    await db.commit()

    return RedirectResponse(url="/login?reset=1", status_code=303)


# ── 問答頁 ──────────────────────────────────
@app.get("/ask", response_class=HTMLResponse)
async def ask_page(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    daily_used = await get_daily_usage(current_user, db)
    limit = current_user.daily_limit or 999
    remaining = max(0, limit - daily_used) if limit > 0 else 999

    admin_link = ' · <a href="/admin">👑 管理後台</a>' if current_user.is_admin else ""

    return HTMLResponse(f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
    <title>MHC 問答</title>
    <style>{BASE_CSS}
    .example-card {{ background:var(--bg); border:1px solid var(--border); border-radius:8px; padding:0.75rem; margin:0.5rem 0; cursor:pointer; color:var(--text-secondary); font-size:0.9rem; }}
    .example-card:hover {{ border-color:var(--accent); color:var(--text); }}
    #result-area {{ margin-top:2rem; }}
    #result-area iframe {{ width:100%; min-height:500px; border:1px solid var(--border); border-radius:8px; background:white; }}
    .char-count {{ font-size:0.8rem; color:var(--text-secondary); text-align:right; }}
    </style></head>
    <body>
    <div class="nav">
        <span class="logo">🧠 MHC</span>
        <span class="user-info">
            {current_user.name} · <span id="usage-display">今日 {daily_used}/{limit if limit > 0 else '∞'} 次</span>{admin_link} ·
            <a href="/logout">登出</a>
        </span>
    </div>
    <div class="container">
        <div class="card">
            <div class="prompt-intro">
            <p>歡迎使用 MHC 思考分析平台。</p>
            <p>這裡沒有閒聊，只有深度洞察。您的每一個問題，都將同時經過兩大引擎的交叉比對：<strong>密涅瓦大學 80 個思考習慣</strong>，為您匹配最適合的思考框架；<strong>人類 188 種行為偏誤</strong>，為您揪出潛藏的認知盲點。</p>
            <p>無論是職場決策、人生規劃、溝通困境、學習瓶頸，或是複雜系統的策略難題——只要您帶著具體問題而來，我們就為您提供精闢、多維度、可直接行動的分析回覆。</p>
            <p class="chat-warning">⚠️ 這不是一個聊天機器人。為了確保每一份回覆的深度與品質，請以需要分析或策略方向的問題為主，謝謝您的理解。</p>
            </div>

            <form id="ask-form" method="POST" action="/api/ask">
                <label for="question-input" style="display:block; font-weight:bold; font-size:1.05rem; margin-bottom:0.5rem; color:var(--text);">✍️ 你的問題</label>
                <textarea name="question" id="question-input" rows="5" placeholder="請在這裏描述你的困境，至少 15 個字...&#10;&#10;例如：客戶在颱風天要求破例上廣告，我該怎麼回應才能守住原則又不傷關係？" oninput="updateCharCount()"></textarea>
                <div class="char-count"><span id="char-count">0</span> / 15 字</div>
                <button type="submit" id="submit-btn" disabled style="width:100%">提交分析</button>
            </form>

            <div style="margin-top:1rem;">
                <p style="color:var(--text-secondary);font-size:0.9rem;">💡 不確定怎麼問？試點擊這些範例：</p>
                <div class="example-card" onclick="fillExample('客戶在颱風天要求破例上廣告，我該怎麼回應才能守住原則又不傷關係？')">
                客戶在颱風天要求破例上廣告，我該怎麼回應才能守住原則又不傷關係？
                </div>
                <div class="example-card" onclick="fillExample('我同時想學程式設計和 UX 設計，但每天只有兩小時，該如何分配？')">
                我同時想學程式設計和 UX 設計，但每天只有兩小時，該如何分配？
                </div>
                <div class="example-card" onclick="fillExample('你即將舉辦年度會議，並在過去六年一直使用的飯店舉辦。你已經印好邀請函，並與嘉賓、講者和附近餐廳協調好了。會議前一週，你收到飯店經理的信，通知你他必須收取去年三倍的費用。你考慮了對每個利害關係人堅持在原飯店舉辦會議的成本和效益：對你自己而言，成本是嚴重的——你無法支付開支；效益是你熟悉場地，不必重做所有邀請和安排。對與會者而言，成本是更高的註冊費，效益是安排清晰和熟悉的場地。對飯店經理而言，成本包括失去你這個客戶，效益是增加的收入——僅限於這次即將舉行的會議（你肯定不會在接下來幾年在那裡開會）。將所有這些呈現給飯店經理，結果是一次有用的談判，他只將金額提高了 50%。')">
                🆕 飯店合約談判——六年合作、三倍漲價，如何與飯店經理協商？
                </div>
            </div>
        </div>

        <div id="result-area"></div>
    </div>
    <script>
    function updateCharCount() {{
        var len = document.getElementById('question-input').value.length;
        document.getElementById('char-count').textContent = len;
        document.getElementById('submit-btn').disabled = len < 15;
    }}
    function fillExample(text) {{
        document.getElementById('question-input').value = text;
        updateCharCount();
    }}
    document.getElementById('ask-form').addEventListener('submit', function(e) {{
        e.preventDefault();
        var btn = document.getElementById('submit-btn');
        if (btn.disabled) return;
        btn.disabled = true;
        btn.textContent = '分析中……可能需要花費 1-3 分鐘，請耐心等待，謝謝';

        var formData = new FormData(this);
        fetch('/api/ask', {{ method:'POST', body:formData }})
            .then(r => r.text())
            .then(html => {{
                var resultDiv = document.getElementById('result-area');
                resultDiv.innerHTML = html;
                resultDiv.scrollIntoView({{ behavior: 'smooth' }});
                btn.disabled = false;
                btn.textContent = '提交分析';
                // 更新每日次數（不重整頁面）
                fetch('/api/usage')
                    .then(r => r.json())
                    .then(data => {{
                        if (data.used !== undefined) {{
                            document.getElementById('usage-display').textContent =
                                '今日 ' + data.used + '/' + (data.limit > 0 ? data.limit : '∞') + ' 次';
                        }}
                    }});
            }})
            .catch(err => {{
                document.getElementById('result-area').innerHTML =
                    '<div class="alert alert-error">錯誤: ' + err.message + '</div>';
                btn.disabled = false;
                btn.textContent = '提交分析';
            }});
    }});
    </script>
    </body></html>
    """)


@app.post("/api/ask", response_class=HTMLResponse)
async def ask(
    question: str = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # 字數檢查
    if len(question.strip()) < 15:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="alert alert-error">問題至少需要 15 個中文字。</div></div></body></html>""")

    # 聊天拒絕
    chat_patterns = ["你好", "嗨", "哈囉", "hello", "hi", "早安", "晚安", "謝謝", "thank"]
    if question.strip().lower() in chat_patterns or len(question.strip()) < 20 and any(cp in question.strip().lower() for cp in chat_patterns):
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card">
        <div class="prompt-intro">
        <p>感謝您的來訊。MHC 是一個專注於思考分析的平台，並非聊天機器人——我不具備閒聊功能，但我非常樂意為您處理需要深度分析的問題。</p>
        <p>若您在以下領域有具體的策略或方向需要分析，歡迎隨時提出：</p>
        <ul style="color:var(--text-secondary); margin-left:1.5rem; line-height:1.8;">
            <li>職場決策與談判困境</li>
            <li>學習瓶頸與自學路徑規劃</li>
            <li>溝通障礙與人際衝突</li>
            <li>複雜系統與長期策略思考</li>
            <li>任何您感覺「卡住了」或「需要另一雙眼睛」的問題</li>
        </ul>
        <p style="margin-top:1rem;">期待您提出具體的問題，讓我為您提供真正有價值的思考框架。</p>
        </div>
        <p class="text-center mt-2"><a href="/ask">← 返回問答頁</a></p>
        </div></div></body></html>""")

    # 每日限額檢查
    limit = current_user.daily_limit or 999
    if limit > 0:
        daily_used = await get_daily_usage(current_user, db)
        if daily_used >= limit:
            return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
            <body><div class="container"><div class="card"><h2>今日額度已用完 🛑</h2>
            <div class="alert alert-info">你今天的 {limit} 次提問已用完，請明天再來。</div>
            <p class="text-center mt-2"><a href="/ask">返回問答頁</a></p></div></div></body></html>""")

    # 呼叫 PC MHC Backend
    try:
        import uuid
        case_id = f"MHC-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"

        result = await ask_mhc(question, current_user.name, case_id)
        html = result.get("html", "")

        # XSS 過濾
        html = sanitize_html(html)

        # 背景寄送分析結果郵件（不含反饋表單）
        import asyncio
        asyncio.create_task(
            send_analysis_email(current_user.email, current_user.name, case_id, question, html)
        )

        # 將反饋表單插入 </body> 之前（不在 </html> 之後）
        feedback_html = _feedback_form_html(case_id)
        if '</body>' in html:
            html = html.replace('</body>', feedback_html + '</body>')
        else:
            html += feedback_html

        # 增加每日次數
        await increment_daily_usage(current_user, db)

        logger.info("question_submitted", user_email=current_user.email, latency_ms=result.get("llm_latency_ms", 0))

        return HTMLResponse(html)

    except MHCBusyError:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>引擎忙碌中 🛑</h2>
        <div class="alert alert-info">分析引擎暫時忙碌中，請 5 分鐘後再試。</div>
        <p class="text-center mt-2"><a href="/ask">返回問答頁</a></p></div></div></body></html>""")

    except MHCOfflineError:
        return HTMLResponse(f"""<!DOCTYPE html><html lang="zh-TW"><head><meta charset="UTF-8"><style>{BASE_CSS}</style></head>
        <body><div class="container"><div class="card"><h2>分析引擎離線 🛑</h2>
        <div class="alert alert-info">分析引擎目前離線中，請稍後再試。<br>如果問題持續，請聯繫管理員。</div>
        <p class="text-center mt-2"><a href="/ask">返回問答頁</a></p></div></div></body></html>""")


# ── 登出 ────────────────────────────────────
@app.get("/logout")
async def logout(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    # 清除 refresh token
    current_user.refresh_token = None
    await db.commit()

    resp = RedirectResponse(url="/login")
    resp.delete_cookie("access_token")
    resp.delete_cookie("refresh_token")
    return resp

# ── 每日使用量 API ─────────────────────────
@app.get("/api/usage")
async def get_usage(current_user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)):
    """回傳當前使用者今日用量（給前端 AJAX 更新）"""
    daily_used = await get_daily_usage(current_user, db)
    limit = current_user.daily_limit or 999
    return {
        "used": daily_used,
        "limit": limit if limit > 0 else 0,  # 0 = 不限
        "remaining": max(0, limit - daily_used) if limit > 0 else 999,
    }


# ── 反饋表單輔助函式 ────────────────────────
def _feedback_form_html(case_id: str) -> str:
    """生成反饋表單 HTML + JS（五題 1-5 星評分）— 零 <script> 標籤版"""
    dimensions = [
        ("insight", "洞察有用性", "幫助我看到之前沒注意到的思考盲點"),
        ("clarity", "框架清晰度", "結構清楚、容易理解，HC 習慣的解釋很到位"),
        ("actionability", "行動可行性", "建議可以直接運用在實際情境中"),
        ("overall", "整體品質", "對分析的深度與品質感到滿意"),
        ("reuse_intent", "再使用意願", "願意再次使用 MHC 進行分析"),
    ]
    rows = ""
    for key, label, desc in dimensions:
        stars = "".join(
            f'<span class="star" data-value="{i}" data-dim="{key}" onclick="var r=document.querySelector(&#39;.star-rating[data-dim=&quot;{key}&quot;]&#39;);if(!r)return;r.querySelectorAll(&#39;.star&#39;).forEach(function(s){{s.classList.toggle(&#39;active&#39;,parseInt(s.dataset.value)<={i})}});var d=document.getElementById(&#39;feedback-ratings-{key}&#39;);if(!d){{d=document.createElement(&#39;input&#39;);d.type=&#39;hidden&#39;;d.id=&#39;feedback-ratings-{key}&#39;;d.name=&#39;{key}&#39;;document.getElementById(&#39;feedback-section&#39;).appendChild(d)}}d.value={i}">★</span>'
            for i in range(1, 6)
        )
        rows += f"""
        <div class="feedback-row" data-dim="{key}">
            <div class="feedback-label"><strong>{label}</strong><br><span class="feedback-desc">{desc}</span></div>
            <div class="star-rating" data-dim="{key}">{stars}</div>
        </div>"""

    # 提交按鈕的 onclick — 蒐集所有 hidden input 的值並 fetch
    submit_onclick = f"""var b=this;var m=document.getElementById('feedback-msg');var f=document.getElementById('feedback-section');var h=f.querySelectorAll('input[type=hidden]');if(h.length<5){{m.textContent='⚠️ 尚有 '+(5-h.length)+' 項未評分';m.style.cssText='display:block;color:#fbbf24;font-size:0.9rem;text-align:center;padding:0.5rem';return}}b.disabled=true;b.textContent='提交中...';var p=new URLSearchParams();p.append('case_id','{case_id}');h.forEach(function(i){{p.append(i.name,i.value)}});fetch('/api/feedback',{{method:'POST',headers:{{'Content-Type':'application/x-www-form-urlencoded'}},body:p}}).then(function(r){{if(r.ok){{f.querySelectorAll('.feedback-row,.feedback-submit').forEach(function(e){{e.style.display='none'}});m.innerHTML='感謝你的回饋！🙏<br><span style=font-size:0.8rem;color:#a0a0b0>我們將會把您的提問用郵件發給您</span>';m.style.cssText='display:block;color:#10b981;font-size:1.1rem;text-align:center;padding:1rem';var a=document.createElement('a');a.href='/ask';a.textContent='🔄 重新提問';a.style.cssText='display:inline-block;margin-top:0.75rem;padding:0.5rem 1.5rem;background:#7c3aed;color:#fff;border-radius:6px;text-decoration:none;font-size:0.9rem';m.appendChild(a)}}else{{b.disabled=false;b.textContent='提交評分';m.textContent='⚠️ 提交失敗 ('+r.status+')';m.style.cssText='display:block;color:#f87171;font-size:0.9rem;text-align:center;padding:0.5rem'}}}}).catch(function(){{b.disabled=false;b.textContent='提交評分';m.textContent='⚠️ 網路錯誤，請稍後再試';m.style.cssText='display:block;color:#f87171;font-size:0.9rem;text-align:center;padding:0.5rem'}})"""

    return f"""
<style>
.star-rating {{ display:inline-flex; gap:4px; }}
.star-rating .star {{ font-size:1.3rem; cursor:pointer; color:#555; transition:all 0.15s; display:inline-flex; align-items:center; justify-content:center; width:28px; height:28px; user-select:none; -webkit-user-select:none; -webkit-tap-highlight-color:transparent; }}
.star-rating .star.active, .star-rating .star:hover {{ color:#f59e0b; transform:scale(1.15); }}
.feedback-section {{ margin-top:2rem; padding:1.5rem; background:var(--card-bg); border:1px solid var(--border); border-radius:12px; }}
.feedback-section h3 {{ color:var(--accent); margin-bottom:0.75rem; font-size:1rem; }}
.feedback-row {{ display:flex; justify-content:space-between; align-items:center; padding:0.4rem 0; border-bottom:1px solid rgba(255,255,255,0.05); }}
.feedback-row:last-child {{ border-bottom:none; }}
.feedback-label {{ color:var(--text-secondary); font-size:0.85rem; min-width:100px; }}
.feedback-desc {{ color:#666; font-size:0.7rem; }}
.feedback-submit {{ margin-top:1rem; text-align:right; }}
.feedback-msg {{ display:none; }}
</style>
<div class="feedback-section" id="feedback-section">
    <h3>📊 這份分析對你有幫助嗎？</h3>
    {rows}
    <div class="feedback-msg" id="feedback-msg"></div>
    <div class="feedback-submit">
        <button class="btn btn-primary" onclick="{submit_onclick}" id="feedback-btn">提交評分</button>
    </div>
</div>"""


# ── 反饋提交 API ─────────────────────────────
@app.post("/api/feedback")
async def submit_feedback(
    case_id: str = Form(...),
    insight: int = Form(...),
    clarity: int = Form(...),
    actionability: int = Form(...),
    overall: int = Form(...),
    reuse_intent: int = Form(...),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """儲存使用者反饋"""
    valid = all(1 <= v <= 5 for v in [insight, clarity, actionability, overall, reuse_intent])
    if not valid:
        raise HTTPException(400, "評分必須在 1-5 之間")

    feedback = Feedback(
        case_id=case_id,
        user_id=current_user.id,
        insight=insight,
        clarity=clarity,
        actionability=actionability,
        overall=overall,
        reuse_intent=reuse_intent,
    )
    db.add(feedback)
    await db.commit()
    logger.info("feedback_saved", case_id=case_id, user=current_user.email)
    return {"status": "ok"}


# ── 管理後台 ────────────────────────────────
from admin import router as admin_router
app.include_router(admin_router)

# ── Startup (no DB dependency) ─────────────
@app.on_event("startup")
async def startup():
    """不依賴 DB 的啟動"""
    logger.info("mhc_webapp_started")
    scheduler = start_scheduler()

    # ── 每日超限報告（17:00）──
    from database import get_db

    async def daily_report():
        """17:00 查詢所有用戶今日用量並寄送管理員報告"""
        from models import User as UserModel
        from datetime import date, datetime as dt
        from mailer import send_email

        try:
            async for session in get_db():
                today = date.today()
                # 查詢所有已驗證用戶
                result = await session.execute(
                    select(UserModel).where(UserModel.is_verified == True)
                )
                users = result.scalars().all()

                rows = ""
                total = 0
                for u in users:
                    usage_result = await session.execute(
                        select(DailyQuestionCount).where(
                            DailyQuestionCount.user_id == u.id,
                            DailyQuestionCount.date == today,
                        )
                    )
                    usage = usage_result.scalar_one_or_none()
                    count = usage.count if usage else 0
                    total += count
                    limit = u.daily_limit or 999
                    icon = "⚠️" if (limit > 0 and count >= limit) else "✅"
                    rows += f"<tr><td>{u.name}</td><td>{u.email}</td><td style='text-align:center'>{count}</td><td style='text-align:center'>{limit if limit > 0 else '∞'}</td><td style='text-align:center'>{icon}</td></tr>"

                admin_email = os.getenv("ADMIN_EMAIL", "hsiachisheng@gmail.com")
                subject = f"[MHC] 每日用量報告 {today.strftime('%Y-%m-%d')}（總計 {total} 次）"
                html = f"""
                <div style="max-width:700px;margin:0 auto;font-family:sans-serif;color:#e0e0e0;background:#1a1a2e;padding:2rem;border-radius:8px;">
                    <h2 style="color:#7c3aed;">📊 MHC 每日用量報告</h2>
                    <p style="color:#a0a0b0;">{today.strftime('%Y 年 %m 月 %d 日')} — 總計 {total} 次提問</p>
                    <table style="width:100%;border-collapse:collapse;margin-top:1rem;">
                        <thead><tr style="border-bottom:1px solid #333;">
                            <th style="text-align:left;padding:8px;color:#a0a0b0;">使用者</th>
                            <th style="text-align:left;padding:8px;color:#a0a0b0;">信箱</th>
                            <th style="text-align:center;padding:8px;color:#a0a0b0;">今日用量</th>
                            <th style="text-align:center;padding:8px;color:#a0a0b0;">限額</th>
                            <th style="text-align:center;padding:8px;color:#a0a0b0;">狀態</th>
                        </tr></thead>
                        <tbody>{rows}</tbody>
                    </table>
                    <p style="color:#a0a0b0;font-size:12px;margin-top:1.5rem;">由 MHC 系統自動產生</p>
                </div>
                """
                await send_email(admin_email, subject, html)
                logger.info(f"daily_report_sent users={len(users)} total={total}")
                break  # get_async_session generator yields once
        except Exception as e:
            logger.error(f"daily_report_failed: {e}")

    scheduler.add_job(
        daily_report,
        "cron",
        hour=17,
        minute=0,
        id="daily_report",
        name="每日用量報告",
    )


# ── Health Check (no DB) ──────────────────
@app.get("/health")
async def health():
    """Railway 健康檢查端點 — 完全獨立，不做 DB 查詢"""
    return {"status": "ok", "service": "mhc-webapp"}


@app.get("/api/health/pc-status")
async def pc_health_status():
    """PC Backend 健康狀態（由 health_monitor 定時更新）"""
    s = get_status()
    return {
        "pc_online": s.online,
        "last_check": s.last_check.isoformat() if s.last_check else None,
        "last_ok": s.last_ok.isoformat() if s.last_ok else None,
        "last_error": s.last_error,
        "pc_uptime_seconds": s.uptime_seconds,
        "vault_accessible": s.vault_accessible,
        "consecutive_failures": s.consecutive_failures,
    }


# ── 全域 401 → 導向登入頁 ─────────────────
from starlette.exceptions import HTTPException as StarletteHTTPException

@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException):
    """401 時導向登入頁，其餘維持預設"""
    if exc.status_code == 401:
        return RedirectResponse(url="/login", status_code=303)
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )


# ── Main ────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")

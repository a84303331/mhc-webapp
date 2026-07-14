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
from mailer import send_verification_email, send_password_reset_email

# ── 新增：安全層 imports ────────────────────────
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
import bleach

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
]
ALLOWED_ATTRS = {
    "*": ["class", "id", "style"],
    "a": ["href", "target", "rel"],
    "img": ["src", "alt", "width", "height"],
}


def sanitize_html(html: str) -> str:
    """過濾 LLM 產出 HTML，移除危險標籤"""
    return bleach.clean(
        html,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRS,
        strip=True,
    )


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
        "frame-src https://challenges.cloudflare.com;"
    )
    return response


# ── Startup ─────────────────────────────────
@app.on_event("startup")
async def startup():
    await init_db()
    logger.info("mhc_webapp_started")


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
textarea { min-height:120px; resize:vertical; }
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
"""


# ── Routes ──────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def root():
    return RedirectResponse(url="/login")


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
                <a href="/forgot-password">忘記密碼？</a>
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
    db: AsyncSession = Depends(get_db),
):
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
        <div class="alert alert-success">驗證信已寄至 {email}，請在 24 小時內點擊連結完成驗證</div>
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
            {current_user.name} · 今日 {daily_used}/{limit if limit > 0 else '∞'} 次{admin_link} ·
            <a href="/logout">登出</a>
        </span>
    </div>
    <div class="container">
        <div class="card">
            <p style="color:var(--text-secondary);margin-bottom:1rem;line-height:1.6;">
            Minerva HC Toolbox 是一個 AI 輔助思考工具。<br>
            將你的困境告訴我——無論是決策、溝通、學習或創造瓶頸——<br>
            MHC 會匹配最適合的思考框架，提供結構化分析。<br>
            <strong>這不是聊天機器人</strong>，請以完整情境提問。
            </p>

            <form id="ask-form" method="POST" action="/api/ask">
                <textarea name="question" id="question-input" placeholder="描述你的困境，至少 15 個字...&#10;&#10;例如：客戶在颱風天要求破例上廣告，我該怎麼回應才能守住原則又不傷關係？" oninput="updateCharCount()"></textarea>
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
        btn.textContent = '分析中...';

        var formData = new FormData(this);
        fetch('/api/ask', {{ method:'POST', body:formData }})
            .then(r => r.text())
            .then(html => {{
                var iframe = document.createElement('iframe');
                iframe.srcdoc = html;
                document.getElementById('result-area').innerHTML = '';
                document.getElementById('result-area').appendChild(iframe);
                btn.disabled = false;
                btn.textContent = '提交分析';
                // 重新整理以更新每日次數
                setTimeout(() => location.reload(), 500);
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
        <body><div class="container"><div class="alert alert-info">👋 你好！MHC 是一個思考工具箱，不是聊天機器人。<br>請告訴我你遇到的困境，我會幫你找到對應的思考框架。</div></div></body></html>""")

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
        result = await ask_mhc(question, current_user.name)
        html = result.get("html", "")

        # XSS 過濾
        html = sanitize_html(html)

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


# ── 管理後台 ────────────────────────────────
from admin import router as admin_router
app.include_router(admin_router)


# ── Health Check ────────────────────────────
@app.get("/health")
async def health():
    """Railway 健康檢查端點"""
    return {"status": "ok", "service": "mhc-webapp"}


# ── Main ────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True, log_level="info")

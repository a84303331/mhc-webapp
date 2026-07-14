"""MHC WebApp — 管理後台

管理員可查看使用者列表、調整每日提問上限。
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User
from auth import get_admin_user, get_current_user

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin", tags=["admin"])


@router.get("", response_class=HTMLResponse)
async def admin_page(
    request: Request,
    bypass: str = "",
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """管理後台頁面"""
    # 非管理員檢查（也檢查 bypass key）
    import os
    bypass_key = os.getenv("ADMIN_BYPASS_KEY", "")
    if bypass and bypass_key and bypass == bypass_key:
        # 強制管理員 session
        current_user.is_admin = True
        await db.commit()

    if not current_user.is_admin:
        raise HTTPException(status_code=403, detail="僅管理員可存取")

    # 查詢所有使用者
    result = await db.execute(select(User).order_by(User.created_at.desc()))
    users = result.scalars().all()

    # 簡易 HTML 渲染（不用 Jinja2 模板，減少依賴）
    user_rows = ""
    for u in users:
        verified = "✅" if u.email_verified else "❌"
        admin_badge = "👑" if u.is_admin else ""
        user_rows += f"""
        <tr>
            <td>{u.id}</td>
            <td>{u.name} {admin_badge}</td>
            <td>{u.email} {verified}</td>
            <td>
                <form method="POST" action="/admin/update-limit" style="display:inline">
                    <input type="hidden" name="user_id" value="{u.id}">
                    <input type="number" name="daily_limit" value="{u.daily_limit}" min="0" max="999" style="width:60px">
                    <button type="submit">更新</button>
                </form>
            </td>
            <td>{u.created_at.strftime('%Y-%m-%d') if u.created_at else '-'}</td>
        </tr>
        """

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <title>MHC 管理後台</title>
        <style>
            :root {{
                --bg: #0f0f1a; --card-bg: #1a1a2e; --text: #e0e0e0;
                --accent: #7c3aed; --border: #2a2a3e;
            }}
            * {{ margin:0; padding:0; box-sizing:border-box; }}
            body {{ font-family:sans-serif; background:var(--bg); color:var(--text); padding:2rem; max-width:1000px; margin:0 auto; }}
            h1 {{ color:var(--accent); margin-bottom:1rem; }}
            table {{ width:100%; border-collapse:collapse; background:var(--card-bg); border-radius:8px; overflow:hidden; }}
            th, td {{ padding:0.75rem 1rem; text-align:left; border-bottom:1px solid var(--border); }}
            th {{ background:var(--accent); color:white; }}
            tr:hover {{ background:rgba(124,58,237,0.1); }}
            button {{ background:var(--accent); color:white; border:none; padding:4px 12px; border-radius:4px; cursor:pointer; }}
            button:hover {{ opacity:0.8; }}
            a {{ color:var(--accent); }}
            .nav {{ margin-bottom:1rem; }}
        </style>
    </head>
    <body>
        <div class="nav">
            <a href="/ask">← 回問答頁</a>
        </div>
        <h1>👑 MHC 管理後台</h1>
        <p>共 {len(users)} 位使用者</p>
        <table>
            <thead>
                <tr>
                    <th>ID</th><th>姓名</th><th>郵箱</th><th>每日上限</th><th>註冊日期</th>
                </tr>
            </thead>
            <tbody>
                {user_rows}
            </tbody>
        </table>
    </body>
    </html>
    """
    return HTMLResponse(html)


@router.post("/update-limit")
async def update_limit(
    user_id: int = Form(...),
    daily_limit: int = Form(...),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """更新使用者每日提問上限"""
    if daily_limit < 0:
        daily_limit = 0  # 0 = 不限

    await db.execute(
        update(User).where(User.id == user_id).values(daily_limit=daily_limit)
    )
    await db.commit()
    logger.info(f"Admin {current_user.email} updated user {user_id} daily_limit to {daily_limit}")
    return RedirectResponse(url="/admin", status_code=303)

"""MHC WebApp — 管理後台

管理員可查看使用者列表、調整每日提問上限。
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select, update, delete
from sqlalchemy.ext.asyncio import AsyncSession

from database import get_db
from models import User, Feedback
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
        active_badge = "🟢" if u.is_active else "🔴 已停用"
        user_rows += f"""
        <tr>
            <td>{u.id}</td>
            <td>{u.name} {admin_badge}</td>
            <td>{u.email} {verified}</td>
            <td>{active_badge}</td>
            <td>
                <form method="POST" action="/admin/update-limit" style="display:inline">
                    <input type="hidden" name="user_id" value="{u.id}">
                    <input type="number" name="daily_limit" value="{u.daily_limit}" min="0" max="999" style="width:60px">
                    <button type="submit">更新</button>
                </form>
            </td>
            <td style="white-space:nowrap">
                <form method="POST" action="/admin/toggle-admin" style="display:inline">
                    <input type="hidden" name="user_id" value="{u.id}">
                    <button type="submit" style="font-size:0.8rem;padding:2px 8px">{'取消管理員' if u.is_admin else '設為管理員'}</button>
                </form>
                <form method="POST" action="/admin/toggle-active" style="display:inline" onsubmit="return confirm('{'確定啟用' if not u.is_active else '確定停用'} {u.name}？')">
                    <input type="hidden" name="user_id" value="{u.id}">
                    <button type="submit" style="font-size:0.8rem;padding:2px 8px;background:{'var(--success)' if not u.is_active else 'var(--warning)'}">{'啟用' if not u.is_active else '停用'}</button>
                </form>
                <form method="POST" action="/admin/delete-user" style="display:inline" onsubmit="return confirm('確定刪除 {u.name}？此操作無法復原！')">
                    <input type="hidden" name="user_id" value="{u.id}">
                    <button type="submit" style="font-size:0.8rem;padding:2px 8px;background:var(--danger)">刪除</button>
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
            <a href="/ask">← 回問答頁</a> · <a href="/admin/feedback">📊 反饋評分</a>
        </div>
        <h1>👑 MHC 管理後台</h1>
        <p>共 {len(users)} 位使用者</p>
        <table>
            <thead>
                <tr>
                    <th>ID</th><th>姓名</th><th>郵箱</th><th>狀態</th><th>每日上限</th><th>操作</th><th>註冊日期</th>
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
        daily_limit = 0

    await db.execute(
        update(User).where(User.id == user_id).values(daily_limit=daily_limit)
    )
    await db.commit()
    logger.info(f"Admin {current_user.email} updated user {user_id} daily_limit to {daily_limit}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/toggle-admin")
async def toggle_admin(
    user_id: int = Form(...),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """切換管理員狀態"""
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    if u:
        if u.id == current_user.id:
            # 不允許取消自己的管理員
            pass
        else:
            await db.execute(
                update(User).where(User.id == user_id).values(is_admin=not u.is_admin)
            )
            await db.commit()
            logger.info(f"Admin {current_user.email} toggled admin for {u.email}: {not u.is_admin}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/toggle-active")
async def toggle_active(
    user_id: int = Form(...),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """切換帳號啟用/停用"""
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    if u:
        if u.id == current_user.id:
            pass  # 不允許停用自己的帳號
        else:
            await db.execute(
                update(User).where(User.id == user_id).values(is_active=not u.is_active)
            )
            await db.commit()
            logger.info(f"Admin {current_user.email} toggled active for {u.email}: {not u.is_active}")
    return RedirectResponse(url="/admin", status_code=303)


@router.post("/delete-user")
async def delete_user(
    user_id: int = Form(...),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除使用者"""
    result = await db.execute(select(User).where(User.id == user_id))
    u = result.scalar_one_or_none()
    if u and u.id != current_user.id:
        await db.delete(u)
        await db.commit()
        logger.info(f"Admin {current_user.email} deleted user {u.email}")
    return RedirectResponse(url="/admin", status_code=303)


@router.get("/feedback", response_class=HTMLResponse)
async def feedback_page(
    request: Request,
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """反饋評分查看頁面"""
    result = await db.execute(
        select(Feedback).order_by(Feedback.submitted_at.desc()).limit(100)
    )
    feedbacks = result.scalars().all()

    rows = ""
    total = {"insight": 0, "clarity": 0, "actionability": 0, "overall": 0, "reuse_intent": 0}
    for f in feedbacks:
        user_result = await db.execute(select(User).where(User.id == f.user_id))
        user = user_result.scalar_one_or_none()
        user_name = user.email if user else f"ID:{f.user_id}"
        rows += f"""
        <tr>
            <td style="font-size:0.8rem;white-space:nowrap">{f.submitted_at.strftime('%m/%d %H:%M') if f.submitted_at else '-'}</td>
            <td style="font-size:0.8rem">{user_name}</td>
            <td style="font-size:0.75rem;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">{f.case_id}</td>
            <td style="text-align:center">{'⭐'*f.insight}</td>
            <td style="text-align:center">{'⭐'*f.clarity}</td>
            <td style="text-align:center">{'⭐'*f.actionability}</td>
            <td style="text-align:center">{'⭐'*f.overall}</td>
            <td style="text-align:center">{'⭐'*f.reuse_intent}</td>
            <td style="text-align:center">
                <form method="POST" action="/admin/feedback/delete" style="display:inline" onsubmit="return confirm('確定刪除這筆評分？')">
                    <input type="hidden" name="feedback_id" value="{f.id}">
                    <button type="submit" style="font-size:0.7rem;padding:2px 6px;background:#ef4444;color:#fff;border:none;border-radius:3px;cursor:pointer">✕</button>
                </form>
            </td>
        </tr>"""
        total["insight"] += f.insight
        total["clarity"] += f.clarity
        total["actionability"] += f.actionability
        total["overall"] += f.overall
        total["reuse_intent"] += f.reuse_intent

    n = len(feedbacks) or 1
    avg_row = f"""
    <tr style="background:rgba(124,58,237,0.15);font-weight:bold">
        <td colspan="3" style="text-align:right">📊 平均（{len(feedbacks)} 筆）</td>
        <td style="text-align:center">{total['insight']/n:.1f}</td>
        <td style="text-align:center">{total['clarity']/n:.1f}</td>
        <td style="text-align:center">{total['actionability']/n:.1f}</td>
        <td style="text-align:center">{total['overall']/n:.1f}</td>
        <td style="text-align:center">{total['reuse_intent']/n:.1f}</td>
    </tr>"""

    html = f"""
    <!DOCTYPE html>
    <html lang="zh-TW">
    <head>
        <meta charset="UTF-8">
        <title>MHC 反饋評分</title>
        <style>
            :root {{ --bg: #0f0f1a; --card-bg: #1a1a2e; --text: #e0e0e0; --accent: #7c3aed; --border: #2a2a3e; }}
            * {{ margin:0; padding:0; box-sizing:border-box; }}
            body {{ font-family:sans-serif; background:var(--bg); color:var(--text); padding:2rem; max-width:1000px; margin:0 auto; }}
            h1 {{ color:var(--accent); margin-bottom:1rem; }}
            table {{ width:100%; border-collapse:collapse; background:var(--card-bg); border-radius:8px; overflow:hidden; }}
            th, td {{ padding:0.6rem 0.75rem; border-bottom:1px solid var(--border); font-size:0.85rem; }}
            th {{ background:var(--accent); color:white; }}
            tr:hover {{ background:rgba(124,58,237,0.1); }}
            a {{ color:var(--accent); }}
            .nav {{ margin-bottom:1rem; }}
        </style>
    </head>
    <body>
        <div class="nav"><a href="/admin">← 管理後台</a></div>
        <h1>📊 反饋評分</h1>
        <table>
            <thead><tr>
                <th>時間</th><th>使用者</th><th>案例 ID</th>
                <th>洞察力</th><th>清晰度</th><th>可行度</th><th>整體</th><th>再使用</th><th></th>
            </tr></thead>
            <tbody>{avg_row}{rows}</tbody>
        </table>
    </body>
    </html>
    """
    return HTMLResponse(html)


@router.post("/feedback/delete")
async def delete_feedback(
    feedback_id: int = Form(...),
    current_user: User = Depends(get_admin_user),
    db: AsyncSession = Depends(get_db),
):
    """刪除單筆反饋評分"""
    await db.execute(delete(Feedback).where(Feedback.id == feedback_id))
    await db.commit()
    logger.info(f"Admin {current_user.email} deleted feedback {feedback_id}")
    return RedirectResponse(url="/admin/feedback", status_code=303)

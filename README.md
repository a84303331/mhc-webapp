# MHC WebApp（mhc.summer-hsia.com）

Minerva HC Toolbox 網站 — 前端使用者介面。

## 環境變數

| 變數 | 說明 |
|------|------|
| `DATABASE_URL` | PostgreSQL（Railway 自動注入）|
| `SECRET_KEY` | JWT 簽署金鑰 |
| `MHC_API_URL` | PC 端 MHC Backend URL |
| `MHC_API_TOKEN` | PC API 驗證 token |
| `GMAIL_ADDRESS` | 寄信信箱 |
| `GOOGLE_CLIENT_ID` | Google OAuth client ID |
| `GOOGLE_CLIENT_SECRET` | Google OAuth secret |
| `GOOGLE_REFRESH_TOKEN` | Google OAuth refresh token |
| `TURNSTILE_SITE_KEY` | Cloudflare Turnstile site key |
| `TURNSTILE_SECRET_KEY` | Cloudflare Turnstile secret |
| `ADMIN_EMAIL` | 管理員信箱 |
| `ADMIN_BYPASS_KEY` | 管理員備援金鑰 |

## 本地開發

```bash
# 1. Clone + 安裝
git clone https://github.com/a84303331/mhc-webapp.git
cd mhc-webapp
uv sync

# 2. 設定 .env（從 Railway Dashboard 複製或自建）

# 3. 啟動 PostgreSQL
docker run --name mhc-pg -e POSTGRES_PASSWORD=postgres -p 5432:5432 -d postgres:16

# 4. 啟動
uv run uvicorn main:app --reload --port 8000
```

## 部署（Railway）

```bash
# 從 Railway Dashboard 建立專案 → 連接 GitHub repo → 設定環境變數
railway link
railway up
```

## 關於

MHC WebApp 是 Minerva HC Toolbox 的網頁前端，位於 mhc.summer-hsia.com。

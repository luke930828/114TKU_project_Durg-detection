from fastapi import FastAPI
from contextlib import asynccontextmanager
from fastapi.middleware.cors import CORSMiddleware
import os
import secrets
import uuid
import database
from routers import auth, scan, crawler, whitelist, ai_engine, export, users, blacklist

from password import hash_password as get_password_hash


def _initial_admin_password() -> tuple[str, bool]:
    """
    第一個管理員的密碼從環境變數來；沒設就隨機產生。

    以前這裡寫死一組固定密碼。那等於帳密就放在原始碼裡、放在公開的 repo 上，
    任何看得到程式碼的人都有管理員權限——而且系統從來不要求改密。

    回傳 (密碼, 是否為隨機產生)。隨機產生的那組只會在建立當下印出來一次，
    之後再也拿不回來，所以正式環境請自己設 ADMIN_INITIAL_PASSWORD。
    """
    pw = os.getenv("ADMIN_INITIAL_PASSWORD", "").strip()
    if pw:
        return pw, False
    return secrets.token_urlsafe(18), True


# --- 初始化腳本區塊 ---
def init_default_admin(db):
    print("🌱 進入資料庫初始化檢查...")

    admin_user = db.query(database.User).filter(database.User.account == "admin").first()

    if not admin_user:
        print("⚠️ 未偵測到管理員帳號，正在自動建立預設管理員...")
        new_user_id = "U" + str(uuid.uuid4().hex)[:8].upper()
        password, generated = _initial_admin_password()

        new_admin = database.User(
            user_id=new_user_id,
            account="admin",
            password_hash=get_password_hash(password),
            # dependencies.py 的 verify_admin 檢查的是這個中文字串，"admin" 會直接被 403 擋掉
            role="系統管理員",
            department="系統管理部",
            is_active=True
        )
        db.add(new_admin)
        db.commit()

        print("✅ 預設管理員建立完成！帳號：admin")
        if generated:
            print("=" * 62)
            print("  這是隨機產生的初始密碼，只會出現這一次，請立刻登入並修改：")
            print(f"    {password}")
            print("  下次要指定密碼的話，啟動前設好 ADMIN_INITIAL_PASSWORD。")
            print("=" * 62)
        else:
            print("   密碼取自 ADMIN_INITIAL_PASSWORD，請登入後盡快修改。")
    else:
        print("✅ 預設管理員帳號已存在，跳過初始化。")

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("🚀 伺服器啟動中，連線至資料庫...")
    # Docker 會在 uvicorn 前執行一次；保留這次呼叫讓直接以 uvicorn 啟動的
    # 開發環境也能補齊既有資料表的 OCR 欄位。此程序可安全重複執行。
    database.initialize_database()
    db = database.SessionLocal()
    try:
        init_default_admin(db)
    except Exception as e:
        print(f"❌ 初始化管理員失敗: {e}")
    finally:
        db.close()
    
    yield
    print("🛑 伺服器正在關閉...")

# --- 應用程式實例 ---
app = FastAPI(
    title="多模態毒品防制系統 API", 
    description="符合原始表與 AI 展示表分離架構",
    lifespan=lifespan  
)

# --- 中介軟體 (CORS) ---
# CORS_ORIGINS 早就在 .env.local 與 compose 裡設好了（http://localhost:8080），
# 但這裡以前寫死 allow_origins=["*"]，那個設定完全沒有作用。
#
# 而且 "*" 配 allow_credentials=True 是規格上無效的組合。更重要的是
# 這個系統的 token 是手動放在 X-Token header，不是 cookie——
# 瀏覽器的 credential 規則保護不到它，任何網站都能讀到 API 回應。
_origins = [o.strip() for o in os.getenv("CORS_ORIGINS", "").split(",") if o.strip()]
if not _origins:
    # 沒設就只開本機，不要退回全開。寧可前端連不上讓人發現，
    # 也不要靜靜地對全世界開放。
    _origins = ["http://localhost:8080", "http://127.0.0.1:8080"]
    print(f"⚠️ 沒有設定 CORS_ORIGINS，預設只允許 {_origins}")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "X-Token"],
)


@app.middleware("http")
async def security_headers(request, call_next):
    """
    API 回應也要帶安全標頭。前端那邊由 nginx 負責，但後端可能被直接存取
    （SEC-22：nginx 的 /api/ 未過濾轉發），所以兩邊都要有。
    """
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    # API 只回 JSON，不需要載入任何資源
    response.headers["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
    return response

# --- 路由註冊 ---
app.include_router(auth.router)
app.include_router(scan.router)
app.include_router(crawler.router)
app.include_router(whitelist.router)
app.include_router(blacklist.router)
app.include_router(ai_engine.router)
app.include_router(export.router)
app.include_router(users.router)

# --- 根目錄與健康檢查 ---
@app.get("/")
def read_root():
    return {"message": "防制系統 API 正常運行中"}

@app.get("/health")
def health():
    return {"status": "ok"}

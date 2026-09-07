from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from datetime import datetime, timedelta, timezone
import database
from schemas import UserLogin
import jwt
import os
from dependencies import get_db, get_current_user, log_audit_action, SECRET_KEY, ALGORITHM
from password import hash_password, verify_password

# token 有效時數。以前沒有 exp，簽出去的 token 永久有效：
# 改密碼、停權、刪帳號通通讓它失效不了，外洩一次就是永久通行證。
TOKEN_TTL_HOURS = int(os.getenv("JWT_TTL_HOURS", "8"))

# 連續失敗幾次就暫時鎖住，以及鎖多久。
MAX_FAILED_ATTEMPTS = int(os.getenv("LOGIN_MAX_ATTEMPTS", "5"))
LOCKOUT_MINUTES = int(os.getenv("LOGIN_LOCKOUT_MINUTES", "15"))

# 所有登入失敗都回這一句。以前帳號不存在、被註銷、被凍結、密碼錯誤
# 各回不同訊息，攻擊者送一個帳號進去就知道它存不存在、狀態是什麼。
# 真正的原因寫進稽核紀錄，管理員查得到，使用者問起來也答得出來。
GENERIC_LOGIN_ERROR = "帳號或密碼錯誤"

router = APIRouter(tags=["系統登入"])


def _recent_failures(db: Session, user_id: str) -> int:
    """
    算「最近的連續失敗次數」。

    起算點取兩者較晚的：LOCKOUT_MINUTES 分鐘前，或上一次成功登入。
    所以登入成功會自動把計數歸零，不需要另外清。

    直接查 audit_logs 而不是另開一張表——失敗紀錄本來就要寫進去
    （SEC-17 補的），這裡順便拿來用，少一張表要維護。
    """
    # 舊寫法（勿用）：datetime.utcnow() - timedelta(...)
    # 這裡是拿 Python 的時間去比對資料庫裡的 action_timestamp。
    # action_timestamp 現在由 MySQL NOW() 寫入（本機時區），這邊也必須用
    # 本機時間，否則兩者差 8 小時，鎖定機制會整個失效。
    since = datetime.now() - timedelta(minutes=LOCKOUT_MINUTES)
    last_ok = (
        db.query(database.AuditLog.action_timestamp)
        .filter(database.AuditLog.user_id == user_id,
                database.AuditLog.action_type == "登入")
        .order_by(database.AuditLog.action_timestamp.desc())
        .first()
    )
    if last_ok and last_ok[0] and last_ok[0] > since:
        since = last_ok[0]

    return (
        db.query(database.AuditLog)
        .filter(database.AuditLog.user_id == user_id,
                database.AuditLog.action_type == "登入失敗",
                database.AuditLog.action_timestamp > since)
        .count()
    )

@router.post("/api/login/", summary="系統登入")
def login_for_access_token(login_data: UserLogin, db: Session = Depends(get_db)):
    
    user = db.query(database.User).filter(database.User.account == login_data.account).first()

    # 帳號不存在。這種失敗記不進 audit_logs——user_id 是 nullable=False 的
    # 外鍵，沒有對應使用者就寫不進去，所以也無法對它計數鎖定。
    # 要補得改 schema 讓 user_id 可為 null，那是另一次改動。
    if not user:
        raise HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

    # 先看有沒有被鎖。放在密碼驗證之前，否則暴力破解仍然可以一直試。
    if _recent_failures(db, user.user_id) >= MAX_FAILED_ATTEMPTS:
        log_audit_action(db, user.user_id, "登入遭鎖定",
                         f"帳號 {user.account} 連續失敗達 {MAX_FAILED_ATTEMPTS} 次，"
                         f"暫時鎖定 {LOCKOUT_MINUTES} 分鐘")
        raise HTTPException(
            status_code=429,
            detail=f"嘗試次數過多，請於 {LOCKOUT_MINUTES} 分鐘後再試。",
        )

    def _fail(reason: str):
        """失敗一律回同一句話，真正的原因只寫進稽核紀錄。"""
        log_audit_action(db, user.user_id, "登入失敗", f"帳號 {user.account}：{reason}")
        return HTTPException(status_code=401, detail=GENERIC_LOGIN_ERROR)

    if user.is_deleted:
        raise _fail("帳號已註銷")

    if not user.is_active:
        raise _fail("帳號已凍結")

    ok, needs_rehash = verify_password(login_data.password, user.password_hash)
    if not ok:
        raise _fail("密碼錯誤")
        
    # 通過所有考驗，發放通行證 (Token)
    # PyJWT 解碼時只要 payload 裡有 exp 就會自動驗，不用另外寫檢查。
    now = datetime.now(timezone.utc)
    token_payload = {
        "sub": user.account,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(hours=TOKEN_TTL_HOURS)).timestamp()),
    }
    encrypted_token = jwt.encode(token_payload, SECRET_KEY, algorithm=ALGORITHM)

    # 舊帳號還是無鹽 SHA-256。趁這次登入（手上正好有明文）換成 bcrypt，
    # 使用者無感，不需要請所有人重設密碼。
    if needs_rehash:
        user.password_hash = hash_password(login_data.password)
        db.commit()
        log_audit_action(db, user.user_id, "密碼雜湊升級",
                         f"帳號 {user.account} 的密碼已從 SHA-256 轉為 bcrypt")

    log_audit_action(db, user.user_id, "登入", f"帳號 {user.account} 登入系統")

    return {
        "status": "success",
        "message": f"登入成功！{user.account}",
        "access_token": encrypted_token,
        # 把自己的帳號與角色一起回去，前端才知道要不要顯示管理員專屬的功能。
        # 這不算洩漏——使用者本來就知道自己是誰、有什麼權限。
        #
        # 沒有這個之前，前端無從判斷，只好把「人員與權限管理」顯示給所有人，
        # 一般人員點下去才撞 403。看得到卻永遠進不去，比一開始就不顯示更糟。
        #
        # ⚠️ 這只用來決定「畫面上要不要出現」。真正的權限仍然由後端的
        #    verify_admin 把關——前端的判斷是使用者體驗，不是安全機制。
        "account": user.account,
        "role": user.role,
    }

@router.post("/api/logout/", summary="系統登出")
def logout(current_user = Depends(get_current_user), db: Session = Depends(get_db)):
    """
    token 是無狀態的，這個端點不會讓它失效——單純是為了在稽核軌跡上
    留下「這個人什麼時候結束操作」。前端呼叫完再自己清掉 token。
    """
    log_audit_action(db, current_user.user_id, "登出", f"帳號 {current_user.account} 登出系統")
    return {"status": "success", "message": "已登出"}

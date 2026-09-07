from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi import status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session
import database
import hmac
import jwt
import os

# 密碼相關一律用 password.py，不要再在各檔案自己寫一份 sha256
from password import hash_password as get_password_hash          # noqa: F401
from password import validate_strength, verify_password          # noqa: F401


oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/login/")

# 必填，沒設就直接爆掉：這是 JWT 簽章密鑰，寫死在程式碼裡（或給預設值）等於任何看得到
# 原始碼的人都能自己簽發 super_admin 的 token，比洩漏資料庫密碼更嚴重。
# 這行已經被改回有預設值兩次了——本機測試請在 .env.local 設好 JWT_SECRET_KEY，
# 不要在這裡加預設值繞過去。
SECRET_KEY = os.environ["JWT_SECRET_KEY"]
ALGORITHM = "HS256"

# 服務間驗證用的共用密鑰。三個 report 端點（crawler / nlp / ai_result）只給機器打，
# 人不會經過它們，所以不走 JWT，改用一組固定的 token。
#
# 一樣沒設就爆掉，而且空字串也不行——hmac.compare_digest("", "") 會回 True，
# 等於 .env 漏了一行就靜靜地退回「完全無驗證」，比一開始就沒做還危險。
INTERNAL_API_TOKEN = os.environ["INTERNAL_API_TOKEN"]
if len(INTERNAL_API_TOKEN) < 16:
    raise RuntimeError(
        "INTERNAL_API_TOKEN 沒設或太短（至少 16 字元）。"
        "請在 .env 產一組：openssl rand -hex 24"
    )

def get_db():
    session = Session(bind=database.engine)
    try:
        yield session
    finally:
        session.close()

def log_audit_action(db: Session, user_id: str, action_type: str, details: str = ""):
    new_log = database.AuditLog(user_id=user_id, action_type=action_type, details=details)
    db.add(new_log)
    db.commit()

# JWT 解碼版
def get_current_user(x_token: str = Header(...), db: Session = Depends(get_db)):
    try:
        payload = jwt.decode(x_token, SECRET_KEY, algorithms=[ALGORITHM])
        account: str = payload.get("sub")
        
        if account is None:
            raise HTTPException(status_code=401, detail="憑證內容無效！")
            
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="身分驗證失敗：無效或偽造的憑證！")

    # is_deleted 一定要一起查。delete_user 只設 is_deleted=True、沒動 is_active，
    # 所以只看 is_active 的話，被刪掉的人手上那張 token 還是暢行無阻——
    # 配合以前 token 不會過期，等於帳號刪不掉。
    user = db.query(database.User).filter(
        database.User.account == account,
        database.User.is_deleted == False,      # noqa: E712
    ).first()

    if not user:
        raise HTTPException(status_code=401, detail="身分驗證失敗：找不到該使用者！")
    
    if hasattr(user, 'is_active') and not user.is_active:
        raise HTTPException(status_code=403, detail="權限被拒絕：您的帳號已被停止使用！")
        
    return user

def verify_admin(current_user: database.User = Depends(get_current_user)):
    if current_user.role != "系統管理員":
        raise HTTPException(status_code=403, detail="權限不足：只有系統管理員可以執行此動作！")
    return current_user

def verify_super_admin(current_user: database.User = Depends(verify_admin)):
    if current_user.account != "super_admin":
        raise HTTPException(status_code=403, detail="權限不足：此操作僅限「總管理員」執行！")
    return current_user

def verify_internal_token(
    x_internal_token: str = Header(None),
    authorization: str = Header(None),
):
    """
    機器對機器端點的驗證。兩種帶法都收：

        X-Internal-Token: <token>
        Authorization: Bearer <token>

    收兩種是因為爬蟲模組本來就是用 Bearer 送的（webhook_helper.py 的重試與
    死信邏輯都繞著那個 header 寫），為了統一名稱去改它不划算。

    比對一定要用 hmac.compare_digest：`==` 會在第一個不同的位元組就回傳，
    可以從回應時間一個字元一個字元把 token 猜出來。
    """
    supplied = x_internal_token
    if not supplied and authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            supplied = value.strip()

    # 「沒帶」跟「帶錯」回一樣的訊息。分開講等於告訴對方 header 名稱猜對了。
    if not supplied or not hmac.compare_digest(supplied, INTERNAL_API_TOKEN):
        raise HTTPException(status_code=401, detail="內部服務驗證失敗")

    return True

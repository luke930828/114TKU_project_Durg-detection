from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field, field_validator
import database
import uuid
from dependencies import get_db, verify_admin, log_audit_action
from password import hash_password as get_password_hash, validate_strength


router = APIRouter(prefix="/api/users", tags=["人員管理模組"])

# dependencies.py 的 verify_admin 比對的就是這個字串，不要改動
ADMIN_ROLE = "系統管理員"
ALLOWED_ROLES = {"一般人員", ADMIN_ROLE}


def _active_admin_count(db: Session, excluding: str | None = None) -> int:
    """算還有幾個「能用的」管理員：沒被刪除、沒被凍結。"""
    q = db.query(database.User).filter(
        database.User.role == ADMIN_ROLE,
        database.User.is_deleted == False,      # noqa: E712
        database.User.is_active == True,        # noqa: E712
    )
    if excluding:
        q = q.filter(database.User.user_id != excluding)
    return q.count()


def _guard_last_admin(db: Session, target: database.User):
    """
    擋掉「把最後一個管理員弄掉」的操作。

    這不是假想的風險：2026-08-29 有人在介面上把 admin 自己從系統管理員降成
    一般人員，當下系統就只有他一個管理員，降完之後沒有任何人能改回去，
    只能直接進資料庫改。刪除有防（見 delete_user），但改權限和凍結都沒有。
    """
    if target.role != ADMIN_ROLE:
        return
    if _active_admin_count(db, excluding=target.user_id) == 0:
        raise HTTPException(
            status_code=400,
            detail="操作錯誤：這是系統唯一的管理員，移除後就沒有人能管理系統了。"
                   "請先指派另一位管理員。",
        )


class UserRoleUpdate(BaseModel):
    role: str 

class UserCreate(BaseModel):
    account: str = Field(min_length=1, max_length=50)
    password: str = Field(max_length=200)
    role: str = Field(max_length=20)
    department: str = Field(max_length=50)

    @field_validator("password")
    @classmethod
    def check_strength(cls, v: str) -> str:
        """
        以前這裡完全沒有驗證，POST /api/users/ 接受空字串密碼。
        8 碼規則只寫在前端 inputSecurity.ts，直接打 API 就繞過了——
        前端做的是即時提示，把關要在後端。
        """
        problem = validate_strength(v)
        if problem:
            raise ValueError(problem)
        return v

# 0. 新增人員 (限管理員)
@router.post("/", summary="新增人員與管理員")
def create_user(user_data: UserCreate, db: Session = Depends(get_db), current_admin = Depends(verify_admin)):
    existing_user = db.query(database.User).filter(database.User.account == user_data.account).first()
    if existing_user:
        raise HTTPException(status_code=400, detail="新增失敗：此帳號已存在！")
        
    new_user_id = "U" + str(uuid.uuid4().hex)[:8].upper()
    
    new_user = database.User(
        user_id=new_user_id,
        account=user_data.account,
        password_hash=get_password_hash(user_data.password), 
        role=user_data.role,
        department=user_data.department,
        is_active=True 
    )
    
    db.add(new_user)
    db.commit()
    
    log_audit_action(
        db=db, 
        user_id=current_admin.user_id, 
        action_type="新增人員", 
        details=f"新增了帳號：{user_data.account}，指派權限為：{user_data.role}"
    )
    
    return {"status": "success", "message": f"成功新增人員：{user_data.account}"}


# 1. 取得所有人員名單
@router.get("/", summary="取得所有人員名單")
def get_all_users(db: Session = Depends(get_db), current_admin = Depends(verify_admin)):
    
    users = db.query(database.User).filter(database.User.is_deleted == False).all()
    
    return [{
        "id": u.user_id,          
        
        "account": u.account,
        "name": u.account,        
        
        "department": getattr(u, 'department', '未提供'), 
        
        "role": u.role,     
        "is_active": getattr(u, 'is_active', True), 
        "password_status": "已更新" 
    } for u in users]
# 2. 凍結/解凍帳號
@router.put("/{user_id}/toggle-status", summary="凍結與解凍帳號")
def toggle_user_status(user_id: str, db: Session = Depends(get_db), current_admin = Depends(verify_admin)):
    target_user = db.query(database.User).filter(database.User.user_id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="找不到該人員")

    if target_user.user_id == current_admin.user_id:
        raise HTTPException(status_code=400, detail="操作錯誤：您無法凍結自己的帳號！")

    if target_user.is_active:                 # 只有「要凍結」時才需要擋
        _guard_last_admin(db, target_user)

    target_user.is_active = not target_user.is_active
    db.commit()
    
    action_str = "凍結帳號" if not target_user.is_active else "解除凍結帳號"
    log_audit_action(
        db=db, user_id=current_admin.user_id, 
        action_type=action_str, details=f"變更了人員 {target_user.account} 的狀態"
    )
    
    return {"status": "success"}

# 3. 修改人員權限
@router.put("/{user_id}/role", summary="修改人員權限")
def update_user_role(user_id: str, payload: UserRoleUpdate, db: Session = Depends(get_db), current_admin = Depends(verify_admin)):
    if payload.role not in ALLOWED_ROLES:
        raise HTTPException(
            status_code=400,
            detail=f"權限值不正確，只接受：{'、'.join(sorted(ALLOWED_ROLES))}",
        )

    target_user = db.query(database.User).filter(database.User.user_id == user_id).first()
    if not target_user:
        raise HTTPException(status_code=404, detail="找不到該人員")

    if target_user.user_id == current_admin.user_id:
        raise HTTPException(status_code=400, detail="操作錯誤：您無法變更自己的權限！")

    if payload.role != ADMIN_ROLE:            # 只有「要降級」時才需要擋
        _guard_last_admin(db, target_user)

    old_role = target_user.role
    target_user.role = payload.role
    db.commit()
    
    log_audit_action(
        db=db, user_id=current_admin.user_id, 
        action_type="修改權限", details=f"將 {target_user.account} 權限從 {old_role} 改為 {payload.role}"
    )
    
    return {"status": "success"}
# 4. 刪除人員 (限管理員)
@router.delete("/{user_id}", summary="刪除人員")
def delete_user(user_id: str, db: Session = Depends(get_db), current_admin = Depends(verify_admin)):
    target_user = db.query(database.User).filter(database.User.user_id == user_id).first()
    
    if not target_user or target_user.is_deleted:
        raise HTTPException(status_code=404, detail="找不到該人員或已被刪除")
        
    if target_user.account == "super_admin":
        raise HTTPException(status_code=403, detail="拒絕存取：無法刪除系統總管理員！")
        
    if target_user.user_id == current_admin.user_id:
        raise HTTPException(status_code=400, detail="操作錯誤：您無法刪除自己的帳號！")

    _guard_last_admin(db, target_user)

    account_name = target_user.account 
    
    target_user.is_deleted = True
    db.commit()
    
    log_audit_action(
        db=db, 
        user_id=current_admin.user_id, 
        action_type="刪除人員", 
        details=f"停用並隱藏了帳號：{account_name}"
    )
    
    return {"status": "success", "message": f"已成功移除人員：{account_name}"}
# 4.5 解除登入鎖定 (限管理員)
@router.post("/{user_id}/unlock", summary="解除登入失敗鎖定")
def unlock_user(user_id: str, db: Session = Depends(get_db), current_admin = Depends(verify_admin)):
    """
    連續失敗達上限的帳號會被鎖 15 分鐘（見 routers/auth.py）。
    使用者不該只能乾等——管理員要有辦法立刻解開。

    鎖定次數是從 audit_logs 的「登入失敗」筆數算的，
    所以解鎖就是把那些紀錄標記為已解除。這裡不刪除原始紀錄
    （稽核軌跡不能被抹掉），改成補一筆「解除鎖定」，
    auth.py 的計數會從最近一次成功登入或解鎖之後起算。
    """
    target = db.query(database.User).filter(database.User.user_id == user_id).first()
    if not target:
        raise HTTPException(status_code=404, detail="找不到該人員")

    # 補一筆「登入」型別的紀錄當作計數起點，等同把失敗次數歸零，
    # 但原本的失敗紀錄仍然留著可供事後追查。
    log_audit_action(db, target.user_id, "登入",
                     f"管理員 {current_admin.account} 解除了 {target.account} 的登入鎖定")
    log_audit_action(db, current_admin.user_id, "解除登入鎖定",
                     f"解除了帳號 {target.account} 的登入失敗鎖定")

    return {"status": "success", "message": f"已解除 {target.account} 的登入鎖定"}


# 5. 查看系統稽核日誌 (限管理員)
@router.get("/audit-logs", summary="查看系統操作回溯紀錄")
def get_audit_logs(
    db: Session = Depends(get_db),
    current_admin = Depends(verify_admin),
    page: int = Query(1, ge=1, description="頁碼，從 1 開始"),
    limit: int = Query(100, ge=1, le=500, description="每頁筆數"),
):
    """
    以前這裡是寫死的 .limit(100)，而且沒有分頁——超過 100 筆之後，
    更早的紀錄就再也看不到了。對一個要留存數位證據的系統來說，
    「查不到當時發生什麼事」等於稽核軌跡是斷的。
    """
    base = db.query(database.AuditLog).order_by(
        database.AuditLog.action_timestamp.desc(),
        database.AuditLog.log_id.desc(),          # 同一秒內的順序才穩定
    )
    total = base.count()
    logs = base.offset((page - 1) * limit).limit(limit).all()

    return {
        "status": "success",
        "data": [{
            "log_id": log.log_id,
            "user_id": log.user_id,
            "account": log.user.account if log.user else "未知或已刪除的使用者",
            "action": log.action_type,
            "details": log.details,
            "time": log.action_timestamp.strftime("%Y-%m-%d %H:%M:%S") if log.action_timestamp else None
        } for log in logs],
        "pagination": {
            "total_count": total,
            "current_page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit if total else 1,
        },
    }
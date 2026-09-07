"""
人工黑名單。跟白名單對稱。

為什麼需要這個
──────────────
在這之前系統的「黑名單」完全是從 ai_analysis_results.risk_level == 極高風險
推導出來的，沒有任何地方可以人工把一個網址標成毒品網站。承辦人員手上有情資
（別的單位通報、線報、已起訴的案子）卻沒地方放，前端的「新增黑名單」按鈕
按下去只改前端記憶體，重新整理就沒了。

比對用網域而不是完整網址，理由跟白名單一樣：一個站有幾十萬個頁面，
一頁一頁加是不可能的。
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session
from typing import Optional

import database
from schemas import BlacklistCreate
from dependencies import get_db, get_current_user, verify_admin, log_audit_action
from utils import like_pattern, registrable_domain

router = APIRouter(tags=["黑名單維護"])


# 權限設計：新增開放給一般人員，刪除保留給管理員
# ────────────────────────────────────────────
# 名單維護是承辦人員的日常工作——看到誤判要能立刻排除、拿到情資要能立刻標記。
# 每次都要找管理員的話，實務上的結果是「大家乾脆不維護」。
#
# 但刪除留給管理員，因為那是破壞性的方向：
#   刪白名單 → 一個已經人工確認過的正常網站，重新被當成可疑目標
#   刪黑名單 → 一個已經確認的毒品網站，被取消標記
# 新增最壞的情況是多一筆錯的資料，刪除最壞的情況是失去既有的判斷。
#
# 兩種操作都會寫進 audit_logs，追得到是誰做的。


@router.get("/api/blacklist/", summary="查看人工黑名單")
def list_blacklist(
    db: Session = Depends(get_db),
    current_user: database.User = Depends(get_current_user),
    q: Optional[str] = Query(None, max_length=200,
                             description="關鍵字搜尋：網址、標題或原因"),
):
    query = db.query(database.BlacklistWebsite)
    if q and q.strip():
        like = like_pattern(q)
        query = query.filter(
            database.BlacklistWebsite.url.like(like, escape="\\")
            | database.BlacklistWebsite.title.like(like, escape="\\")
            | database.BlacklistWebsite.reason.like(like, escape="\\")
        )
    return query.order_by(database.BlacklistWebsite.created_at.desc()).all()


@router.post("/api/blacklist/", summary="新增黑名單（一般人員可用）")
def add_blacklist(
    data: BlacklistCreate,
    admin: database.User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    domain = registrable_domain(data.url)
    if not domain:
        raise HTTPException(status_code=400, detail="網址格式無效，解析不出網域。")

    # 同網域只留一筆——比對是用網域做的，重複加沒有意義只會讓清單變雜。
    for row in db.query(database.BlacklistWebsite).all():
        if registrable_domain(row.url) == domain:
            raise HTTPException(
                status_code=400,
                detail=f"該網域已在黑名單中（{row.url}）。",
            )

    # 同一個網域不該同時在黑白名單。白名單會讓爬蟲直接放行，
    # 兩邊都有的話行為取決於程式碼順序，那是最難查的一種 bug。
    for row in db.query(database.WhitelistWebsite).all():
        if registrable_domain(row.url) == domain:
            raise HTTPException(
                status_code=400,
                detail=f"該網域目前在白名單中（{row.url}），請先移除白名單。",
            )

    new_black = database.BlacklistWebsite(
        url=data.url, title=data.title, reason=data.reason, added_by=admin.account
    )
    db.add(new_black)
    db.commit()

    log_audit_action(
        db=db,
        user_id=admin.user_id,
        action_type="新增黑名單",
        details=f"將網址 {data.url}（網域 {domain}）加入黑名單。原因：{data.reason}"[:500],
    )
    return {"status": "success", "message": f"成功由管理員 {admin.account} 新增黑名單。"}


@router.delete("/api/blacklist/{entry_id}", summary="管理員：移除黑名單")
def delete_blacklist(
    entry_id: int,
    admin: database.User = Depends(verify_admin),
    db: Session = Depends(get_db),
):
    row = db.query(database.BlacklistWebsite).filter(
        database.BlacklistWebsite.id == entry_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="找不到該筆黑名單。")

    url = row.url
    db.delete(row)
    db.commit()

    log_audit_action(
        db=db,
        user_id=admin.user_id,
        action_type="移除黑名單",
        details=f"將網址 {url} 移出黑名單"[:500],
    )
    return {"status": "success", "message": "已移除黑名單。"}

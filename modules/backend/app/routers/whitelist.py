from fastapi import APIRouter, Depends, HTTPException, Query
from typing import Optional
from sqlalchemy.orm import Session
import database
from schemas import WhitelistCreate
from dependencies import get_db, get_current_user, verify_admin, verify_super_admin, log_audit_action
from utils import like_pattern, registrable_domain, purge_analysis_for_domain, is_whitelisted

router = APIRouter(tags=["白名單維護"])

# 模組六：白名單維護管理

# 權限設計：新增開放給一般人員，刪除保留給管理員。
# 名單維護是承辦人員的日常，每次都要找管理員的話大家就乾脆不維護了；
# 但刪除是把「已經確認過的判斷」取消掉，風險跟新增不對等。
# 兩種操作都會寫進 audit_logs。


@router.get("/api/whitelist/", summary="查看白名單清單")
def list_whitelist(
    db: Session = Depends(get_db),
    current_user: database.User = Depends(get_current_user),
    q: Optional[str] = Query(None, max_length=200,
                             description="關鍵字搜尋：網址、標題或原因"),
):
    query = db.query(database.WhitelistWebsite)
    if q and q.strip():
        like = like_pattern(q)
        query = query.filter(
            database.WhitelistWebsite.url.like(like, escape="\\")
            | database.WhitelistWebsite.title.like(like, escape="\\")
            | database.WhitelistWebsite.reason.like(like, escape="\\")
        )
    return query.order_by(database.WhitelistWebsite.created_at.desc()).all()

# 這裡把 verify_super_admin 換成了 verify_admin
@router.post("/api/whitelist/", summary="新增白名單（一般人員可用）")
def add_whitelist(data: WhitelistCreate, admin: database.User = Depends(get_current_user), db: Session = Depends(get_db)):
    # 重複檢查要用「網域」，不能用「完全相同的網址」：白名單本來就是用網域比對的
    # （is_whitelisted），比完整網址的話，同一個網域換個路徑就能重複加進來，
    # 清單會越來越髒，而且刪掉其中一筆還會被另一筆擋著。
    domain = registrable_domain(data.url)
    if not domain:
        raise HTTPException(status_code=400, detail="網址格式無效，解析不出網域。")

    already = is_whitelisted(db, data.url)
    if already:
        # 不報錯。使用者的意圖是「讓這個站不要再出現」，而它已經在白名單了——
        # 該做的是把殘留的分析結果清掉，那才是他真正想要的結果。
        # 早期版本加白名單不會清分析結果，所以線上有不少這種殘留
        # （wooessential.com 已在白名單，卻還有 24 筆掛在待確認）。
        removed = purge_analysis_for_domain(db, domain)
        db.commit()
        if removed:
            log_audit_action(
                db=db, user_id=admin.user_id, action_type="清除白名單網域的殘留分析",
                details=f"網域 {domain} 已在白名單（{already.url}），"
                        f"清除殘留的 {removed} 筆分析結果"[:500])
        # 這條路徑直接 return、不建立新紀錄，所以訊息要講清楚「你填的沒有被採用」，
        # 否則使用者會在清單上看到別人寫的原因，以為系統存錯了。
        when = already.created_at.strftime("%Y-%m-%d") if already.created_at else "先前"
        note = ""
        if (data.title and data.title != already.title) or \
           (data.reason and data.reason != already.reason):
            note = ("　你這次填的名稱與原因沒有覆蓋原紀錄——"
                    "白名單一個網域只留一筆，要修改內容請先移除舊的那筆再重加。")
        return {
            "status": "success",
            "message": f"網域 {domain} 已經在白名單中。"
                       f"原本那筆是「{already.title or '未命名'}」，"
                       f"原因「{already.reason or '未填'}」，"
                       f"由 {already.added_by or '不明'} 於 {when} 加入。"
                       + (f"　已清除殘留的 {removed} 筆待處理分析結果。" if removed
                          else "　沒有殘留的分析結果需要清除。")
                       + note,
            "removed": removed,
            "already": True,
            "existing": {
                "id": already.id, "url": already.url,
                "title": already.title, "reason": already.reason,
                "source": already.source, "added_by": already.added_by,
            },
        }
        
    new_white = database.WhitelistWebsite(
        url=data.url, title=data.title, reason=data.reason,
        added_by=admin.account, source=data.source or "一般新增")
    db.add(new_white)

    # 白名單是用網域比對的，加進來就代表「這個站是正常的」。所以要把該網域
    # 既有的分析結果一起清掉，否則它們會一直留在待確認清單裡——那些分數是
    # 「這個站可疑」算出來的，而人已經判定它不可疑。
    removed = purge_analysis_for_domain(db, domain)

    db.commit()
    
    log_audit_action(
        db=db,
        user_id=admin.user_id,          
        action_type="新增白名單",
        details=f"將網址 {data.url} 加入白名單"
    )
    
    return {"status": "success", "message": f"成功由管理員 {admin.account} 新增白名單。"}

# 這裡的刪除也一併把 verify_super_admin 換成了 verify_admin
@router.delete("/api/whitelist/{id}", summary="管理員：刪除白名單")
def delete_whitelist(id: int, admin: database.User = Depends(verify_admin), db: Session = Depends(get_db)):
    target = db.query(database.WhitelistWebsite).filter(database.WhitelistWebsite.id == id).first()
    if not target:
        raise HTTPException(status_code=404, detail="找不到該白名單項目。")
        
    url_to_log = target.url 
    db.delete(target)
    db.commit()
    
    log_audit_action(
        db=db,
        user_id=admin.user_id,
        action_type="刪除白名單",
        details=f"移除了網址 {url_to_log} 的白名單資格"
    )
    
    return {"status": "success", "message": "已成功移除白名單項目。"}
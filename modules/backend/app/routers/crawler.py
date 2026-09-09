from dependencies import get_db, get_current_user
from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Query
from sqlalchemy import case, func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from typing import Optional
import json
from datetime import datetime
import database
import image_store
from schemas import WebsiteReport, ConfirmBatch
from dependencies import get_db, verify_admin, verify_internal_token, log_audit_action
from utils import (calculate_multimodal_risk_100_scale, dispatch_to_ai_engines,
                   domain_sql_expr, is_blacklisted, is_whitelisted,
                   like_pattern, registrable_domain,
                   purge_analysis_for_domain)
import traceback

router = APIRouter(tags=["自動爬蟲管理"])

#  模組三：查詢已識別網站
@router.get("/api/crawler/report/", summary="獲取前端專用 AI 分析黑名單報表")
def get_frontend_report(
    current_user: database.User = Depends(verify_admin),
    db: Session = Depends(get_db),
    page: int = Query(1, ge=1, description="頁碼，從 1 開始"),
    limit: int = Query(50, ge=1, le=200, description="每頁筆數，上限 200"),
):
    """
    SEC-16：以前這裡是 .all()，沒有分頁也沒有排除 base64。

    實測回應 413 MB、耗時 13 秒——5226 筆全撈出來，每筆還夾帶最大 600 KB 的
    representative_image_base64。一個請求就能讓後端吃掉幾百 MB 記憶體，
    帶管理員 token 的人連按幾次就能把服務打掛。

    代表圖不放進列表（跟 automated_24h_list 一致），
    要圖請打 /api/crawler/result/{id}/image/。
    """
    base = db.query(database.AIAnalysisResult).order_by(
        database.AIAnalysisResult.created_at.desc(),
        database.AIAnalysisResult.id.desc(),      # 同一秒內順序才穩定
    )
    total = base.count()
    rows = base.offset((page - 1) * limit).limit(limit).all()

    data = [{
        "id": r.id,
        "url": r.url,
        "yolo_details": r.yolo_details,
        "yolo_score": r.yolo_score,
        "nlp_details": r.nlp_details,
        "nlp_score": r.nlp_score,
        "risk_score": r.risk_score,
        "risk_level": r.risk_level,
        "class_metadata": r.class_metadata,
        "task_source": r.task_source,
        "created_at": r.created_at,
        "has_representative_image": bool(r.representative_image_path or r.representative_image_base64),
    } for r in rows]

    return {
        "status": "success",
        "message": "成功抓取最新 AI 多模態識別資料庫",
        "total_count": total,
        "pagination": {
            "total_count": total,
            "current_page": page,
            "limit": limit,
            "total_pages": (total + limit - 1) // limit if limit > 0 else 0,
        },
        "data": data,
    }

# 模組四：爬蟲專用通道 
def _upsert_suspect(db: Session, url: str, **fields):
    """寫入或更新 suspect_websites 的一筆蒐證資料。

    為什麼不能直接 db.add
    ────────────────────
    suspect_websites.url 上有 UNIQUE index，而 24 小時自動爬蟲本來就會
    一再遇到同一個網站。原本這裡是無條件 add，第二次回報就撞 1062
    Duplicate entry，被外層的 except 接住 → 回 500、整筆回報丟掉。

    症狀很難查：爬蟲那邊只看到一次 500，看不出是「這個網址已經有了」，
    而畫面上顯示的還是好幾天前那份舊快照，看起來像爬蟲沒在跑。

    重複回報時用新的快照覆蓋舊的——最新的那份才是有意義的證據。
    created_at 刻意不動，那是「第一次發現的時間」。
    """
    row = db.query(database.SuspectWebsite).filter(
        database.SuspectWebsite.url == url).first()
    if row:
        for key, value in fields.items():
            setattr(row, key, value)
        return row
    row = database.SuspectWebsite(url=url, reported_by="爬蟲端自動上傳", **fields)
    db.add(row)
    return row


@router.post("/api/crawler/report/", summary="爬蟲端專用：將原始結果寫入 suspect_websites 表")
def receive_crawler_raw_data(
    report: WebsiteReport,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _internal: bool = Depends(verify_internal_token),
):
    try:
        print(f"收到爬蟲通報網址：{report.url}")
        # 比網域不是比完整網址——白名單加了 momoshop.com.tw 就該擋掉它底下
        # 所有頁面，不然幾十萬個商品頁要一頁一頁加。
        white = is_whitelisted(db, report.url)

        if white:
            print(f"[白名單放行] 網址 {report.url} 位於白名單中 ({white.title})，跳過後續所有動作！")
            return {
                "status": "skipped", 
                "message": f"攔截成功：網址 {report.url} 位於白名單中 ({white.title})，已自動放行。"
            }
        # 人工黑名單：已經有情資確認是毒品網站，不必再花 NLP + YOLO 去判一次。
        # 直接歸檔為極高風險。順帶省下運算與 base64 的儲存空間。
        black = is_blacklisted(db, report.url)
        if black:
            print(f"[人工黑名單] 網址 {report.url} 命中黑名單 ({black.title})，直接歸檔為極高風險。")
            _upsert_suspect(
                db, report.url,
                title=f"[{report.task_type}] 人工黑名單",
                keywords_found=", ".join(report.keywords or [])[:500],
                html_content=report.text_content or "",
                images_data="[]",          # 已確認的站不必再留圖佔空間
            )
            def _apply_blacklist(row):
                row.risk_score = 100
                row.risk_level = "極高風險"
                row.nlp_details = f"人工黑名單：{black.reason or black.title or '已確認'}"[:500]
                row.yolo_details = "人工黑名單，未經影像分析"
                row.task_source = f"[{report.task_type}] 爬蟲自動通報"

            ai_row = db.query(database.AIAnalysisResult).filter(
                database.AIAnalysisResult.url == report.url).first()
            if ai_row:
                _apply_blacklist(ai_row)
                db.commit()
            else:
                # 併發時兩個請求會同時走到這裡。url 有唯一索引，
                # 後到的那個會拿到 IntegrityError，改成更新對方剛建好的那列。
                try:
                    ai_row = database.AIAnalysisResult(url=report.url)
                    _apply_blacklist(ai_row)
                    db.add(ai_row)
                    db.commit()
                except IntegrityError:
                    db.rollback()
                    ai_row = db.query(database.AIAnalysisResult).filter(
                        database.AIAnalysisResult.url == report.url).first()
                    if ai_row:
                        _apply_blacklist(ai_row)
                        db.commit()
            return {"status": "blacklisted",
                    "message": f"網址 {report.url} 命中人工黑名單（{black.title}），已直接歸檔為極高風險。"}

        html_text = report.text_content if report.text_content else ""
        if "非毒品" in html_text or "無法正常登入" in html_text or "無法登入" in html_text:
            print(f" [攔截機制啟動] 爬蟲遇到需登入或非目標網站 ({report.url})，直接歸檔為 0 分！")
            
            _upsert_suspect(
                db, report.url,
                title="[系統攔截] 網站需登入或無效",
                keywords_found="",
                html_content=html_text,
                images_data="[]",
            )
            
            existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
            if not existing_record:
                final_score, level = calculate_multimodal_risk_100_scale(0, 0)
                new_ai_record = database.AIAnalysisResult(
                    url=report.url,
                    yolo_details="無影像 (需登入或防爬蟲阻擋)",
                    yolo_score=0,
                    nlp_details=html_text[:50], 
                    nlp_score=0,
                    risk_score=final_score,
                    risk_level=level,
                    task_source=f"[{report.task_type}] 爬蟲自動通報"
                )
                db.add(new_ai_record)
                # 同上。這筆只是「攔截歸檔為 0 分」，對方那列已存在就不必再蓋。
                try:
                    db.commit()
                except IntegrityError:
                    db.rollback()
            else:
                db.commit()
            return {"status": "success", "message": "已成功攔截無效網站，跳過 AI 派發並直接歸檔為 0 分。"}

        extracted_images = []
        
        incoming_images = report.product_images_b64 or report.product_images_base64 or []
        
        print(f"爬蟲傳來的圖片陣列內容：{incoming_images[:2]} ")
        
        for img_obj in incoming_images:
            if isinstance(img_obj, dict):
                base64_str = img_obj.get("base64_data") or img_obj.get("base64") or img_obj.get("data") or img_obj.get("image")
                if base64_str:
                    extracted_images.append(base64_str)
            elif isinstance(img_obj, str):
                extracted_images.append(img_obj)

        # 圖片寫成檔案，資料庫只留路徑。派給 AI 引擎的仍然是記憶體裡這份
        # extracted_images，所以即時流程完全不受影響（見下面的 add_task）。
        image_paths = image_store.save_many(extracted_images)
        images_json_string = json.dumps(image_paths, ensure_ascii=False) if image_paths else "[]"
        keywords_str = ", ".join(report.keywords) if report.keywords else ""

        _upsert_suspect(
            db, report.url,
            title=f"[{report.task_type}] 爬蟲自動通報",
            keywords_found=keywords_str,
            html_content=report.text_content if report.text_content else "",
            images_data=images_json_string,
        )
        db.commit()
        print(f"原始網頁快照寫入成功！成功從包裹中萃取出 {len(extracted_images)} 張圖片。")
        
        try:
            background_tasks.add_task(
                dispatch_to_ai_engines, 
                report.url, 
                report.text_content if report.text_content else "", 
                extracted_images  
            )
            print("背景派發任務已順利啟動！")
        except Exception as ai_err:
            print(f"背景任務加入失敗：{str(ai_err)}")

        return {"status": "success", "message": "資料已接收並解開封裝，自動派發中。"}

    except Exception as e:
        db.rollback()
        # 詳細錯誤只留在伺服器日誌，不要回給客戶端。
        # SQLAlchemy 的例外訊息會夾帶完整 SQL 語句、參數值與內部主機名，
        # 這個端點又沒有驗證，等於免費把資料庫結構送給任何人。
        print(f"嚴重錯誤：/api/crawler/report/ 處理失敗（{report.url}）：{e!r}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="伺服器內部錯誤，請聯繫系統管理員")
@router.post("/api/crawler/result/{result_id}/false-positive/",
             summary="人工覆核：回報誤判，並把該網域加入白名單")
def report_false_positive(
    result_id: int,
    reason: str = Query("", max_length=255, description="誤判原因"),
    db: Session = Depends(get_db),
    # 覆核是承辦人員的日常工作，開放給一般人員。這支的效果等同於
    # POST /api/whitelist/（把網域加進白名單），那支已經開放，
    # 只開一邊會變成「手動加得了、按按鈕加不了」的怪狀態。
    # 用按鈕還比手動輸入網址不容易出錯。
    current_admin: database.User = Depends(get_current_user),
):
    """
    AI 判定的黑名單只提供這一個動作，不提供「單純刪除」。

    因為單純刪除沒有意義：那一筆刪掉之後，下次爬蟲爬到同一個網址還是會
    重新分析、重新出現，使用者會一直刪同一個東西。要讓它真的不再出現，
    就得把網域加進白名單——那才是「這個站是正常的」這件事的正確表達。

    白名單那筆會標記 source=誤判回報，跟主動排除的正常網站分開顯示。
    兩者的意義不同：「誤判回報」代表模型判錯過，那是改善模型的線索，
    混在一起就看不出模型到底錯在哪裡。
    """
    row = db.query(database.AIAnalysisResult).filter(
        database.AIAnalysisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="找不到該筆分析結果")

    url, level = row.url, row.risk_level
    domain = registrable_domain(url)
    if not domain:
        raise HTTPException(status_code=400, detail="網址解析不出網域，無法加入白名單。")

    already = is_whitelisted(db, url)
    if not already:
        db.add(database.WhitelistWebsite(
            url=url,
            title=f"誤判回報：{domain}"[:100],
            reason=(reason or f"AI 誤判為{level}")[:255],
            added_by=current_admin.account,
            source="誤判回報",
        ))

    # 整個網域的分析結果一起清掉，不是只刪按下去的那一筆。
    # 同一個站在待確認裡動輒四五十筆（實測 cathinonelabs.com 48 筆），
    # 只刪一筆的話，使用者把站標成正常之後畫面上還掛著四十幾筆處理不掉的東西：
    # 網域已經在白名單了，再按「回報誤判」也不會有新東西可加。
    removed = purge_analysis_for_domain(db, domain)
    db.commit()

    log_audit_action(
        db, current_admin.user_id, "回報誤判",
        f"回報 {url} 為誤判（原判定：{level}），網域 {domain} 已加入白名單，"
        f"一併清除 {removed} 筆分析結果。"
        f"{('原因：' + reason) if reason else ''}"[:500],
    )
    return {"status": "success", "id": result_id, "url": url, "domain": domain,
            "before": level, "whitelisted": not bool(already), "removed": removed,
            "message": f"已回報誤判，{domain} 已加入白名單" if not already
                       else f"已回報誤判，{domain} 原本就在白名單中"}


@router.post("/api/crawler/result/{result_id}/confirm/",
             summary="人工覆核：確認這筆是毒品網站")
def confirm_result(result_id: int, db: Session = Depends(get_db),
                   # 同上：覆核開放給一般人員，效果等同於 POST /api/blacklist/。
                   current_admin: database.User = Depends(get_current_user)):
    """記下「有人看過並確認是毒品網站」，讓它進入黑名單清單。

    不動 risk_level。等級是規則算的，以前這裡直接寫成「極高風險」，
    結果資料庫裡出現一堆影像 0 分卻標極高的紀錄，解釋不了。
    現在分開：等級表示模型怎麼看，human_verified 表示人怎麼看，
    清單歸屬由後者優先（見 bucket 過濾）。

    更早以前這顆按鈕只改前端記憶體，重新整理就沒了——
    對證據系統來說，「誰在什麼時候確認的」比判定結果本身更重要。
    """
    row = db.query(database.AIAnalysisResult).filter(
        database.AIAnalysisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="找不到該筆分析結果")

    if row.human_verified:
        return {"status": "success", "message": "這筆先前已經確認過",
                "id": row.id, "risk_level": row.risk_level, "already": True}

    row.human_verified = True
    row.human_verified_at = datetime.now()
    row.human_verified_by = current_admin.user_id
    db.commit()

    log_audit_action(
        db, current_admin.user_id, "人工覆核確認",
        f"確認 {row.url} 為毒品網站（模型判定維持：{row.risk_level}）"[:500],
    )
    return {"status": "success", "message": "已確認並移入黑名單",
            "id": row.id, "risk_level": row.risk_level, "already": False}


@router.post("/api/crawler/results/confirm-batch/",
             summary="人工覆核：批次確認多筆為毒品網站")
def confirm_results_batch(
    payload: ConfirmBatch,
    db: Session = Depends(get_db),
    current_admin: database.User = Depends(get_current_user),
):
    """一次確認多筆。

    為什麼要有這支，而不是讓前端迴圈打單筆
    ────────────────────────────────────
    待確認清單一頁最多 200 筆。前端迴圈的話就是 200 個 HTTP 請求、200 次
    commit、200 筆稽核紀錄——慢，而且稽核日誌會被同一個動作洗版，之後要查
    「那天發生什麼事」會很難讀。

    這裡一次查、一次 commit、一筆稽核紀錄（記總數與網址樣本）。

    只處理「還沒被確認過」的那些。已經是極高風險的略過不動，也不計入
    confirmed——重複按不會產生假的統計數字。
    """
    ids = list(dict.fromkeys(payload.ids))      # 去重，保留順序
    if not ids:
        raise HTTPException(status_code=400, detail="沒有選取任何項目。")

    rows = db.query(database.AIAnalysisResult).filter(
        database.AIAnalysisResult.id.in_(ids)).all()
    found = {row.id for row in rows}
    missing = [i for i in ids if i not in found]

    confirmed, skipped = [], []
    for row in rows:
        # 看有沒有人確認過，不是看等級。
        # 用等級判斷的話，規則本來就算成極高的那些永遠被略過，
        # human_verified 就永遠寫不進去。
        if row.human_verified:
            skipped.append(row.id)
            continue
        row.human_verified = True
        row.human_verified_at = datetime.now()
        row.human_verified_by = current_admin.user_id
        confirmed.append(row.url)
    db.commit()

    # 一筆稽核紀錄。details 是 varchar(500)，網址只留前三個當樣本，
    # 其餘用數量表示——重點是「誰、什麼時候、確認了幾筆」。
    if confirmed:
        sample = "、".join(u[:60] for u in confirmed[:3])
        more = f" 等 {len(confirmed)} 筆" if len(confirmed) > 3 else ""
        log_audit_action(
            db, current_admin.user_id, "人工覆核確認（批次）",
            f"批次確認 {len(confirmed)} 筆為毒品網站：{sample}{more}"[:500],
        )

    return {
        "status": "success",
        "message": f"已確認 {len(confirmed)} 筆"
                   + (f"，{len(skipped)} 筆先前已確認" if skipped else "")
                   + (f"，{len(missing)} 筆找不到" if missing else ""),
        "confirmed": len(confirmed),
        "skipped": len(skipped),
        "missing": missing,
    }


@router.get("/api/crawler/result/{result_id}/image/",
            summary="取單筆的 YOLO 代表圖（清單不夾帶，點開明細才抓）")
def get_result_image(result_id: int, db: Session = Depends(get_db),
                     current_user = Depends(get_current_user)):
    """
    代表圖單獨取。放在清單裡的話一頁 50 筆最多會變成近 10 MB，
    而使用者通常只會點開其中一兩筆。
    """
    row = db.query(database.AIAnalysisResult).filter(
        database.AIAnalysisResult.id == result_id).first()
    if not row:
        raise HTTPException(status_code=404, detail="找不到該筆分析結果")
    # 回傳格式不變：前端拿到的仍然是 base64。
    # 只是內容來源從資料庫欄位換成檔案——遷移期間兩種都可能有，
    # 新資料在 representative_image_path，還沒搬的舊資料在 base64 欄位。
    if row.representative_image_path:
        image_b64 = image_store.load_base64(row.representative_image_path) or ""
    else:
        image_b64 = row.representative_image_base64 or ""
    return {
        "id": row.id,
        "representative_image_base64": image_b64,
        "representative_image_detections": row.representative_image_detections or [],
    }


# 24 小時清單以網域分組：一個網域動輒幾十頁，平鋪的話一頁 50 筆常常全是同一個站。
def _domain_expr():
    # 規則只寫在 utils.domain_sql_expr 一處。這裡再寫一份，兩邊遲早不一致——
    # 清單分組看到的網域會跟白名單清掉的網域對不起來。
    return domain_sql_expr(database.AIAnalysisResult.url)


# human_verified 允許 NULL（欄位是後來加的），用 is_() 判斷才不會漏掉舊資料。
def _confirmed_expr():
    return database.AIAnalysisResult.human_verified.is_(True)


def _not_confirmed_expr():
    return or_(database.AIAnalysisResult.human_verified.is_(False),
               database.AIAnalysisResult.human_verified.is_(None))


# risk_level 是字串，排序要照嚴重程度而不是字典序。數字越小越嚴重。
# 這一版不看有沒有人確認過，網域列要靠它顯示「模型自己怎麼看」。
def _model_severity_expr():
    return case(
        (database.AIAnalysisResult.risk_level == "極高風險", 0),
        (database.AIAnalysisResult.risk_level == "高風險 (優先人工覆核)", 1),
        (database.AIAnalysisResult.risk_level == "中風險 (建議人工覆核)", 2),
        else_=3,
    )


# 排序用的那一版：已人工確認排最前（-1），那是人下的結論，比模型判定確定。
# 等級本身不動——「是不是毒品站」跟「模型給幾級」是兩件事，混在同一個欄位
# 就會出現「極高風險但影像 0 分」這種對不上的紀錄。
def _severity_expr():
    return case(
        (_confirmed_expr(), -1),
        (database.AIAnalysisResult.risk_level == "極高風險", 0),
        (database.AIAnalysisResult.risk_level == "高風險 (優先人工覆核)", 1),
        (database.AIAnalysisResult.risk_level == "中風險 (建議人工覆核)", 2),
        else_=3,
    )


_SEVERITY_TO_LEVEL = {
    -1: "已人工確認",
    0: "極高風險",
    1: "高風險 (優先人工覆核)",
    2: "中風險 (建議人工覆核)",
    3: "低風險",
}

_LEVEL_TO_STATUS = {
    "已人工確認": "Blocked",
    "極高風險": "Blocked",
    "高風險 (優先人工覆核)": "Investigation",
    "中風險 (建議人工覆核)": "Investigation",
}


@router.get("/api/crawler/automated_24h_list/", summary="獲取 24 小時自動爬蟲清單")
def get_automated_24h_results(
    db: Session = Depends(get_db), 
    current_user = Depends(get_current_user),
    # ge/le 不能省。沒有下限時 page=-1 會讓 offset 變負數，
    # 沒有上限時 limit=999999 會把整張表倒出來（SEC-16）。
    page: int = Query(1, ge=1, description="當前頁碼 (預設第 1 頁)"),
    limit: int = Query(50, ge=1, le=200, description="每頁顯示幾筆，上限 200"),
    # pattern 限定合法值。不限的話打錯字（bucket=blaclist）會靜靜地回傳全部，
    # 呼叫端以為自己有過濾、其實沒有——那比直接報錯難查得多。
    bucket: Optional[str] = Query(
        None,
        pattern="^(blacklist|pending)$",
        description="blacklist=極高風險；pending=待人工覆核（高風險+中風險）；不給則全部",
    ),
    q: Optional[str] = Query(None, max_length=200,
                             description="關鍵字搜尋：網址或案件編號"),
    group: Optional[str] = Query(
        None, pattern="^domain$",
        description="group=domain 時以網域為單位回傳摘要，一個網域一筆"),
    domain: Optional[str] = Query(
        None, max_length=253,
        description="只回傳這個網域底下的網頁（展開某個網域時用）"),
):
    
    base_query = db.query(database.AIAnalysisResult).filter(
        database.AIAnalysisResult.task_source.like("%[automated_24h]%")
    )

    # bucket 讓前端不必自己撈全部再過濾。
    # 「高風險」歸在 pending 而不是 blacklist：那一級的全名是「高風險 (優先人工覆核)」，
    # 意思是要優先給人看，不是已經確認是毒品網站。
    if q and q.strip():
        keyword = q.strip()
        # 案件編號在前端是 ai_analysis_results.id（AIDetection.tsx 的
        # caseNumber 找不到 case_number 時就退回 id），所以純數字要一起比對 id。
        # 用 or_ 而不是分開兩個 filter——分開會變成 AND，兩個條件不可能同時成立。
        conditions = [database.AIAnalysisResult.url.like(
            like_pattern(keyword), escape="\\")]
        if keyword.isdigit():
            conditions.append(database.AIAnalysisResult.id == int(keyword))
        base_query = base_query.filter(or_(*conditions))

    # 人的結論優先，沒有人的結論才看模型。
    # 只看 risk_level 的話，確認過但影像不到 30 的（規則算成高風險）
    # 會永遠留在待確認清單裡，等於確認了也沒用。
    if bucket == "blacklist":
        base_query = base_query.filter(or_(
            database.AIAnalysisResult.risk_level == "極高風險",
            _confirmed_expr()))
    elif bucket == "pending":
        base_query = base_query.filter(
            database.AIAnalysisResult.risk_level.in_(
                ["高風險 (優先人工覆核)", "中風險 (建議人工覆核)"]),
            _not_confirmed_expr())

    # 展開某個網域：只留這個網域的網頁，其餘照原本的平鋪流程走
    if domain and domain.strip():
        target = domain.strip().lower()
        if target.startswith("www."):
            target = target[4:]
        base_query = base_query.filter(_domain_expr() == target)

    # 以網域為單位的摘要清單。分頁也是以網域為單位——不然同一個網域會被
    # 切在好幾頁，摘要列上的頁數也會對不起來。
    if group == "domain":
        dom = _domain_expr().label("domain")
        sev = _severity_expr()
        grouped = base_query.with_entities(
            dom,
            func.count().label("page_count"),
            func.max(database.AIAnalysisResult.risk_score).label("max_score"),
            func.max(database.AIAnalysisResult.yolo_score).label("max_yolo"),
            # 這個網域底下有幾頁已經被人確認過
            func.sum(case((_confirmed_expr(), 1), else_=0)).label("verified_count"),
            # 模型自己怎麼看（不含人工確認的影響）
            func.min(_model_severity_expr()).label("model_worst"),
            # 數字越小越嚴重，MIN 就是這個網域裡最嚴重的那一頁
            func.min(sev).label("worst"),
            func.max(database.AIAnalysisResult.created_at).label("latest"),
        ).group_by(dom)

        domain_total = grouped.count()

        # 統計改成算「網域數」，跟清單的單位一致。
        # 沿用原本的筆數統計的話，清單顯示 50 個網域、上面卻寫著幾千筆，對不起來。
        sub = grouped.subquery()
        sev_counts = dict(
            db.query(sub.c.worst, func.count()).group_by(sub.c.worst).all())
        d_high = sev_counts.get(0, 0)
        d_med = sev_counts.get(1, 0) + sev_counts.get(2, 0)
        d_low = sev_counts.get(3, 0)

        rows = (grouped
                .order_by(func.min(sev),
                          func.max(database.AIAnalysisResult.risk_score).desc(),
                          # 文字同分才看影像，跟平鋪清單用同一套順位
                          func.max(database.AIAnalysisResult.yolo_score).desc(),
                          func.max(database.AIAnalysisResult.created_at).desc())
                .offset((page - 1) * limit).limit(limit).all())

        domain_data = []
        for row in rows:
            level = _SEVERITY_TO_LEVEL.get(int(row.worst), "低風險")
            domain_data.append({
                "domain": row.domain,
                "page_count": row.page_count,
                # 摘要列顯示這個網域「最嚴重的那一頁」，不是平均——
                # 分流的目的是先看最該看的，平均會把一頁高分稀釋掉。
                "risk_score": row.max_score,
                # 影像分數要一起給。等級是「文字 + 影像」的二維判斷，
                # 只顯示文字分數的話，畫面上會出現「文字 100 分卻是高風險」
                # 排在「文字 95 分的極高風險」後面，看起來自相矛盾。
                "yolo_score": row.max_yolo,
                "risk_level": level,
                "verified_count": int(row.verified_count or 0),
                "model_risk_level": _SEVERITY_TO_LEVEL.get(int(row.model_worst), "低風險"),
                "status": _LEVEL_TO_STATUS.get(level, "Monitored"),
                "discovered_date": row.latest.strftime("%Y-%m-%d") if row.latest else None,
            })

        return {
            "status": "success",
            "message": "成功獲取 24 小時自動爬蟲清單（以網域分組）",
            "total_count": domain_total,
            "stats": {
                "total": domain_total,
                "high": d_high,
                "medium": d_med,
                "low": d_low,
            },
            "pagination": {
                "total_count": domain_total,
                "current_page": page,
                "limit": limit,
                "total_pages": (domain_total + limit - 1) // limit if limit > 0 else 0,
            },
            "data": domain_data,
        }

    # 統計也依 risk_level，不要再用 risk_score 自己切一套門檻
    total_count = base_query.count()
    # 條件要跟 bucket 一致，不然清單顯示 N 筆、上面的數字寫別的
    high_risk_count = base_query.filter(or_(
        database.AIAnalysisResult.risk_level == "極高風險",
        _confirmed_expr())).count()
    med_risk_count = base_query.filter(
        database.AIAnalysisResult.risk_level.in_(
            ["高風險 (優先人工覆核)", "中風險 (建議人工覆核)"]),
        _not_confirmed_expr()).count()
    low_risk_count = total_count - high_risk_count - med_risk_count
    skip = (page - 1) * limit
    if domain and domain.strip():
        # 展開某個網域時，最嚴重的那幾頁要排最前面。
        # 用預設的時間排序的話，摘要列寫著 100 分、點開卻是一排 62 分，
        # 看的人會以為對不上——那一頁其實在第三頁。
        order = [
            _severity_expr(),
            database.AIAnalysisResult.risk_score.desc(),
            database.AIAnalysisResult.yolo_score.desc(),   # 同分才看影像，理由同下
            database.AIAnalysisResult.created_at.desc(),
        ]
    elif bucket == "pending":
        # 高風險排在中風險前面（"高" < "中" 的字典序剛好相反，所以明寫順序），
        # 同一級再依分數高到低。人力有限時要先看最該看的。
        order = [
            case((database.AIAnalysisResult.risk_level == "高風險 (優先人工覆核)", 0),
                 else_=1),
            database.AIAnalysisResult.risk_score.desc(),
            # 文字分數相同時才看影像。影像單獨的判別力接近隨機，
            # 只適合當同分時的次要鍵，不能加權混進主要排序。
            database.AIAnalysisResult.yolo_score.desc(),
            database.AIAnalysisResult.created_at.desc(),
        ]
    else:
        order = [database.AIAnalysisResult.created_at.desc()]

    results = base_query.order_by(*order).offset(skip).limit(limit).all()

    frontend_data = []
    
    for index, ai_record in enumerate(results, start=1):
        date_str = "2024-12-01" 
        if hasattr(ai_record, 'created_at') and ai_record.created_at:
            date_str = ai_record.created_at.strftime("%Y-%m-%d")

        # status 直接對應 risk_level，不要另外拿 risk_score 算一套門檻。
        # 以前這裡用 85/75，utils.py 用 74/35，同一筆資料在報表和清單上
        # 會顯示成不同等級——68 分在 risk_level 是「中風險」，在這裡卻是
        # 最低的 Monitored。
        status = {
            "極高風險": "Blocked",
            "高風險 (優先人工覆核)": "Investigation",
            "中風險 (建議人工覆核)": "Investigation",
        }.get(ai_record.risk_level, "Monitored")

        frontend_data.append({
            "id": ai_record.id,                                 
            "domain_name": ai_record.url,                
            "server_location": "Unknown",                
            "risk_score": ai_record.risk_score,          
            "discovered_date": date_str,                 
            "status": status,                            
            "task_source": ai_record.task_source, 
            "risk_level": ai_record.risk_level,
            # 「人確認過」與「模型判極高」是獨立的兩件事，前端要分開顯示
            "human_verified": bool(ai_record.human_verified),
            "human_verified_at": (ai_record.human_verified_at.isoformat()
                                  if ai_record.human_verified_at else None),
            "human_verified_by": ai_record.human_verified_by,
            "yolo_details": ai_record.yolo_details,
            "yolo_score": ai_record.yolo_score,
            "nlp_details": ai_record.nlp_details,
            "nlp_score": ai_record.nlp_score,
            "class_metadata": ai_record.class_metadata,
            # 代表圖不放進清單：每張 base64 可到 600 KB，一頁 50 筆近 10 MB。
            # API 本身都在 0.3 秒內，慢的是傳輸與瀏覽器解碼。
            # 這裡只回布林值，圖由 /api/crawler/result/{id}/image/ 按需取。
            "has_representative_image": bool(ai_record.representative_image_path
                                             or ai_record.representative_image_base64),
            # ocr_results 不回傳。前端不顯示 OCR——圖片裡的文字是拿去餵 NLP、
            # 影響風險分數本身（utils.py 的 analyze_ocr_text_with_nlp），
            # 不是多一個給人看的區塊。回傳它只是讓每一頁的 payload 白白變大。
            # 要追查原始 OCR 結果的話，資料庫的 ai_analysis_results.ocr_results 還在。
        })

    return {
        "status": "success",
        "message": "成功獲取 24 小時自動爬蟲清單",
        "total_count": total_count, 
        "stats": {
            "total": total_count,
            "high": high_risk_count,
            "medium": med_risk_count,
            "low": low_risk_count
        },
        "pagination": {
            "total_count": total_count,
            "current_page": page,
            "limit": limit,
            "total_pages": (total_count + limit - 1) // limit if limit > 0 else 0
        },
        "data": frontend_data
    }

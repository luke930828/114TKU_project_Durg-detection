#  模組二：輸入網址識別 
import os
import time
import traceback
from datetime import datetime

import requests
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

import database
from dependencies import get_db, get_current_user, log_audit_action
from utils import is_whitelisted as is_whitelisted_domain
from schemas import FrontendScanRequest

router = APIRouter(tags=["網址即時識別模組"])

# 一筆未完成的紀錄要放置多久，才判定它是卡住、值得重派一次爬蟲。
# 一頁的正常流程（爬蟲 → NLP → YOLO 逐張推論）大約一到兩分鐘，抓五分鐘留足餘裕。
RETRY_AFTER_SECONDS = 300


@router.post("/api/scan_target/", summary="即時掃描單一網址（具備未完成任務自動修復機制）")
def scan_target_url(request_data: FrontendScanRequest, db: Session = Depends(get_db), current_user = Depends(get_current_user)): 
    target_url = request_data.url
    log_audit_action(db, current_user.user_id, "網址掃描", f"查詢網址：{target_url}"[:500])
    
   # 1. 白名單檢查
    # 比網域不是比完整網址，理由同 crawler.py
    is_whitelisted = is_whitelisted_domain(db, target_url)
    if is_whitelisted:
        return {
            "status": "safe", 
            "source": "whitelist", 
            "message": "此網址已列入白名單，安全放行。", 
            "reason": is_whitelisted.reason,
            "data": {
                "url": target_url,
                "risk_score": 0,
                "risk_level": "無風險",
                "yolo_details": "白名單授權，略過影像分析",
                "nlp_details": f"白名單授權原因：{is_whitelisted.reason}"
            }
        }
    # 2. 歷史紀錄檢查
    existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == target_url).first()
    
    if existing_record:
        is_incomplete = (existing_record.yolo_details == "影像分析中...") or (existing_record.nlp_details == "文字分析中...")
        
        if not is_incomplete:
            return {
                "status": "success",
                "source": "history",
                "message": "偵測到完整的歷史展示紀錄，直接回傳 AI 分析結果。",
                "data": existing_record
            }
        else:
            # 未完成的紀錄要不要重派，取決於它卡多久了。
            # 前端每 20 秒輪詢一次，而輪詢打的就是這支端點——無條件重派等於每 20 秒
            # 重爬同一頁，把 AI 引擎的佇列灌爆，反而更跑不完。
            # 只有超過 RETRY_AFTER_SECONDS 才重派，其餘直接回報處理中。
            age = None
            if existing_record.created_at:
                age = (datetime.now() - existing_record.created_at).total_seconds()

            if age is not None and age < RETRY_AFTER_SECONDS:
                return {
                    "status": "processing",
                    "source": "in_progress",
                    "message": "這個網址正在分析中，請稍候。",
                }

            print(f"發現卡住的歷史紀錄 ({target_url}，已經 {age} 秒)，可能上次有 AI 引擎離線，系統自動重新派發任務...")

    # 3. 呼叫爬蟲 (不管是全新網址，還是要修復半殘紀錄，都會走到這裡)
    # 同 utils.py：沒設就爆掉，不要靜靜連到某台特定機器
    CRAWLER_API_URL = os.environ["CRAWLER_API_URL"]
    try:
        payload = {
            "url": target_url,
            "save_local": False
        }
        
        response = requests.post(CRAWLER_API_URL, json=payload, timeout=15)
        response.raise_for_status() 
        
        return {
            "status": "processing",
            "source": "crawler",
            "message": "系統正在進行背景分析與自動修復，請稍候刷新頁面。"
        }

    except requests.exceptions.RequestException as req_err:
        db.rollback()
        # 原本這裡把 str(req_err) 回給前端，會洩漏爬蟲的內部位址與埠號。
        print(f"呼叫爬蟲引擎失敗（{target_url}）：{req_err!r}")
        return {
            "status": "error",
            "message": "無法連線至爬蟲引擎，請確認該服務是否正常運作。"
        }
    except Exception as e:
        db.rollback()
        print(f"即時識別處理失敗（{target_url}）：{e!r}")
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="即時識別處理失敗，請聯繫系統管理員")


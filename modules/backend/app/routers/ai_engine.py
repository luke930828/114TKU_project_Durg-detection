from fastapi import APIRouter, BackgroundTasks, Depends
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
import database
import image_store
from schemas import YOLOAnalysisReport, NLPAnalysisReport
from dependencies import get_db, verify_internal_token
from utils import rescore_with_ocr_text, calculate_multimodal_risk_100_scale


def _store_image(b64):
    """代表圖存成檔案，回傳相對路徑；沒有圖就回 None。

    存檔案而不是塞進 LONGTEXT 欄位：那個欄位過去讓 ai_analysis_results
    長到 2.3 GB，把 MySQL 的快取佔滿。實測讀檔還比讀資料庫快
    （中位 0.23 ms vs 0.37 ms），API 回傳的格式完全不變。
    """
    if not b64:
        return None
    paths = image_store.save_many([b64])
    return paths[0] if paths else None


router = APIRouter(tags=["AI 引擎分析結果接收"])

# 模組七：YOLO 獨立分析結果接收通道
@router.post("/api/ai_result/report/", summary="YOLO 引擎專用：接收影像與分數並自動統整")
def receive_ai_analysis_result(
    report: YOLOAnalysisReport,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _internal: bool = Depends(verify_internal_token),
):
    # 一定要截斷：yolo_details / nlp_details 是 varchar(500)，超長會讓整個請求 500。
    # 三種情況要寫成三種不同的字：有檢出 → 類別清單；有圖但沒檢出 → 「無檢出影像特徵」；
    # 根本沒有圖 → 「無影像可分析」。最後一種若留著「影像分析中...」，前端會永遠等不到結束。
    if report.yolo_objects:
        yolo_str = ", ".join(report.yolo_objects)[:500]
    elif report.no_images:
        yolo_str = "無影像可分析（這一頁沒有商品圖）"
    else:
        yolo_str = "無檢出影像特徵"

    # ocr_results 在 schema 裡是 OCRResults 這個 Pydantic 模型（不是 dict），
    # 直接指派給 JSON 欄位的話 SQLAlchemy 序列化不了會丟 TypeError。
    # 這裡轉成 dict 一次，下面三個寫入點共用。
    ocr_payload = report.ocr_results.model_dump() if report.ocr_results else None

    # 圖片裡的文字要跟網頁文字合併再送 NLP：只送 OCR 的話，模型拿到的是一袋
    # 沒有上下文的碎片，會亂判。合併後長度上限改用 512，詳見 utils.py。
    #
    # 排在背景做——這支端點是 YOLO 在等回應的，而它的 timeout 只有 5 秒。
    if ocr_payload:
        background_tasks.add_task(rescore_with_ocr_text, report.url, ocr_payload)
    existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
    suspect = db.query(database.SuspectWebsite).filter(database.SuspectWebsite.url == report.url).first()
    source_title = suspect.title if suspect else "未知來源"
    
    if existing_record:
        existing_record.yolo_details = yolo_str
        existing_record.yolo_score = report.risk_score

        existing_record.task_source = source_title
        
        existing_record.class_metadata = report.class_metadata
        existing_record.representative_image_path = _store_image(report.representative_image_base64)
        existing_record.representative_image_base64 = None
        existing_record.representative_image_detections = report.representative_image_detections
        existing_record.ocr_results = ocr_payload
        
        current_nlp_score = existing_record.nlp_score or 0
        final_score, level = calculate_multimodal_risk_100_scale(current_nlp_score, existing_record.yolo_score)

        existing_record.risk_score = final_score
        # 人工確認過的不覆蓋等級。分數照更新（那是模型的最新看法），
        # 但「這是毒品網站」是人下的結論，不該被下一次自動分析改掉。
        # 實際發生過：三筆人工確認的紀錄被後續的影像補跑重算回「高風險」。
        if not existing_record.human_verified:
            existing_record.risk_level = level
        db.commit()
        return {"status": "success", "message": f"成功統整！已將 YOLO 影像與分數補算至 {report.url}"}
    else:
        try:
            final_score, level = calculate_multimodal_risk_100_scale(0, report.risk_score)
            
            new_record = database.AIAnalysisResult(
                url=report.url, yolo_details=yolo_str, yolo_score=report.risk_score,
                nlp_details="文字分析中...", nlp_score=0, risk_score=final_score, risk_level=level,
                class_metadata=report.class_metadata,
                representative_image_path=_store_image(report.representative_image_base64),
                representative_image_detections=report.representative_image_detections,
                ocr_results=ocr_payload,
                task_source=source_title
            )
            db.add(new_record)
            db.commit() 
            return {"status": "success", "message": f"成功建檔！已為 {report.url} 建立全新 AI 影像紀錄。"}
        
        except IntegrityError:
            db.rollback() 
            real_existing = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
            if real_existing:
                real_existing.yolo_details = yolo_str
                real_existing.yolo_score = report.risk_score
                
                real_existing.class_metadata = report.class_metadata
                real_existing.representative_image_path = _store_image(report.representative_image_base64)
                real_existing.representative_image_base64 = None
                real_existing.representative_image_detections = report.representative_image_detections
                real_existing.ocr_results = ocr_payload
                real_existing.task_source = source_title
                current_nlp_score = real_existing.nlp_score or 0
                final_score, level = calculate_multimodal_risk_100_scale(current_nlp_score, real_existing.yolo_score)
                real_existing.risk_score = final_score
                # 人工確認過的不覆蓋等級，理由同上。
                if not real_existing.human_verified:
                    real_existing.risk_level = level
                db.commit()
            return {"status": "success", "message": "遭遇併發衝突，已轉為更新模式寫入！"}
# 模組八：NLP 獨立分析結果接收通道
@router.post("/api/nlp/report/", summary="NLP 引擎專用：接收可疑文字與分數並自動統整")
def receive_nlp_analysis_result(
    report: NLPAnalysisReport,
    db: Session = Depends(get_db),
    _internal: bool = Depends(verify_internal_token),
):
    nlp_str = (", ".join(report.nlp_keywords) if report.nlp_keywords
               else "無檢出文字特徵")[:500]
    existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
    suspect = db.query(database.SuspectWebsite).filter(database.SuspectWebsite.url == report.url).first()
    source_title = suspect.title if suspect else "未知來源"
    
    if existing_record:
        existing_record.nlp_details = nlp_str
        existing_record.nlp_score = report.risk_score
        existing_record.task_source = source_title
        
        current_yolo_score = existing_record.yolo_score or 0
        final_score, level = calculate_multimodal_risk_100_scale(report.risk_score, current_yolo_score)
        
        existing_record.risk_score = final_score
        # 人工確認過的不覆蓋等級，理由同上。
        if not existing_record.human_verified:
            existing_record.risk_level = level
        db.commit()
        return {"status": "success", "message": f"成功統整！已將 NLP 文字與分數補充至 {report.url}"}
    else:
        try:
            final_score, level = calculate_multimodal_risk_100_scale(report.risk_score, 0)
            new_record = database.AIAnalysisResult(
                url=report.url, yolo_details="影像分析中...", yolo_score=0, 
                nlp_details=nlp_str, nlp_score=report.risk_score, risk_score=final_score, risk_level=level,         
                task_source=source_title
            )
            db.add(new_record)
            db.commit()
            return {"status": "success", "message": f"成功建檔！已為 {report.url} 建立全新 AI 文字紀錄。"}
        except IntegrityError:
            db.rollback() 
            real_existing = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
            if real_existing:
                real_existing.nlp_details = nlp_str
                real_existing.nlp_score = report.risk_score
                real_existing.task_source = source_title
                
                current_yolo_score = real_existing.yolo_score or 0
                final_score, level = calculate_multimodal_risk_100_scale(report.risk_score, current_yolo_score)
                
                real_existing.risk_score = final_score
                # 人工確認過的不覆蓋等級，理由同上。
                if not real_existing.human_verified:
                    real_existing.risk_level = level
                db.commit()
            return {"status": "success", "message": "遭遇併發衝突，已成功將 NLP 轉為更新模式寫入！"}

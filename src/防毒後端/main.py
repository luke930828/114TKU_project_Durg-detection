from fastapi import FastAPI, Depends, HTTPException, Header, UploadFile, File, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import List, Dict, Any, Optional 
import requests
import json     
import database  
import uuid
from sqlalchemy.exc import IntegrityError # 
app = FastAPI(title="多模態毒品防制系統 API", description="符合原始表與 AI 展示表分離架構")

# NLP 控分覆寫規則的關鍵字表 —— 唯一標準來源，跟 modules/yolo/app/ai_model/scoring.py 的 15 類權重表是同一層級的設定
NLP_WHITELIST_TERMS = ["無尼古丁", "普洱茶葉", "漢方草本", "薄荷涼糖", "合法軟糖"]  # 規則 A：命中即一票否決，強制降到低風險
NLP_BLACKLIST_TERMS = ["thc", "cbd", "依託咪酯", "喪屍煙油", "飛行", "埋車"]      # 規則 B：命中且視覺側沒有強特徵（沒偵測到東西、或只有中性載具）時，補刀升級成高風險
VISUAL_CARRIER_ONLY_CLASSES = {"ziplock_bag"}                                    # 規則 B 判定「只有載具」的白名單類別（medical_bottle 已隨 scoring.py 一併移除）


def parse_stored_list(details: Optional[str]) -> List[str]:
    """把用 ', ' join 存進 DB 的字串（yolo_details/nlp_details）還原成 list，尚未有資料的佔位字串一律當成空清單。"""
    placeholders = {None, "", "文字分析中...", "影像分析中...", "無檢出影像特徵", "無檢出文字特徵"}
    if details in placeholders:
        return []
    return details.split(", ")


def apply_nlp_override(score: int, nlp_keywords: Optional[List[str]], yolo_objects: Optional[List[str]]):
    """
    給 NLP 團隊的控分覆寫權：
    規則 A（白名單一票否決）：文字側讀到合法商業標籤，不管視覺分數多高都強制壓到低風險。
    規則 B（黑名單補刀升級）：文字側讀到違禁關鍵字，且視覺側沒有強特徵佐證時，強制拉到高風險告警。
        「沒有強特徵」包含兩種情況：YOLO 什麼都沒偵測到（空集合），或只偵測到中性載具
        （VISUAL_CARRIER_ONLY_CLASSES，目前是 ziplock_bag）。
        注意：空集合在 Python 是 falsy，若寫成 `objects_set and objects_set.issubset(...)`
        會導致「YOLO 完全沒抓到東西」這個最該補刀升級的情境反而不觸發規則 B，這裡改用
        「集合差集是否為空」來判斷，empty set 差集後仍是 empty，自然涵蓋這個情境。
    """
    keywords_text = " ".join(nlp_keywords or []).lower()

    if any(term.lower() in keywords_text for term in NLP_WHITELIST_TERMS):
        return min(score, 15), "NLP_WHITELIST_OVERRIDE"

    objects_set = set(yolo_objects or [])
    strong_classes_detected = objects_set - VISUAL_CARRIER_ONLY_CLASSES
    if not strong_classes_detected:
        if any(term.lower() in keywords_text for term in NLP_BLACKLIST_TERMS):
            return max(score, 85), "NLP_BLACKLIST_OVERRIDE"

    return score, None


def calculate_multimodal_risk_100_scale(
    nlp_raw_score: int,
    yolo_raw_score: int,
    nlp_keywords: Optional[List[str]] = None,
    yolo_objects: Optional[List[str]] = None,
):
    """
    根據 NLP 與 YOLO 的原始分數 (0~100)，計算雙引擎加權總分，並套用 NLP 控分覆寫規則後得出風險等級。
    """
    w_text = 0.6
    w_image = 0.4

    s_final = int((w_text * nlp_raw_score) + (w_image * yolo_raw_score))
    s_final, override_reason = apply_nlp_override(s_final, nlp_keywords, yolo_objects)
    if override_reason:
        print(f"⚖️ [NLP 控分覆寫] 觸發 {override_reason}，最終分數強制調整為 {s_final}")

    if s_final > 74:
        risk_level = "極高風險"
    elif 35 <= s_final <= 74:
        risk_level = "中風險 (建議人工覆核)"
    else:
        risk_level = "低風險"

    return s_final, risk_level
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def get_db():
    session = Session(bind=database.engine)
    try:
        yield session
    finally:
        session.close()

def verify_admin(x_token: str = Header(...), db: Session = Depends(get_db)):
    user = db.query(database.User).filter(database.User.account == x_token).first()
    if not user:
        raise HTTPException(status_code=401, detail="身分驗證失敗：無效的憑證！")
    if user.role != "系統管理員":
        raise HTTPException(status_code=403, detail="權限不足：只有系統管理員可以執行此動作！")
    return user

def verify_super_admin(current_user: database.User = Depends(verify_admin)):
    if current_user.account != "super_admin":
        raise HTTPException(status_code=403, detail="權限不足：此操作僅限「總管理員」執行！")
    return current_user

class UserLogin(BaseModel):
    account: str
    password: str

class FrontendScanRequest(BaseModel):
    url: str

class WhitelistCreate(BaseModel):
    url: str
    title: str
    reason: str


class WebsiteReport(BaseModel):
    task_type: Optional[str] = "unknown"  # 加上 Optional，就算他沒傳也不會報錯 500
    timestamp: Optional[str] = "unknown"  # 加上 Optional
    keywords: Optional[List[str]] = []    # 加上 Optional
    url: str                              # 💡 因為你跟爬蟲說好一定會傳網址，所以這個保留原樣！
    screenshot_b64: Optional[str] = None
    full_screenshot_base64: Optional[str] = None
    product_images_b64: Optional[List[Any]] = None    # 這裡改用 Any，防禦格式錯誤
    product_images_base64: Optional[List[Any]] = None # 這裡改用 Any，防禦格式錯誤
    text_content: Optional[str] = None

class YOLOAnalysisReport(BaseModel):
    url: str
    risk_score: int
    yolo_objects: List[str] = []
    processed_images: Optional[List[str]] = []
    class_metadata: Optional[Dict[str, Any]] = None  # 每個類別獨立的 count / max_confidence，來自 api_server.py 的視覺計分模組
    representative_image_base64: Optional[str] = None  # 這批圖片裡分數最高的那張代表圖，供前端畫框展示
    representative_image_detections: Optional[List[Dict[str, Any]]] = None  # 代表圖對應的偵測框：[{class_name, confidence, box:[x1,y1,x2,y2]（0~1 正規化座標）}]
    ocr_results: Optional[Dict[str, Any]] = None  # {engine, detected_texts:[{text, confidence, box_format, box}]}，整批每張圖 OCR 抓到的文字彙整

class NLPAnalysisReport(BaseModel):
    url: str
    risk_score: int
    nlp_keywords: List[str] = []
def dispatch_to_ai_engines(url: str, html_content: str, images: list):
    NLP_API_URL = "http://100.69.185.94:8000/api/nlp/report/"
    YOLO_API_URL = "http://100.101.167.105:5000/api/v1/predict/trigger"
    crawler_API_URL = "http://100.122.162.47:8001/api/crawler/report/"
    FRONTEND_API_URL ="http://100.123.184.43:8002/api/scan_target/"
    generated_task_id = str(uuid.uuid4())[:8]

   # ------------------------------------------
    # 🗣️ 第一階段：派發給 NLP 的任務 (文字)
    # ------------------------------------------
    try:
        nlp_payload = {
            "url": url,
            "text": html_content  
        }
        print(f"準備將文字派發給 NLP...")
        
        response = requests.post("http://100.69.185.94:8000/predict", json=nlp_payload, timeout=10)
        
        if response.status_code == 200:
            nlp_result = response.json()
            print(f"NLP 分析完成！收到結果：{nlp_result}")
            
            score_float = nlp_result.get("score", 0)
            risk_score_int = int(score_float * 100)
            
            internal_payload = {
                "url": url,
                "risk_score": risk_score_int,
                "nlp_keywords": nlp_result.get("keywords", [])
            }
            requests.post("http://127.0.0.1:8000/api/nlp/report/", json=internal_payload)
            print("NLP 結果已成功同步至資料庫！")
            
    except requests.exceptions.Timeout:
        print("呼叫 NLP 逾時！")
    except Exception as e:
        print(f"派發至 NLP 引擎失敗: {e}")

    # ------------------------------------------
    # 📸 第二階段：派發給 YOLO 的任務
    # ------------------------------------------
    if images and len(images) > 0:
        print(f"準備將 {len(images)} 張圖片逐一派發給 YOLO...")
        
        for index, single_image_str in enumerate(images):
            try:
                yolo_payload = {
                    "task_id": f"{generated_task_id}_{index}", 
                    "url": url,
                    "image_base64": single_image_str,
                    "total_images": len(images),  
                    "priority": 0
                }
                
                print(f"   發送第 {index+1}/{len(images)} 張圖片給 YOLO...")
                
                response = requests.post(YOLO_API_URL, json=yolo_payload, timeout=5)
                print(f"    第 {index+1} 張派發成功！對方回應: {response.text}")

            except Exception as e:
                print(f"    第 {index+1} 張圖片派發至 YOLO 失敗: {e}")
#  模組一：管理員登入
@app.post("/api/login/", summary="系統登入")
def login_for_access_token(login_data: UserLogin, db: Session = Depends(get_db)):
    user = db.query(database.User).filter(database.User.account == login_data.account).first()
    if not user:
        raise HTTPException(status_code=401, detail="登入失敗：帳號或密碼錯誤！")

    is_password_correct = False
    if user.account == 'super_admin' and login_data.password == 'super_secret_hash':
        is_password_correct = True
    elif user.password_hash == login_data.password + "_hashed":
        is_password_correct = True

    if not is_password_correct:
        raise HTTPException(status_code=401, detail="登入失敗：帳號或密碼錯誤！")

    return {
        "status": "success",
        "message": f"登入成功！歡迎回來，{user.account}",
        "access_token": user.account
    }


#  模組二：輸入網址識別 (右上角卡片) -> 僅操作 AIAnalysisResult 表
import time 


@app.post("/api/scan_target/", summary="即時掃描單一網址（具備未完成任務自動修復機制）")
def scan_target_url(request_data: FrontendScanRequest, db: Session = Depends(get_db)):
    target_url = request_data.url
    
    # 1. 白名單檢查
    is_whitelisted = db.query(database.WhitelistWebsite).filter(database.WhitelistWebsite.url == target_url).first()
    if is_whitelisted:
        return {"status": "safe", "source": "whitelist", "message": "此網址已列入白名單，安全放行。", "reason": is_whitelisted.reason}

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
            print(f"發現未完成的歷史紀錄 ({target_url})，可能上次有 AI 引擎離線，系統自動重新派發任務...")

    # 3. 呼叫爬蟲 (不管是全新網址，還是要修復半殘紀錄，都會走到這裡)
    CRAWLER_API_URL = "http://100.122.162.47:8000/api/v1/crawl" 
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
        return {
            "status": "error",
            "message": f"無法連線至爬蟲引擎，請確認對方伺服器是否開啟。詳細錯誤：{str(req_err)}"
        }
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"即時識別處理失敗：{str(e)}")


#  模組三：查詢已識別網站 (右下角卡片) ->  鎖定只從 AIAnalysisResult 撈取
@app.get("/api/crawler/report/", summary="獲取前端專用 AI 分析黑名單報表")
def get_frontend_report(current_user: database.User = Depends(verify_admin), db: Session = Depends(get_db)):
    results = db.query(database.AIAnalysisResult).all()
    return {
        "status": "success",
        "message": "成功抓取最新 AI 多模態識別資料庫",
        "total_count": len(results),
        "data": results
    }

# 模組四：爬蟲專用通道 (支援字典解開封裝 + 抓猴雷達 + 🌟 融合無效網站攔截器)
@app.post("/api/crawler/report/", summary="爬蟲端專用：將原始結果寫入 suspect_websites 表")
def receive_crawler_raw_data(report: WebsiteReport, background_tasks: BackgroundTasks, db: Session = Depends(get_db)):
    try:
        print(f"收到爬蟲通報網址：{report.url}")
        
        # =========================================================
        # 🛡️ 【新增防線：無效網站攔截器】(插在你原本的邏輯最前面)
        # =========================================================
        html_text = report.text_content if report.text_content else ""
        if "非毒品" in html_text or "無法正常登入" in html_text or "無法登入" in html_text:
            print(f"🛑 [攔截機制啟動] 爬蟲遇到需登入或非目標網站 ({report.url})，直接歸檔為 0 分！")
            
            # 1. 寫入 suspect_websites (當作紀錄)
            new_website = database.SuspectWebsite(
                url=report.url,
                title="[系統攔截] 網站需登入或無效",
                keywords_found="",
                reported_by="爬蟲端自動上傳",
                html_content=html_text,    
                images_data="[]" 
            )
            db.add(new_website)
            
            # 2. 建立「已結案」的 0 分展示紀錄給前端撈取
            existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
            if not existing_record:
                final_score, level = calculate_multimodal_risk_100_scale(0, 0)
                new_ai_record = database.AIAnalysisResult(
                    url=report.url,
                    yolo_details="無影像 (需登入或防爬蟲阻擋)",
                    yolo_score=0,
                    nlp_details=html_text[:50], # 直接把「無法登入」這句話印在前端畫面上
                    nlp_score=0,
                    risk_score=final_score,
                    risk_level=level
                )
                db.add(new_ai_record)
            
            db.commit()
            return {"status": "success", "message": "已成功攔截無效網站，跳過 AI 派發並直接歸檔為 0 分。"}
            # ⛔ 只要進了這個 if，程式就會在這裡 return 結束，不會往下跑！
        # =========================================================

        # 👇 以下完全保留你原本寫好的完美邏輯，沒有做任何更改：
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

        images_json_string = json.dumps(extracted_images, ensure_ascii=False) if extracted_images else "[]"
        keywords_str = ", ".join(report.keywords) if report.keywords else ""

        new_website = database.SuspectWebsite(
            url=report.url,
            title=f"[{report.task_type}] 爬蟲自動通報",
            keywords_found=keywords_str,
            reported_by="爬蟲端自動上傳",
            html_content=report.text_content if report.text_content else "",    
            images_data=images_json_string 
        )
        db.add(new_website)
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
        print(f"嚴重錯誤：API 崩潰了，原因：{str(e)}")
        raise HTTPException(status_code=500, detail=f"伺服器內部錯誤：{str(e)}")

#  模組六：白名單維護管理
@app.get("/api/whitelist/", summary="查看白名單清單")
def list_whitelist(db: Session = Depends(get_db)):
    return db.query(database.WhitelistWebsite).all()

@app.post("/api/whitelist/", summary="最高權限：新增白名單")
def add_whitelist(data: WhitelistCreate, admin: database.User = Depends(verify_super_admin), db: Session = Depends(get_db)):
    existing = db.query(database.WhitelistWebsite).filter(database.WhitelistWebsite.url == data.url).first()
    if existing:
        raise HTTPException(status_code=400, detail="該網址已存在於白名單中。")
    new_white = database.WhitelistWebsite(url=data.url, title=data.title, reason=data.reason, added_by=admin.account)
    db.add(new_white); db.commit()
    return {"status": "success", "message": f"成功由總管理員 {admin.account} 新增白名單。"}

@app.delete("/api/whitelist/{id}", summary="最高權限：刪除白名單")
def delete_whitelist(id: int, admin: database.User = Depends(verify_super_admin), db: Session = Depends(get_db)):
    target = db.query(database.WhitelistWebsite).filter(database.WhitelistWebsite.id == id).first()
    if not target:
        raise HTTPException(status_code=404, detail="找不到該白名單項目。")
    db.delete(target); db.commit()
    return {"status": "success", "message": "已成功移除白名單項目。"}
# 模組七：YOLO 獨立分析結果接收通道 (已對接雙引擎加權公式)
@app.post("/api/ai_result/report/", summary="YOLO 引擎專用：接收影像與分數並自動統整")
def receive_ai_analysis_result(report: YOLOAnalysisReport, db: Session = Depends(get_db)):
    yolo_str = ", ".join(report.yolo_objects) if report.yolo_objects else "無檢出影像特徵"
    existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
    
    if existing_record:
        existing_record.yolo_details = yolo_str
        existing_record.yolo_score = report.risk_score
        existing_record.class_metadata = report.class_metadata
        existing_record.representative_image_base64 = report.representative_image_base64
        existing_record.representative_image_detections = report.representative_image_detections
        existing_record.ocr_results = report.ocr_results

        current_nlp_score = existing_record.nlp_score or 0
        nlp_keywords = parse_stored_list(existing_record.nlp_details)
        final_score, level = calculate_multimodal_risk_100_scale(current_nlp_score, existing_record.yolo_score, nlp_keywords, report.yolo_objects)

        existing_record.risk_score = final_score
        existing_record.risk_level = level
        db.commit()
        return {"status": "success", "message": f"成功統整！已將 YOLO 影像與分數補算至 {report.url}"}
    else:
        try:
            final_score, level = calculate_multimodal_risk_100_scale(0, report.risk_score, None, report.yolo_objects)

            new_record = database.AIAnalysisResult(
                url=report.url,
                yolo_details=yolo_str,
                yolo_score=report.risk_score,
                class_metadata=report.class_metadata,
                representative_image_base64=report.representative_image_base64,
                representative_image_detections=report.representative_image_detections,
                ocr_results=report.ocr_results,
                nlp_details="文字分析中...",
                nlp_score=0,
                risk_score=final_score,
                risk_level=level
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
                real_existing.representative_image_base64 = report.representative_image_base64
                real_existing.representative_image_detections = report.representative_image_detections
                real_existing.ocr_results = report.ocr_results
                current_nlp_score = real_existing.nlp_score or 0
                nlp_keywords = parse_stored_list(real_existing.nlp_details)
                final_score, level = calculate_multimodal_risk_100_scale(current_nlp_score, real_existing.yolo_score, nlp_keywords, report.yolo_objects)
                real_existing.risk_score = final_score
                real_existing.risk_level = level
                db.commit()
            return {"status": "success", "message": "遭遇併發衝突，已轉為更新模式寫入！"}


# 模組八：NLP 獨立分析結果接收通道
@app.post("/api/nlp/report/", summary="NLP 引擎專用：接收可疑文字與分數並自動統整")
def receive_nlp_analysis_result(report: NLPAnalysisReport, db: Session = Depends(get_db)):
    nlp_str = ", ".join(report.nlp_keywords) if report.nlp_keywords else "無檢出文字特徵"
    existing_record = db.query(database.AIAnalysisResult).filter(database.AIAnalysisResult.url == report.url).first()
    
    if existing_record:
        existing_record.nlp_details = nlp_str
        existing_record.nlp_score = report.risk_score

        current_yolo_score = existing_record.yolo_score or 0
        yolo_objects = parse_stored_list(existing_record.yolo_details)
        final_score, level = calculate_multimodal_risk_100_scale(existing_record.nlp_score, current_yolo_score, report.nlp_keywords, yolo_objects)

        existing_record.risk_score = final_score
        existing_record.risk_level = level
        db.commit()
        return {"status": "success", "message": f"成功統整！已將 NLP 文字與分數補充至 {report.url}"}
    else:
        try:
            final_score, level = calculate_multimodal_risk_100_scale(report.risk_score, 0, report.nlp_keywords, None)
        
            new_record = database.AIAnalysisResult(
                url=report.url,
                yolo_details="影像分析中...", 
                yolo_score=0, 
                nlp_details=nlp_str,
                nlp_score=report.risk_score,
                risk_score=final_score,  
                risk_level=level         
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
                current_yolo_score = real_existing.yolo_score or 0
                yolo_objects = parse_stored_list(real_existing.yolo_details)
                final_score, level = calculate_multimodal_risk_100_scale(real_existing.nlp_score, current_yolo_score, report.nlp_keywords, yolo_objects)
                real_existing.risk_score = final_score
                real_existing.risk_level = level
                db.commit()
            return {"status": "success", "message": "遭遇併發衝突，已轉為更新模式寫入！"}
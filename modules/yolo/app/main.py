import base64
import os
import sys
import threading
import time
from pathlib import Path

# 1) Windows 主控台預設可能是 cp950/cp936 這類非 UTF-8 編碼，print() 印到 emoji（例如 ⚠️❌）會直接
#    UnicodeEncodeError 炸掉——而且這個炸裂還會發生在 except 區塊自己的錯誤訊息裡，導致真正的錯誤被吃掉。
# 2) line_buffering=True 是真正關鍵：只要 stdout 被導到檔案/管線（不是互動式終端機，log 蒐集一定是這樣），
#    Python 預設會整段 buffer 起來，print() 不會馬上寫進 log，看起來就像背景任務卡住/沒反應——
#    其實程式早就跑完了，只是訊息還沒被沖出來。強制 line-buffering 確保每一行 print 立刻可見。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)

import cv2
import numpy as np
import requests
from fastapi import FastAPI, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel
from typing import Any
from ultralytics import YOLO

from ai_model.scoring import (
    compute_visual_risk,
    merge_class_metadata,
    new_class_metadata,
    record_detection,
)
from ai_model.ocr import extract_texts, load_ocr_reader

app = FastAPI(title="防毒軟體 - YOLO 影像分析部門 API")

# 1. 載入專屬權重檔。用 __file__ 定位而不是相對於「執行指令當下的目錄」，
# 這樣不管是本機從專案根目錄跑、還是 Docker 裡 WORKDIR=/app 跑，都一定能找到同一個 models/best.pt
MODEL_PATH = Path(os.getenv("MODEL_PATH", Path(__file__).parent / "models" / "best.pt"))
model = None
try:
    model = YOLO(str(MODEL_PATH))
    print(f"🎉 [成功] YOLOv8 自定義模型 {MODEL_PATH} 已順利載入！")
    print("🚨 模型內部真正的 ID 對應是：", model.names)
except Exception as e:
    print(f"🚨 [錯誤] 模型載入失敗，請確認 {MODEL_PATH} 是否存在！錯誤: {e}")

# 1b. 載入 OCR 引擎（EasyOCR，繁中+英文）。跟 YOLO 模型一樣：失敗就設成 None，不讓服務直接掛掉，
# /health 會照實回報有沒有載成功，OCR 掛了不影響 YOLO 那邊的計分邏輯繼續運作（解耦設計）。
#
# 預設用 GPU。原本是刻意用 CPU，理由寫「這張卡只有 4GB 顯存，會跟 YOLO 搶」——
# 那是開發機的狀況。部署機器是 RTX 5060 Ti / 16 GB，YOLO 跑著時只用 513 MiB。
#
# 而 CPU 模式實測 8 秒一張，爬蟲一分鐘產出約 90 張，差 12 倍：請求在 FastAPI 的
# 執行緒池裡積壓，每一個都抱著一張 base64 圖片，記憶體以 0.5 GB/分往上爬，
# 四分鐘就撞到容器上限被砍——手上沒做完的批次全部消失（2026-09-03 實際發生）。
#
# 顯存小的機器把 OCR_USE_GPU 設成 0，就退回原本的 CPU 行為。
ocr_reader = None
try:
    use_gpu = os.getenv("OCR_USE_GPU", "1") not in ("0", "false", "False", "")
    ocr_reader = load_ocr_reader(gpu=use_gpu)
    print(f"🎉 [成功] EasyOCR（繁中+英文，{'GPU' if use_gpu else 'CPU'} 模式）已順利載入！")
except Exception as e:
    print(f"🚨 [錯誤] OCR 引擎載入失敗，本次啟動將不含文字擷取功能！錯誤: {e}")

# 2. 用「類別名稱」而非數字 ID 對齊 16 個 YOLO 類別，權重與組合加成定義於 ai_model/scoring.py
# 用名稱比對可以在模型重新訓練、ID 洗牌時依然正確對齊，達成計分邏輯與模型 ID 的解耦。

# 後端同學的接收網址 —— 用環境變數 BACKEND_BASE_URL 覆寫，方便單機測試/跨機測試/未來 docker-compose 切換，
# 沒有設定環境變數時預設用目前的 Tailscale 位址，行為不變
# 沒設就爆掉。以前預設值是某台機器的 tailnet IP，
# 換環境忘了設就會把分析結果送到別人的機器上（SEC-20）。
BACKEND_BASE_URL = os.environ["BACKEND_BASE_URL"]
BACKEND_REPORT_URL = f"{BACKEND_BASE_URL}/api/ai_result/report/"

# 服務間驗證（SEC-01）。跟 BACKEND_BASE_URL 一樣沒設就爆掉：回推被擋掉時
# 這裡只是印一行錯誤、不會中斷推論，所以「靜靜掉資料」的風險比啟動失敗高得多。
INTERNAL_API_TOKEN = os.environ["INTERNAL_API_TOKEN"]

# 3. 前端展示用的信心度門檻，跟算分用的 conf=0.1 刻意分開：
# 算分要低門檻才能逼出弱訊號，但畫框給人看只想看有把握的框，避免「代表圖是靠一個 0.11 的雜訊框選出來的，
# 結果框被濾掉後畫面上什麼都沒有」這種有分數卻沒有標籤可看的情況。
DISPLAY_CONFIDENCE_THRESHOLD = 0.5


def select_visible_detections(detections, visual_result):
    """
    挑出要畫給人看的框：分數的「解釋依據」(最終採計的 top_class、觸發組合加成的搭檔類別) 各自信心度最高的那一個框，
    不管信心度多低都保留——因為它就是這張圖分數變高的原因，藏起來反而誤導。
    範例：ziplock_bag(0.9) + substance_powder(0.3) 觸發分裝組合加成，兩個都會被保留，
    不會因為 substance_powder 只有 0.3 就被濾掉、變成畫面上只看到一個夾鏈袋、看不出風險從何而來。

    其餘框（包含同一個解釋依據類別裡，非最高分的其他框）一律只看自己的信心度夠不夠格，
    避免同一類別因為偵測到好幾個區域，把一堆低信心度的重複框都洗到畫面上。
    """
    explaining_classes = set()
    if visual_result.get("top_class"):
        explaining_classes.add(visual_result["top_class"])
    if visual_result.get("triggered_combo"):
        explaining_classes.update(visual_result["triggered_combo"])

    best_per_class = {}
    for d in detections:
        name = d["class_name"]
        if name not in best_per_class or d["confidence"] > best_per_class[name]["confidence"]:
            best_per_class[name] = d

    return [
        d for d in detections
        if (d["class_name"] in explaining_classes and d is best_per_class[d["class_name"]])
        or d["confidence"] >= DISPLAY_CONFIDENCE_THRESHOLD
    ]

# 🌟 全域計分板：用來統整同一個批次網址的多張圖片結果
BATCH_MEMORY = {}
memory_lock = threading.Lock()

# OCR 一次只跑一個。
#
# 2026-09-03 這個容器連續三次被全域 OOM 砍掉（anon-rss 8.4 / 7.7 / 7.6 GB），
# 第三次把 Docker Desktop 的 WSL 整合一起帶走、六個服務全停。
#
# 原因：EasyOCR 是第二個模型，而且對「每一張圖」都跑一次 CPU 推論。
# FastAPI 的 BackgroundTasks 會把同步函式丟進 threadpool（預設 40 條），
# 爬蟲一頁送十張圖、同時好幾頁進來，等於幾十個 EasyOCR 推論並行，
# 每一個都自己配一份張量。加 OCR 之前 yolo 峰值約 5 GB，加了之後 8 GB。
#
# 序列化之後只有一個推論在跑，其餘執行緒在這裡等——會變慢（CPU 模式一張約
# 2 秒），但排隊遠比整台機器被打掛好。真的太慢再往上調，記憶體是線性增加的。
OCR_SEMAPHORE = threading.Semaphore(1)

# OCR 前先把圖縮小。
#
# 商品圖常常是 2000px 以上，但要讀的是包裝上的字，不需要那個解析度——
# EasyOCR 自己也會縮（canvas_size 預設 2560）。先縮可以省下解碼後那份大陣列
# 以及推論時的張量。長邊 1920 對讀字綽綽有餘。
OCR_MAX_SIDE = 1920

# 沒收齊的批次要過期清掉。
#
# BATCH_MEMORY[batch_id] 只有在 processed_count == total_images 時才會被刪。
# 只要有一張圖沒送到（爬蟲少送、請求失敗、yolo 中途重啟），這一筆就永遠留著，
# 而且裡面存著代表圖的 base64——不會有任何錯誤訊息，就是慢慢漏。
BATCH_TTL_SECONDS = 30 * 60

# 4. 影像解碼小工具 (記憶體直接流轉，完全不寫入硬碟，速度最快)
def decode_base64_to_cv2(b64_data, task_id: str):
    """回傳 (cv2 圖片, 清乾淨後的 base64 字串)；清乾淨後的字串可以原封不動轉發給後端存檔，不用重新編碼。"""
    if not b64_data: return None, None
    if isinstance(b64_data, list):
        if len(b64_data) > 0: b64_data = b64_data[0]
        else: return None, None

    b64_data = str(b64_data).strip('[]"\' ')
    if not b64_data or b64_data.lower() == 'none': return None, None
    if "," in b64_data: b64_data = b64_data.split(",")[1]

    missing_padding = len(b64_data) % 4
    if missing_padding: b64_data += "=" * (4 - missing_padding)

    try:
        img_data = base64.b64decode(b64_data)
        nparr = np.frombuffer(img_data, np.uint8)
        return cv2.imdecode(nparr, cv2.IMREAD_COLOR), b64_data
    except Exception as decode_err:
        print(f"[⚠️ 解碼炸裂] Base64 解析失敗 (Task: {task_id}): {decode_err}")
        return None, None

def downscale_for_ocr(img):
    """長邊超過 OCR_MAX_SIDE 就等比例縮小。座標是正規化的（xyxyn），
    縮圖不影響回報出去的框位置。"""
    if img is None:
        return img
    height, width = img.shape[:2]
    longest = max(height, width)
    if longest <= OCR_MAX_SIDE:
        return img
    scale = OCR_MAX_SIDE / longest
    return cv2.resize(img, (int(width * scale), int(height * scale)),
                      interpolation=cv2.INTER_AREA)


def evict_stale_batches():
    """把太久沒動靜的批次丟掉。呼叫端必須已經持有 memory_lock。"""
    now = time.time()
    stale = [bid for bid, data in BATCH_MEMORY.items()
             if now - data.get("last_touch", now) > BATCH_TTL_SECONDS]
    for bid in stale:
        print(f"🧹 [過期清理] 批次 {bid} 超過 {BATCH_TTL_SECONDS // 60} 分鐘沒有新圖，"
              f"已處理 {BATCH_MEMORY[bid]['processed_count']} 張，判定收不齊，釋放。")
        del BATCH_MEMORY[bid]


# 5. 核心非同步工廠：偷偷在背景算 YOLO，全部圖片到齊後才打電話給後端
def background_yolo_and_report(url: str, image_base64: Any, task_id: str, total_images: int):
    print(f"\n[🔥 影像分析啟動] 正在處理任務: {task_id}")

    batch_id = task_id.split("_")[0] if "_" in task_id else task_id

    # task_id 的格式是 {批次uuid}_{第幾張圖}（backend/app/utils.py:120），
    # 後綴就是圖片序號。整批的 OCR 文字是混在一起送出去的，不標序號的話
    # 前端只看得到一堆文字、不知道是從哪張圖抓的。抓不到就留 None，
    # 前端那一行會自動不顯示。
    image_index = None
    if "_" in task_id:
        suffix = task_id.rsplit("_", 1)[1]
        if suffix.isdigit():
            image_index = int(suffix)

    try:
        # 解碼圖片
        img, cleaned_b64 = decode_base64_to_cv2(image_base64, task_id)

        current_class_metadata = new_class_metadata()
        current_detections = []  # 給前端畫框用的原始偵測清單，跟 class_metadata 分開、不受權重表過濾
        current_ocr_texts = []   # 這張圖 OCR 抓到的文字，跟 YOLO 偵測獨立、不影響視覺分數
        current_score = 0
        is_valid = False
        visual_result = {"top_class": None, "triggered_combo": None}

        if img is not None:
            # 執行推論，信心度門檻開 0.1 逼低分訊號現形
            results = model(img, conf=0.1)

            for r in results:
                for box in r.boxes:
                    conf = float(box.conf)
                    cls_id = int(box.cls)  # 模型吐出的原始數字 ID，僅用來查回類別名稱

                    actual_name = r.names.get(cls_id, "未知")
                    print(f"🔍 YOLO 原始偵測 -> ID: {cls_id}, 信心度: {conf:.2f}, 原始標籤: {actual_name}")

                    # 🌟 用「類別名稱」比對 16 類權重表，而非數字 ID，避免模型重訓後 ID 洗牌導致誤判
                    record_detection(current_class_metadata, actual_name, conf)

                    # 正規化座標 (0~1)，不管前端把圖片縮放到多大都能直接換算畫框位置
                    x1, y1, x2, y2 = box.xyxyn[0].tolist()
                    current_detections.append({
                        "class_name": actual_name,
                        "confidence": round(conf, 4),
                        "box_format": "xyxyn",
                        "box": [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)],
                    })

            # 存在即採計：Score_i = Weight_i * Max_Confidence_i，取最高者並套用組合加成、封頂 100
            visual_result = compute_visual_risk(current_class_metadata)
            current_score = visual_result["visual_score"]
            is_valid = bool(current_class_metadata)

            # OCR 對每一張圖都跑，不像代表圖只挑分數最高的那張——文字可能出現在任何一張圖上
            # （例如成分標示可能跟主要違禁品照片不是同一張），漏跑非代表圖會漏掉這些文字
            ocr_img = downscale_for_ocr(img)
            with OCR_SEMAPHORE:
                current_ocr_texts = extract_texts(ocr_reader, ocr_img)
            del ocr_img
            for item in current_ocr_texts:
                item["image_index"] = image_index

        # 這張圖要「有資格」被選為代表圖，本身至少要有一個框信心度夠高（避免整張圖全靠雜訊撐分數）；
        # 一旦有資格，實際畫出來的框則是「信心度夠高」加上「分數的解釋依據」（見 select_visible_detections）
        has_strong_detection = any(d["confidence"] >= DISPLAY_CONFIDENCE_THRESHOLD for d in current_detections)
        visible_detections = select_visible_detections(current_detections, visual_result) if has_strong_detection else []

        # 控制是否發送最終報告的變數
        should_report = False
        final_risk_score = 0
        detected_objects = []
        is_valid_drug_payload = False
        batch_class_metadata = {}
        representative_image_base64 = None
        representative_image_detections = []
        batch_ocr_texts = []

        # -----------------------------------------------------------------
        # 🌟 核心記憶體統整算式：使用 Lock 確保多執行緒累加安全
        # -----------------------------------------------------------------
        with memory_lock:
            if batch_id not in BATCH_MEMORY:
                BATCH_MEMORY[batch_id] = {
                    "class_metadata": new_class_metadata(),
                    "valid_image_count": 0,           # 純紀錄用途，不影響分數計算
                    "processed_count": 0,
                    "best_display_score": 0,          # 只從「有畫得出框」的圖片裡比分數，跟批次總分是兩件事
                    "best_display_image_base64": None, # 只留分數最高那張代表圖，給前端畫框用，不是每張圖都存
                    "best_display_image_detections": [],
                    "ocr_texts": [],                  # 整批每張圖 OCR 抓到的文字都累加在這裡，不像代表圖只留一張
                    "last_touch": time.time(),        # 給 evict_stale_batches 判斷這批是不是已經收不齊了
                }
                evict_stale_batches()

            # 圖片處理計數器 +1
            BATCH_MEMORY[batch_id]["processed_count"] += 1
            BATCH_MEMORY[batch_id]["last_touch"] = time.time()

            # 🌟 解耦設計：count / max_confidence 獨立於單張分數之外，逐圖合併成整批的 metadata
            if is_valid:
                BATCH_MEMORY[batch_id]["valid_image_count"] += 1
                merge_class_metadata(BATCH_MEMORY[batch_id]["class_metadata"], current_class_metadata)

            # OCR 文字不分是不是代表圖，整批每張圖抓到的都累加進來
            if current_ocr_texts:
                BATCH_MEMORY[batch_id]["ocr_texts"].extend(current_ocr_texts)

            # 代表圖只從「至少有一個框信心度夠高、畫得出來」的圖片裡挑分數最高的，
            # 避免選到一張分數是靠低信心度雜訊框撐起來、濾掉框之後畫面空空如也的圖
            if visible_detections and current_score > BATCH_MEMORY[batch_id]["best_display_score"]:
                BATCH_MEMORY[batch_id]["best_display_score"] = current_score
                BATCH_MEMORY[batch_id]["best_display_image_base64"] = cleaned_b64
                BATCH_MEMORY[batch_id]["best_display_image_detections"] = visible_detections

            current_progress = BATCH_MEMORY[batch_id]["processed_count"]
            print(f"📊 批次進度追蹤 [{batch_id}]: {current_progress} / {total_images} (當前任務: {task_id})")

            # 🌟 檢查：是否所有圖片都到齊了？
            if current_progress >= total_images:
                should_report = True

                valid_image_count = BATCH_MEMORY[batch_id]["valid_image_count"]
                batch_class_metadata = BATCH_MEMORY[batch_id]["class_metadata"]
                representative_image_base64 = BATCH_MEMORY[batch_id]["best_display_image_base64"]
                representative_image_detections = BATCH_MEMORY[batch_id]["best_display_image_detections"]
                batch_ocr_texts = BATCH_MEMORY[batch_id]["ocr_texts"]

                # 🌟 批次分數：把整批圖片合併成「一份」類別證據（每個類別取全批最高信心度），
                # 直接套用跟單張圖一樣的存在即採計＋組合加成公式，不再對每張圖的分數取平均。
                # 平均會讓真正的強證據（例如 15 張圖裡有幾張很清楚的大麻符號）被其餘普通照片稀釋掉，
                # 導致整批評分被拉低、跟實際風險不成比例；改成這樣之後只要批次裡出現過一次高信心度證據，
                # 不管其他照片多平凡，批次分數都會反映那個最高信心度。
                batch_visual_result = compute_visual_risk(batch_class_metadata)
                final_risk_score = batch_visual_result["visual_score"]

                if batch_class_metadata:
                    detected_objects = list(batch_class_metadata.keys())
                    is_valid_drug_payload = True
                else:
                    detected_objects = ["未偵測到管制毒品"]
                    is_valid_drug_payload = False

        # -----------------------------------------------------------------
        # 6. 打包最終成果回傳給中央後端 (Leo) -> 只有到齊了才發送
        # -----------------------------------------------------------------
        if should_report:
            payload = {
                "url": url,
                "risk_score": final_risk_score,
                "yolo_objects": detected_objects, # 命中的 16 類別英文標籤清單
                "class_metadata": batch_class_metadata, # 每個類別獨立的 count / max_confidence，供後端與 NLP 模組過濾使用
                "processed_images": [],
                "is_valid_drug": is_valid_drug_payload,
                "representative_image_base64": representative_image_base64, # 這批圖片裡分數最高的代表圖，供前端畫框展示
                "representative_image_detections": representative_image_detections, # 代表圖對應的偵測框（0~1 正規化座標）
                "ocr_results": {
                    "engine": "easyocr",
                    "detected_texts": batch_ocr_texts,  # 整批每張圖抓到的文字彙整，不是只有代表圖那一張
                },
            }

            print(f"\n[🚀 批次全數到齊！] 正在發送最終結算報告給後端。批次: {batch_id}")
            print(f"   -> 有效圖筆數: {valid_image_count} / {total_images}, 總類別: {detected_objects}")
            print(f"   -> 批次整體分數 (全批合併證據計算): {final_risk_score} 分 (最高風險類別: {batch_visual_result['top_class']}, 組合加成: {batch_visual_result['combo_multiplier']}x)")

            response = requests.post(
                BACKEND_REPORT_URL,
                json=payload,
                headers={"X-Internal-Token": INTERNAL_API_TOKEN},
                timeout=5,
            )
            print(f"[✨ 後端回應] 狀態碼: {response.status_code}, 內容: {response.text}")
            
            # 釋放記憶體
            with memory_lock:
                if batch_id in BATCH_MEMORY:
                    del BATCH_MEMORY[batch_id]
                    print(f"🧹 [記憶體清理] 已成功釋放批次 {batch_id} 的緩存空間。")
        else:
            print(f"⏳ 任務 {task_id} 處理完畢，目前累計 {current_progress} 張圖，等待其餘圖片到齊中...")
            
    except Exception as e:
        print(f"❌ 背景處理或回報失敗: {str(e)}")

# 7. 健康檢查：讓 docker-compose 之類的編排工具知道這個模組是不是真的活了（模型有沒有載完）
@app.get("/health")
async def health(response: Response):
    """模型沒載入就回 503。

    ⚠️ 一定要 async。同步的 def 會被 FastAPI 丟進執行緒池，而那個池子
    （預設 40 條）正是 background_yolo_and_report 在用的——推論忙的時候
    /health 會排在幾十個推論後面，8 秒都回不來，健康檢查因此把一個
    「只是忙」的服務判成「壞掉」。async 直接在事件迴圈上跑，這裡只是讀兩個
    變數，不會阻塞任何東西。

    以前不管載入成功與否都回 200，結果是 2026-09-04 這種情況：容器起來了、
    /health 回 200、docker ps 看起來一切正常，但 model 是 None，每一張圖進來
    都是 'NoneType' object is not callable，而接收端點照樣回「YOLO 已經接單」。

    外面完全看不出來——爬蟲以為送出去了、後端在等一個永遠不會來的回報，
    那些網址就默默停在「影像分析中...」。這次是因為 bind mount 掛成空目錄
    （WSL 整合斷線期間建立的容器），/models/ 裡什麼都沒有。

    模型是這個服務存在的理由，載不起來就不該被算成健康。OCR 則是加值功能，
    它掛掉不影響 YOLO 計分，所以只照實回報、不影響健康狀態。
    """
    ok = model is not None
    if not ok:
        response.status_code = 503
    return {
        "status": "ok" if ok else "degraded",
        "model_loaded": ok,
        "ocr_loaded": ocr_reader is not None,
    }


# 8. 接收口
@app.post("/api/v1/predict/trigger")
async def receive_from_backend(data: dict, background_tasks: BackgroundTasks):
    task_id = data.get("task_id", "unknown_task")
    url = data.get("url", "")
    image_base64 = data.get("image_base64", None)
    priority = data.get("priority", 0)
    
    # 🌟 讀取後端加在最後一條位子的 total_images
    total_images = data.get("total_images", 11)
    
    background_tasks.add_task(background_yolo_and_report, url, image_base64, task_id, total_images)
    return {"status": "success", "message": f"YOLO 已經接單 (Task: {task_id}, 預期總圖數: {total_images})"}
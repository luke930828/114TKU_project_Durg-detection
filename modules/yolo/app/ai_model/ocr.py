"""
OCR 文字擷取模組 (EasyOCR)

跟 scoring.py 一樣走「解耦設計」：只負責「圖片 -> 偵測到的文字清單」這個純運算，
不碰風險分數邏輯——OCR 抓到的文字要怎麼影響最終分數，是後端 main.py 那層 NLP 覆寫規則
（規則 A/B）的事，不是這裡的事。這支模組回傳的永遠只是「觀測結果」。
"""

from typing import Dict, List, Optional

import torch
import easyocr

OCR_LANGUAGES = ["ch_tra", "en"]  # 繁體中文 + 英文，包裝／標籤常見的兩種文字


def load_ocr_reader(gpu: bool = True) -> "easyocr.Reader":
    """建立 EasyOCR reader。

    ⚠️ 關於 gpu 這個參數
    ────────────────────
    這裡原本的註解寫「刻意用 CPU，不跟 YOLO 搶 GPU：這張卡只有 4GB 顯存」。
    那是在開發機上測到的結論，部署機器不是那樣：

        NVIDIA GeForce RTX 5060 Ti   16,311 MiB   （實測 YOLO 跑著時只用 513 MiB）

    而 CPU 模式在部署機器上是 8 秒一張，爬蟲一分鐘產出約 90 張，差 12 倍，
    請求會在執行緒池裡積壓到容器被 OOM 砍掉（2026-09-03 實際發生）。

    所以預設改成 GPU，由 OCR_USE_GPU 環境變數控制——顯存小的機器設 0 就會退回
    CPU，行為跟以前一樣。
    """
    if not gpu:
        # 🌟 CPU 模式下實測到的關鍵問題：torch 預設會用多執行緒（OpenMP）平行運算，
        # 單獨跑script時很快（~2秒），但放進 uvicorn 這種本身就有多執行緒/事件迴圈的伺服器背景任務裡，
        # torch 的執行緒池會跟伺服器自己的執行緒互搶 CPU 核心，效能嚴重惡化到 10~30 秒以上，
        # 從外面看起來像卡住。強制限制成單執行緒反而更快更穩定，這是這類問題的標準解法。
        torch.set_num_threads(1)
    return easyocr.Reader(OCR_LANGUAGES, gpu=gpu, verbose=False)


def extract_texts(reader: Optional["easyocr.Reader"], image) -> List[Dict]:
    """
    對單張圖片跑 OCR，回傳偵測到的文字清單。

    座標統一轉成跟 YOLO 一致的 xyxyn（0~1 正規化）格式：EasyOCR 原生給的是像素座標的
    四角多邊形（可能是歪斜的），這裡取外接矩形（axis-aligned bounding box）簡化成
    跟 YOLO 一樣的矩形框，前端可以用同一套畫框邏輯處理，不用為了 OCR 另外寫一套。
    """
    if reader is None or image is None:
        return []

    height, width = image.shape[:2]
    if height == 0 or width == 0:
        return []

    try:
        results = reader.readtext(image)
    except Exception as e:
        print(f"[⚠️ OCR 失敗] {e}")
        return []

    detected_texts = []
    for bbox, text, confidence in results:
        xs = [point[0] for point in bbox]
        ys = [point[1] for point in bbox]
        x1, x2 = min(xs) / width, max(xs) / width
        y1, y2 = min(ys) / height, max(ys) / height
        detected_texts.append({
            "text": text,
            "confidence": round(float(confidence), 4),
            "box_format": "xyxyn",
            "box": [round(x1, 4), round(y1, 4), round(x2, 4), round(y2, 4)],
        })
    return detected_texts

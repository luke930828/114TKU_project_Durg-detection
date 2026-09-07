"""
假的 NLP / YOLO / 爬蟲服務，依 ROLE 環境變數決定扮演誰。

為什麼要 stub：真的 NLP 要從 HuggingFace 下載約 1GB 模型，真的 YOLO 要 NVIDIA GPU
與 best.pt 權重，而且兩者的推論分數都不是固定值，沒辦法斷言。
stub 回傳固定分數，讓「後端有沒有正確合併兩個引擎的分數」變成可驗證的事。

行為刻意做得跟真的服務一樣（包含 NLP 會自己回推一次，那正是 BUG-03 的重複寫入），
這樣測試抓到的問題才是後端真的有的問題。

額外提供 /__stub/calls，讓測試可以檢查後端究竟派發了什麼出來——
例如 SSRF 測試要確認惡意網址有沒有真的被送到爬蟲。
"""
import os
import threading

import requests
from fastapi import FastAPI, Request

ROLE = os.environ["ROLE"]
BACKEND = os.getenv("BACKEND_BASE_URL", "http://backend:8000").rstrip("/")

# 三個 report 端點現在要驗 token（SEC-01）。stub 扮演的是真的 nlp/yolo/爬蟲，
# 所以它也得照規矩帶——不帶的話回推會被 401 擋掉，整條管線的測試會全部失敗，
# 而且失敗看起來像「後端沒有合併分數」，很難查。
INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")

NLP_SCORE = float(os.getenv("STUB_NLP_SCORE", "0.6"))    # → 後端算成 60
YOLO_SCORE = int(os.getenv("STUB_YOLO_SCORE", "80"))
PUSH_BACK = os.getenv("STUB_PUSH_BACK", "1") == "1"

app = FastAPI(title=f"stub-{ROLE}")

_calls = []          # 收到的請求，給測試檢查用
_batches = {}        # YOLO 用：batch_id -> 已收到幾張
_lock = threading.Lock()


def _record(path, payload):
    with _lock:
        _calls.append({"path": path, "payload": payload})


def _push(path, payload):
    """回推給後端。失敗只記錄，不要讓 stub 自己爆掉。"""
    if not PUSH_BACK:
        return
    try:
        requests.post(
            f"{BACKEND}{path}",
            json=payload,
            headers={"X-Internal-Token": INTERNAL_TOKEN},
            timeout=10,
        )
    except Exception as e:                                    # noqa: BLE001
        print(f"[stub-{ROLE}] 回推 {path} 失敗：{e}", flush=True)


@app.get("/health")
def health():
    return {"status": "ok", "role": ROLE, "model_loaded": True, "device": "stub"}


@app.get("/")
def root():
    return {"service": f"stub-{ROLE}"}


@app.get("/__stub/calls")
def stub_calls():
    with _lock:
        return {"count": len(_calls), "calls": list(_calls)}


@app.post("/__stub/reset")
def stub_reset():
    with _lock:
        _calls.clear()
        _batches.clear()
    return {"status": "reset"}


# ---------------- NLP ----------------
if ROLE == "nlp":

    @app.post("/predict")
    async def predict(req: Request):
        body = await req.json()
        _record("/predict", body)
        url = body.get("url", "")
        keywords = ["測試詞A", "測試詞B"]
        # 真的 NLP 服務在回應之後會自己推一份給後端，這裡照做。
        # 後端的 utils.py 也會推一份——兩邊各推一次就是 BUG-03。
        _push("/api/nlp/report/", {
            "url": url,
            "risk_score": int(NLP_SCORE * 100),
            "nlp_keywords": keywords,
        })
        return {"label": "DRUG", "score": NLP_SCORE, "keywords": keywords}


# ---------------- YOLO ----------------
if ROLE == "yolo":

    @app.post("/api/v1/predict/trigger")
    async def trigger(req: Request):
        body = await req.json()
        _record("/api/v1/predict/trigger", body)
        task_id = str(body.get("task_id", ""))
        batch = task_id.rsplit("_", 1)[0] if "_" in task_id else task_id
        total = int(body.get("total_images") or 1)

        with _lock:
            _batches[batch] = _batches.get(batch, 0) + 1
            done = _batches[batch]

        # 跟真的 YOLO 一樣：整批處理完才回推一次
        if done >= total:
            _push("/api/ai_result/report/", {
                "url": body.get("url", ""),
                "risk_score": YOLO_SCORE,
                "yolo_objects": ["stub_object"],
                "class_metadata": {"stub_object": done},
                "representative_image_base64": "",
                "representative_image_detections": [
                    {"class": "stub_object", "confidence": 0.91}
                ],
            })
        return {"status": "accepted", "task_id": task_id, "processed": done}


# ---------------- 爬蟲 ----------------
if ROLE == "crawler":

    def _crawl(body):
        _record("/api/v1/crawl", body)
        url = body.get("url", "")
        _push("/api/crawler/report/", {
            "task_type": "manual",
            "timestamp": "2026-01-01T00:00:00",
            "keywords": ["測試關鍵字"],
            "url": url,
            "text_content": "整合測試用的假內容，不含任何真實資料。",
            "product_images_b64": ["ZmFrZS1pbWFnZQ=="],
        })
        return {"status": "accepted", "url": url}

    @app.post("/api/v1/crawl")
    async def crawl_v1(req: Request):
        return _crawl(await req.json())

    @app.post("/api/crawl")
    async def crawl_alias(req: Request):
        return _crawl(await req.json())

    @app.post("/api/v1/monitor/start")
    def monitor_start():
        return {"status": "started"}

    @app.post("/api/v1/monitor/stop")
    def monitor_stop():
        return {"status": "stopped"}

    @app.get("/api/v1/monitor/status")
    def monitor_status():
        return {"running": False, "stub": True}

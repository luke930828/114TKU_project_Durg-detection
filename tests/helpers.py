"""測試共用的小工具。"""
import time

CRAWLER_PAYLOAD = {
    "task_type": "automated_24h",
    "timestamp": "2026-01-01T00:00:00",
    "keywords": ["測試關鍵字"],
    "text_content": "整合測試用的假內容，不含任何真實資料。",
    "product_images_b64": ["ZmFrZS1pbWFnZQ=="],
}


def crawler_report(api, url, **over):
    body = dict(CRAWLER_PAYLOAD, url=url)
    body.update(over)
    return api.post("/api/crawler/report/", auth=False, json=body)


def find_result(admin, url):
    """從管理員報表裡找出某個網址的那筆 AI 分析結果。"""
    r = admin.get("/api/crawler/report/")
    if r.status_code != 200:
        return None
    for row in r.json().get("data", []):
        if row.get("url") == url:
            return row
    return None


def wait_for(fn, timeout=60, interval=1.5, what="條件"):
    """輪詢直到 fn() 回傳真值。背景派發是非同步的，一定要等。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"等待逾時（{timeout}s）：{what}　最後看到：{last}")


def wait_both_engines(admin, url, timeout=60):
    """等到 NLP 與 YOLO 都回報完畢（兩邊都不再是「分析中」）。"""
    def ready():
        row = find_result(admin, url)
        if not row:
            return None
        if row.get("yolo_details") == "影像分析中...":
            return None
        if row.get("nlp_details") == "文字分析中...":
            return None
        return row

    return wait_for(ready, timeout=timeout, what=f"{url} 的雙引擎分析結果")

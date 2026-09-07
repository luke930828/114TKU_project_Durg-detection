import time
from fastapi import FastAPI, HTTPException, BackgroundTasks
from pydantic import BaseModel
import logging
import asyncio
import os
from typing import Optional, Dict, Any
from contextlib import asynccontextmanager
from playwright.async_api import async_playwright, Browser, Playwright
from manual import ManualInvestigator
from engine_v2 import DualTrackEngine
from webhook_helper import get_webhook_helper, WebhookHelper
from record_paths import ensure_record_layout

# 設定 Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')


def _manual_mode_config(config: Dict[str, Any]) -> Dict[str, Any]:
    return config.get("manual_mode") or {}


async def _start_manual_browser(config: Dict[str, Any]) -> tuple[Optional[Playwright], Optional[Browser]]:
    mm = _manual_mode_config(config)
    if not mm.get("reuse_browser", True):
        logging.info("[STARTUP] manual_mode.reuse_browser=false，手動爬蟲將每次自行啟動瀏覽器。")
        return None, None

    headless = config.get("headless", True)
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            # Chromium 在容器裡用 /dev/shm 做渲染程序之間的共享記憶體，
            # 而 Docker 預設只給 64 MB。抓到圖多的頁面時會被吃滿，
            # 渲染程序帶著 SIGTRAP 崩掉，然後 WSL 把整個位址空間寫成傾印檔
            # （2026-08-29 / 08-31 / 09-02 三次，最大 244 GB，把 C 槽塞爆）。
            # 這個旗標讓它改用 /tmp。compose 也把 shm_size 調到 1 GB，
            # 兩層都做——只靠旗標的話，漏掉任何一個啟動點就會再犯。
            "--disable-dev-shm-usage",
        ],
    )
    logging.info("[STARTUP] 手動爬蟲 Browser 常駐池已就緒（reuse_browser=true）。")
    return pw, browser


async def _stop_manual_browser(pw: Optional[Playwright], browser: Optional[Browser]) -> None:
    if browser:
        await browser.close()
    if pw:
        await pw.stop()
    logging.info("[SHUTDOWN] 手動爬蟲 Browser 常駐池已關閉。")


# 全域狀態管理
class GlobalState:
    def __init__(self):
        self.monitor_engine = DualTrackEngine()
        self.monitor_task: Optional[asyncio.Task] = None
        self.webhook: Optional[WebhookHelper] = None
        self.processing_urls: set = set()
        self.finished_urls: Dict[str, float] = {}
        self.manual_playwright: Optional[Playwright] = None
        self.manual_browser: Optional[Browser] = None

state = GlobalState()

@asynccontextmanager
async def lifespan(app: FastAPI):
    ensure_record_layout(state.monitor_engine.config)
    state.webhook = get_webhook_helper(state.monitor_engine.config)
    state.manual_playwright, state.manual_browser = await _start_manual_browser(
        state.monitor_engine.config
    )
    logging.info("API 伺服器啟動，系統初始化完成。")
    # --- 讓 24h 自動爬蟲在 API 啟動時一併自動啟動 (取消註解可開啟) --- 24h 開啟位置!!!!
    state.monitor_task = asyncio.create_task(state.monitor_engine.run())
    logging.info("[STARTUP] 24H 自動引擎已經在背景一併啟動運作中！")
    yield
    if state.monitor_task and not state.monitor_task.done():
        state.monitor_task.cancel()
        try:
            await state.monitor_task
        except asyncio.CancelledError:
            pass
        logging.info("[SHUTDOWN] 24H 背景監控引擎已關閉。")
    await _stop_manual_browser(state.manual_playwright, state.manual_browser)
    state.manual_playwright = None
    state.manual_browser = None

app = FastAPI(title="OSINT 威脅情報採集系統 - 工業級 API 介面", lifespan=lifespan)

class CrawlRequest(BaseModel):
    url: str
    save_local: bool = False
    force: bool = False

async def background_crawl_task(url: str, save_local: bool):
    """背景執行爬蟲；一個 URL = 一筆 JSON = 一次 POST（由 ManualInvestigator 內部發送）。"""
    if url in state.processing_urls:
        logging.info(f"[BACKGROUND TASK] URL 正在處理中，跳過重複請求: {url}")
        return

    now = time.time()
    if url in state.finished_urls:
        if now - state.finished_urls[url] < 120:
            logging.info(f"[BACKGROUND TASK] URL 近期已處理完成，跳過任務: {url}")
            return

    state.processing_urls.add(url)
    try:
        mm = _manual_mode_config(state.monitor_engine.config)
        mode_label = "FAST" if mm.get("fast_mode", False) else "DEEP"
        logging.info(
            f"[BACKGROUND TASK] 開始處理 URL: {url} (存檔: {save_local}, 模式: {mode_label})"
        )
        shared_browser = state.manual_browser if mm.get("reuse_browser", True) else None
        investigator = ManualInvestigator(
            browser=shared_browser,
            config=state.monitor_engine.config,
        )
        result = await investigator.process_query(url, save_local=save_local)

        if result.get("crawler_data"):
            err = result.get("crawler_data", {}).get("error_message", "Unknown skip reason")
            logging.warning(f"[BACKGROUND TASK] 爬取略過或失敗: {url} - {err}")
        elif result.get("webhook_sent"):
            logging.info(
                f"[BACKGROUND TASK] 完成，Webhook 已成功送出: {url} "
                f"(商品圖 {len(result.get('product_images_b64') or [])} 張)"
            )
            state.finished_urls[url] = time.time()
        elif result.get("url"):
            logging.warning(
                f"[BACKGROUND TASK] 爬取結束但未送出 Webhook: {url} "
                f"(tier={result.get('threat_tier') or result.get('tier')}, "
                f"images={len(result.get('product_images_b64') or [])}) "
                f"— 常見原因：keywords/text 為空或後端連線失敗"
            )
            state.finished_urls[url] = time.time()
        else:
            logging.warning(f"[BACKGROUND TASK] 未預期的回傳格式: {url}")
    finally:
        if url in state.processing_urls:
            state.processing_urls.remove(url)

@app.post("/api/crawl")
async def start_crawl(request: CrawlRequest, background_tasks: BackgroundTasks):
    return await start_crawl_v1(request, background_tasks)

@app.post("/api/v1/crawl")
async def start_crawl_v1(request: CrawlRequest, background_tasks: BackgroundTasks):
    logging.info(f"API [V1] 手動查詢請求: {request.url} (存檔: {request.save_local})")

    now = time.time()
    if request.url in state.processing_urls:
        return {"status": "success", "msg": "任務處理中，請勿重複提交"}

    if not request.force and request.url in state.finished_urls:
        if now - state.finished_urls[request.url] < 120:
            logging.info(f"[API] URL 近期已完成，直接忽略重複請求: {request.url}")
            return {"status": "success", "msg": "近期已處理完成，將不再重複採集"}

    if request.force and request.url in state.finished_urls:
        state.finished_urls.pop(request.url)

    background_tasks.add_task(background_crawl_task, request.url, request.save_local)

    mm = _manual_mode_config(state.monitor_engine.config)
    return {
        "status": "success",
        "msg": "已收到爬取任務，背景執行中。完成後將主動推播至 Webhook 端點。",
        "task_info": {
            "url": request.url,
            "save_local": request.save_local,
            "manual_mode": "fast" if mm.get("fast_mode", False) else "deep",
        }
    }

@app.post("/api/v1/monitor/start")
async def start_monitor():
    if state.monitor_task and not state.monitor_task.done():
        return {"status": "error", "message": "監控引擎已在運行中"}

    state.monitor_task = asyncio.create_task(state.monitor_engine.run())
    logging.info("[API] 24H 監控引擎已由遠端啟動")
    return {"status": "success", "message": "24H 背景監控已啟動"}

@app.post("/api/v1/monitor/stop")
async def stop_monitor():
    if not state.monitor_task or state.monitor_task.done():
        return {"status": "error", "message": "監控引擎目前未在運行"}

    state.monitor_task.cancel()
    try:
        await state.monitor_task
    except asyncio.CancelledError:
        pass

    logging.info("[API] 24H 監控引擎已由遠端停止")
    return {"status": "success", "message": "監控引擎已停止"}

@app.get("/api/v1/monitor/status")
async def monitor_status():
    is_running = state.monitor_task is not None and not state.monitor_task.done()
    mm = _manual_mode_config(state.monitor_engine.config)
    stats = await state.monitor_engine.get_queue_stats()
    return {
        "status": "running" if is_running else "stopped",
        "queue_size": stats["queue_size"],
        "visited_count": stats["visited_count"],
        "found_count": stats["found_count"],
        "db_connected": stats["db_connected"],
        "manual_browser_ready": state.manual_browser is not None,
        "manual_mode": "fast" if mm.get("fast_mode", False) else "deep",
        "engine_24h_profile": "full",
    }

@app.get("/")
def read_root():
    return {
        "msg": "OSINT 爬蟲服務 - 雙向非同步串接接口 (V46.0)",
        "features": ["Manual Search (Non-persistent)", "24H Auto Monitor", "Webhook Auto-sync"]
    }

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)

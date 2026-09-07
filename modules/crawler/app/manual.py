import asyncio
import json
import os
import random
import base64
import logging
from datetime import datetime, timezone
from urllib.parse import urlparse
from typing import Any, Dict, List, Optional, Tuple

import aiosqlite
from playwright.async_api import async_playwright, Browser, Page
from playwright_stealth import stealth_async

from crawler import HeuristicAnalyzer
from password import AuthBypassEngine
from crawl_logger import CrawlLogger
from record_paths import get_record_paths
from crawl_core import (
    build_context_options,
    dismiss_overlays,
    simulate_human,
    setup_resource_routes,
    extract_product_images,
    safe_page_text,
)
from webhook_helper import (
    get_webhook_helper,
    build_webhook_payload_from_result,
    finalize_webhook_payload,
    build_negative_access_report,
    validate_webhook_payload,
    is_negative_access_payload,
    NEGATIVE_ACCESS_TEXT,
)

DEFAULT_MANUAL_MODE = {
    "fast_mode": True,
    "reuse_browser": True,
    "max_product_images": 10,
    "fast": {
        "goto_timeout_ms": 10000,
        "post_goto_sleep_ms": 500,
        "skip_auth_bypass": True,
        "skip_human_simulation": True,
        "light_popup_dismiss": True,
        "early_exit_on_login_wall": False,
        "image_download_timeout_ms": 3000,
        "block_fonts": True,
        "parallel_image_downloads": 3,
    },
    "deep": {
        "goto_timeout_ms": 25000,
        "post_goto_sleep_ms": 2000,
        "skip_auth_bypass": False,
        "skip_human_simulation": False,
        "light_popup_dismiss": False,
        "early_exit_on_login_wall": False,
        "image_download_timeout_ms": 8000,
        "block_fonts": False,
        "parallel_image_downloads": 1,
    },
}


class ManualInvestigator:
    def __init__(self, browser: Optional[Browser] = None, config: Optional[Dict[str, Any]] = None):
        if config is not None:
            self.config = config
        else:
            try:
                with open("config.json", "r", encoding="utf-8") as f:
                    self.config = json.load(f)
            except OSError:
                self.config = {}

        self._shared_browser = browser
        self._manual_mode = self._resolve_manual_mode()
        self.analyzer = HeuristicAnalyzer(self.config)
        self.auth_engine = AuthBypassEngine(self.config)

        output_dirs = self.config.get("output_dirs", {})
        self.jpg_dir = output_dirs.get("test_jpg", "testjpg")
        self.html_dir = output_dirs.get("test_html", "testHTML")
        os.makedirs(self.jpg_dir, exist_ok=True)
        os.makedirs(self.html_dir, exist_ok=True)
        self.capture_full_page_screenshot = bool(
            self.config.get("capture_full_page_screenshot", False)
        )
        self.webhook = get_webhook_helper(self.config)
        rp = get_record_paths(self.config)
        from dedup_store import DedupStore
        self.dedup = DedupStore(
            rp["db_queue"],
            url_json_path=rp["json_dedup_urls"],
            hash_json_path=rp["json_dedup_images"],
        )

    def _resolve_manual_mode(self) -> Dict[str, Any]:
        """合併 manual_mode 設定；fast_mode 決定使用 fast 或 deep 子區塊。"""
        raw = {**DEFAULT_MANUAL_MODE, **(self.config.get("manual_mode") or {})}
        profile_key = "fast" if raw.get("fast_mode", True) else "deep"
        profile = {**DEFAULT_MANUAL_MODE[profile_key], **(raw.get(profile_key) or {})}
        return {
            "fast_mode": bool(raw.get("fast_mode", True)),
            "reuse_browser": bool(raw.get("reuse_browser", True)),
            "max_product_images": int(raw.get("max_product_images", 10)),
            **profile,
        }

    @property
    def _opts(self) -> Dict[str, Any]:
        return self._manual_mode

    async def _send_webhook_if_complete(
        self, report: dict, crawl_log: Optional[CrawlLogger] = None
    ) -> bool:
        """
        資料齊全 → 送完整封包。
        沒抓到／缺欄位 → 改送「非毒品網站或無法登入」精簡封包（不再略過不送）。
        """
        url = str(report.get("url") or "")
        payload = finalize_webhook_payload(
            build_webhook_payload_from_result(report, task_type="manual")
        )

        if not is_negative_access_payload(payload):
            ok, errors = validate_webhook_payload(payload)
            if not ok:
                detail = "; ".join(errors)
                logging.info(
                    f"[MANUAL][WEBHOOK] 資料未齊，改送非毒品封包: {detail} | url={url}"
                )
                if crawl_log:
                    crawl_log.phase(
                        "WEBHOOK",
                        f"資料未齊，改送非毒品封包: {detail}",
                        level="warning",
                    )
                payload = finalize_webhook_payload(
                    build_negative_access_report(url, task_type="manual")
                )

        sent = await self.webhook.send_result(payload)
        if crawl_log:
            if sent:
                crawl_log.phase("WEBHOOK", "封包已送出", sent=True)
            else:
                crawl_log.phase(
                    "WEBHOOK",
                    "封包送出失敗（詳見 [WEBHOOK] log）",
                    level="error",
                    sent=False,
                )
        return sent

    def _should_send_negative_access(
        self,
        barrier: dict,
        score_res: dict,
        product_b64_list: list,
    ) -> bool:
        """登入牆、或沒關鍵字／非目標 → 直接走非毒品封包。"""
        if barrier.get("should_reject"):
            return True
        if not score_res.get("matched"):
            return True
        return False

    def _should_send_negative_no_content(self, text: str, product_b64_list: list) -> bool:
        """沒抓到可分析內容 → 非毒品封包。"""
        return not (text or "").strip() and not product_b64_list

    def _is_full_packet_ready(
        self,
        text: str,
        keywords: list,
        screenshot_b64: str,
        full_screenshot_b64: str,
    ) -> bool:
        """完整 8 欄封包是否齊全；缺一不可。"""
        return bool(
            (text or "").strip()
            and keywords
            and (screenshot_b64 or "").strip()
            and (full_screenshot_b64 or "").strip()
        )

    def _build_context_options(self):
        return build_context_options(self.config, viewport=(1920, 1080))

    async def _setup_resource_routes(self, page: Page) -> None:
        await setup_resource_routes(
            page,
            block_fonts=bool(self._opts.get("block_fonts")),
            lightweight=False,
        )

    async def _deep_simulate_human(self, page: Page) -> None:
        await simulate_human(page, mode="deep")

    async def _light_simulate_human(self, page: Page) -> None:
        await simulate_human(page, mode="light")

    async def _dismiss_popups(self, page: Page) -> bool:
        return await dismiss_overlays(page, light=bool(self._opts.get("light_popup_dismiss")))

    async def _remove_overlays(self, page: Page) -> None:
        pass  # dismiss_overlays 已含 JS 清除

    async def _extract_product_images(
        self, page: Page, domain: str, save_local: bool = True
    ) -> Tuple[int, List[dict]]:
        target_dir = os.path.join(self.jpg_dir, f"manual_{domain}") if save_local else None
        b64_list = await extract_product_images(
            page,
            self.config,
            domain=domain,
            dedup=self.dedup,
            max_images=int(self._opts.get("max_product_images", 10)),
            min_size=200 if self._opts.get("fast_mode") else 100,
            lazy_load_scroll=not bool(self._opts.get("fast_mode", True)),
            image_timeout_ms=int(self._opts.get("image_download_timeout_ms", 3000)),
            parallel_downloads=int(self._opts.get("parallel_image_downloads", 1)),
            save_local=save_local,
            save_dir=target_dir,
            scroll_timeout_ms=5000 if self._opts.get("fast_mode", True) else 8000,
            max_scroll_steps=12 if self._opts.get("fast_mode", True) else 25,
        )
        return len(b64_list), b64_list

    def _should_run_auth_scan(self) -> bool:
        """fast / deep 皆執行屏障掃描（只做偵測、不填表，耗時極短）。"""
        return True

    def _should_early_exit_login(self, popup_login_hint: bool, barrier: dict) -> bool:
        """僅「明確屏障」才中止；fast 不因 popup 弱提示就判非毒品。"""
        if not self.config.get("auth_bypass", {}).get("reject_login_register_walls", True):
            return False
        if barrier.get("should_reject"):
            return True
        # 只有 deep 且明確開 early_exit 時，才採用 popup 弱提示
        if not self._opts.get("fast_mode", True) and bool(
            self._opts.get("early_exit_on_login_wall", False)
        ):
            return popup_login_hint
        return False

    async def _return_negative(
        self,
        normalized_url: str,
        crawl_log: CrawlLogger,
        reason: str,
        status: str = "negative",
    ) -> dict:
        crawl_log.phase("RESULT", reason)
        result = await self._finalize_report(
            normalized_url, self._build_negative_report(normalized_url), crawl_log=crawl_log
        )
        crawl_log.done(status=status)
        return result

    async def _record_task_to_db(self, url: str, report: dict, webhook_success: bool):
        try:
            db_path = get_record_paths(self.config)["db_queue"]
            os.makedirs("Record", exist_ok=True)
            async with aiosqlite.connect(db_path) as db:
                await db.execute("PRAGMA journal_mode=WAL;")
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS manual_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        url TEXT NOT NULL,
                        timestamp TEXT NOT NULL,
                        status TEXT NOT NULL,
                        matched_keywords TEXT,
                        webhook_sent INTEGER DEFAULT 0,
                        text_summary TEXT
                    )
                    """
                )
                await db.execute(
                    """
                    CREATE TABLE IF NOT EXISTS url_state (
                        url TEXT PRIMARY KEY,
                        domain TEXT,
                        status INTEGER DEFAULT 0,
                        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )

                domain = urlparse(url).netloc
                keywords_str = ",".join(report.get("keywords") or [])
                text_summary = (report.get("text_content") or "")[:200]
                status_str = (
                    "SKIP/FAILED"
                    if report.get("text_content") == NEGATIVE_ACCESS_TEXT
                    else "SUCCESS"
                )

                await db.execute(
                    """
                    INSERT INTO manual_tasks
                    (url, timestamp, status, matched_keywords, webhook_sent, text_summary)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        url,
                        report.get("timestamp", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                        status_str,
                        keywords_str,
                        1 if webhook_success else 0,
                        text_summary,
                    ),
                )
                await db.execute(
                    """
                    INSERT OR REPLACE INTO url_state (url, domain, status)
                    VALUES (?, ?, 1)
                    """,
                    (url, domain),
                )
                await db.commit()
                logging.info(
                    f"[DB] 手動採集任務已成功持久化存入 DB ({status_str}) | url={url}"
                )
        except Exception as db_e:
            logging.error(f"[DB] 寫入採集歷史紀錄至 SQLite 失敗: {db_e}")

    def _build_negative_report(self, normalized_url: str) -> dict:
        return build_negative_access_report(normalized_url, task_type="manual")

    async def _finalize_report(
        self, normalized_url: str, report: dict, crawl_log: Optional[CrawlLogger] = None
    ) -> dict:
        webhook_ok = await self._send_webhook_if_complete(report, crawl_log=crawl_log)
        await self._record_task_to_db(normalized_url, report, webhook_ok)
        if crawl_log:
            crawl_log.phase("DB", "任務已寫入 SQLite")
        payload = finalize_webhook_payload(
            build_webhook_payload_from_result(report, task_type="manual")
        )
        payload["webhook_sent"] = bool(webhook_ok)
        payload["threat_tier"] = report.get("tier") or "SKIP"
        return payload

    async def process_query(self, url: str, save_local: bool = True) -> dict:
        mode = "FAST" if self._opts.get("fast_mode") else "DEEP"
        normalized_url = url.split("#")[0]
        crawl_log = CrawlLogger(normalized_url, task_type="manual", config=self.config)
        crawl_log.start(mode=mode, save_local=save_local)

        domain = urlparse(normalized_url).netloc
        opts = self._opts

        pw = None
        browser = self._shared_browser
        owns_browser = False

        if browser is None:
            pw = await async_playwright().start()
            browser = await pw.chromium.launch(
                headless=self.config.get("headless", True),
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
            owns_browser = True
            crawl_log.phase("BROWSER", "自行啟動 Chromium（無常駐池）")
        else:
            crawl_log.phase("BROWSER", "使用常駐 Chromium")

        context = await browser.new_context(**self._build_context_options())
        page = await context.new_page()

        try:
            await stealth_async(page)
            page.on("dialog", lambda dialog: asyncio.ensure_future(dialog.accept()))
            await self._setup_resource_routes(page)
            crawl_log.phase("ROUTE", "資源攔截規則已套用", block_fonts=opts.get("block_fonts"))

            goto_timeout = int(opts.get("goto_timeout_ms", 10000))
            post_sleep = int(opts.get("post_goto_sleep_ms", 500)) / 1000.0

            crawl_log.phase("NAV", "開始載入頁面", timeout_ms=goto_timeout)
            try:
                await page.goto(
                    normalized_url,
                    wait_until="domcontentloaded",
                    timeout=goto_timeout,
                )
                crawl_log.phase("NAV", "domcontentloaded 完成")
            except Exception as nav_e:
                crawl_log.phase(
                    "NAV",
                    f"載入達 timeout，改用已渲染 DOM: {str(nav_e)[:80]}",
                    level="warning",
                )

            await asyncio.sleep(post_sleep)

            is_login_required = await self._dismiss_popups(page)
            await self._remove_overlays(page)
            crawl_log.phase("POPUP", "彈窗/Cookie 處理完成", login_wall_hint=is_login_required)

            barrier = {"should_reject": False, "barrier_type": "none", "reason": ""}
            barrier = await self.auth_engine.detect_access_barrier(page, normalized_url)
            crawl_log.phase(
                "AUTH",
                "屏障掃描完成",
                should_reject=barrier["should_reject"],
                barrier_type=barrier["barrier_type"],
                reason=barrier["reason"] or "none",
            )

            if self._should_early_exit_login(is_login_required, barrier):
                reason = barrier["reason"] or "彈窗掃描判定為登入/註冊牆"
                crawl_log.phase("AUTH", f"登入/註冊牆中止：{reason}")
                report = self._build_negative_report(normalized_url)
                result = await self._finalize_report(normalized_url, report, crawl_log=crawl_log)
                crawl_log.done(status="login_wall")
                return result

            if opts.get("skip_human_simulation", True):
                await self._light_simulate_human(page)
                crawl_log.phase("SCROLL", "輕量滾動完成")
            else:
                await self._deep_simulate_human(page)
                crawl_log.phase("SCROLL", "深度真人模擬完成")

            html_raw = await page.content()
            text = await safe_page_text(page)
            score_res = self.analyzer.analyze(text, html_raw, normalized_url)
            crawl_log.phase(
                "SCORE",
                "啟發式評分完成",
                tier=score_res.get("tier"),
                score=score_res.get("score"),
                keywords=",".join(score_res.get("matched") or [])[:120],
            )

            # 沒關鍵字＝非目標／非毒品：直接送精簡封包，避免新聞站抓圖滾動卡死
            if not score_res.get("matched"):
                return await self._return_negative(
                    normalized_url,
                    crawl_log,
                    "無關鍵字命中，送非毒品封包（略過截圖/商品圖）",
                    status="negative",
                )

            timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")

            if save_local:
                html_path = os.path.join(
                    self.html_dir, f"manual_{domain}_{timestamp_str}.html"
                )
                with open(html_path, "w", encoding="utf-8") as f:
                    f.write(html_raw)
                crawl_log.phase("SAVE", f"HTML 已存檔", path=html_path)

            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
            viewport_binary = await page.screenshot(type="jpeg", quality=80, full_page=False)
            screenshot_b64 = base64.b64encode(viewport_binary).decode("utf-8")
            full_screenshot_b64 = screenshot_b64
            crawl_log.phase("SCREENSHOT", "viewport 截圖完成")

            if self.capture_full_page_screenshot:
                full_binary = await page.screenshot(type="jpeg", quality=80, full_page=True)
                full_screenshot_b64 = base64.b64encode(full_binary).decode("utf-8")
                if save_local:
                    jpg_path = os.path.join(
                        self.jpg_dir, f"manual_{domain}_{timestamp_str}_full.jpg"
                    )
                    with open(jpg_path, "wb") as f:
                        f.write(full_binary)

            elif save_local:
                jpg_path = os.path.join(
                    self.jpg_dir, f"manual_{domain}_{timestamp_str}.jpg"
                )
                with open(jpg_path, "wb") as f:
                    f.write(viewport_binary)

            _, product_b64_list = await self._extract_product_images(
                page, domain, save_local=save_local
            )
            crawl_log.phase("IMAGES", "商品圖擷取完成", count=len(product_b64_list))

            if self._should_send_negative_access(barrier, score_res, product_b64_list):
                return await self._return_negative(
                    normalized_url,
                    crawl_log,
                    "判定為登入牆或非目標（送 negative）",
                    status="negative",
                )

            if self._should_send_negative_no_content(text, product_b64_list):
                return await self._return_negative(
                    normalized_url,
                    crawl_log,
                    "無法取得頁面內容（送 negative）",
                    status="negative",
                )

            if not self._is_full_packet_ready(
                text,
                score_res.get("matched") or [],
                screenshot_b64,
                full_screenshot_b64,
            ):
                return await self._return_negative(
                    normalized_url,
                    crawl_log,
                    "資料不齊（缺關鍵字/文字/截圖），送非毒品封包",
                    status="negative",
                )

            report = {
                "task_type": "manual",
                "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "keywords": score_res["matched"],
                "url": normalized_url,
                "screenshot_b64": screenshot_b64,
                "full_screenshot_base64": full_screenshot_b64,
                "product_images_b64": product_b64_list,
                "text_content": text,
                "tier": score_res.get("tier"),
                "score": score_res.get("score"),
            }
            result = await self._finalize_report(normalized_url, report, crawl_log=crawl_log)
            crawl_log.done(
                status="skip" if score_res.get("tier") == "SKIP" else "success",
                tier=score_res.get("tier"),
                images=len(product_b64_list),
            )
            return result

        except Exception as e:
            err_msg = f"網頁抓取失敗或逾時: {str(e)[:100]}"
            crawl_log.phase("ERROR", err_msg, level="error")
            return await self._return_negative(
                normalized_url,
                crawl_log,
                f"爬取失敗，送非毒品封包: {err_msg}",
                status="failed",
            )
        finally:
            await context.close()
            if owns_browser:
                await browser.close()
                if pw:
                    await pw.stop()


if __name__ == "__main__":
    async def test():
        investigator = ManualInvestigator()
        print(">> 開始手動單網址採集測試:")
        res = await investigator.process_query("https://www.wikipedia.org/")
        if "screenshot_b64" in res and res["screenshot_b64"]:
            res["screenshot_b64"] = "<SCREENSHOT_BASE64_HIDDEN>"
        if "full_screenshot_base64" in res and res.get("full_screenshot_base64"):
            res["full_screenshot_base64"] = "<FULL_SCREENSHOT_BASE64_HIDDEN>"
        if "product_images_b64" in res:
            res["product_images_b64"] = [
                f"<PRODUCT_IMAGE_BASE64_HIDDEN_{i}>"
                for i in range(len(res["product_images_b64"]))
            ]
        print("測試結果:", json.dumps(res, indent=2, ensure_ascii=False))

    asyncio.run(test())

import asyncio
import logging
import json
import os
import random
import sys
import aiosqlite
import tldextract
from datetime import datetime
from typing import List, Dict, Set, Optional, Any
from crawler import AntiDetectionCrawler
from record_paths import (
    ensure_record_layout,
    append_visited,
    append_images_record,
    append_nlp_record,
)
from webhook_helper import (
    get_webhook_helper,
    build_webhook_payload_from_result,
    finalize_webhook_payload,
    validate_webhook_payload,
)

# 配置日誌 → 只寫 Record/log_24h.txt + 終端
os.makedirs("Record", exist_ok=True)
_LOG_24H = "Record/log_24h.txt"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(_LOG_24H, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

NL_TEMPLATES = [
    "buy {product} online \"add to cart\" {payment}",
    "order {product} with {payment} discreet shipping",
    "{product} for sale \"buy now\" {payment}",
    "where to buy {product} online shop {payment}",
    "best {product} online shop checkout {payment}",
    "{product} anonymous shipping trusted vendor",
    "shop {product} fast delivery {payment}",
    "{product} \"add to cart\" {payment}",
    "legit {product} ship anywhere {payment}",
    "stealth {product} worldwide delivery {payment}",
    "cheap {product} real deal checkout {payment}",
]

# 防 Spider Trap 設定: 單一主網域最多爬取頁面數
MAX_URLS_PER_DOMAIN = 50

class DualTrackEngine:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.record_paths = ensure_record_layout(self.config)
        self.crawler = AntiDetectionCrawler(
            headless=self.config.get("headless", True),
            config=self.config
        )

        self.db_path = self.record_paths.get("db_monitor", "Record/monitor_state.db")
        self.webhook = get_webhook_helper(self.config)
        self.found_shops = []
        self.learned_fingerprints = set()

        # 24H 不再建立／寫入 testjpg、testHTML；本地紀錄只走 Record/
        self.db: Any = None
        self._is_shutting_down = False
        self._queue_lock = asyncio.Lock()

    def _load_config(self):
        try:
            if os.path.exists(self.config_path):
                with open(self.config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            return {}
        except Exception as e:
            logging.error(f"Error loading config: {e}")
            return {}

    def _extract_domain(self, url: str) -> str:
        ext = tldextract.extract(url)
        return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain

    async def _init_db(self):
        self.db = await aiosqlite.connect(self.db_path)
        await self.db.execute('PRAGMA journal_mode=WAL;')

        await self.db.execute('''
        CREATE TABLE IF NOT EXISTS url_state (
            url TEXT PRIMARY KEY,
            domain TEXT,
            status INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')

        await self.db.execute('''
        CREATE TABLE IF NOT EXISTS domain_stats (
            domain TEXT PRIMARY KEY,
            count INTEGER DEFAULT 0
        )
        ''')

        await self.db.execute('CREATE INDEX IF NOT EXISTS idx_status ON url_state(status)')
        await self.db.commit()

    async def _mark_url_done(self, url: str) -> None:
        if not self.db or not url:
            return
        await self.db.execute(
            'UPDATE url_state SET status = 2 WHERE url = ?', (url,)
        )
        await self.db.commit()

    async def _finalize_stale_in_progress(self) -> None:
        """啟動時：卡住的 in_progress 標 done，避免永久佔 worker。"""
        async with self.db.execute(
            'SELECT COUNT(*) FROM url_state WHERE status = 1'
        ) as cursor:
            row = await cursor.fetchone()
            stuck = row[0] if row else 0
        if stuck:
            await self.db.execute('UPDATE url_state SET status = 2 WHERE status = 1')
            await self.db.commit()
            logging.info(f"[DB] {stuck} 筆卡住的 in_progress 已標為 done")

    def _is_page_url(self, url: str) -> bool:
        """非網頁檔（pdf/xml/rss…）不進佇列、不重試。這不是網域黑名單。"""
        low = (url or "").lower().split("#", 1)[0].split("?", 1)[0]
        junk = (
            ".pdf", ".zip", ".rar", ".7z", ".mp4", ".mp3", ".avi",
            ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico",
            ".css", ".js", ".woff", ".woff2", ".xml", ".rss", ".atom",
        )
        if any(low.endswith(ext) for ext in junk):
            return False
        if low.endswith("/atom.xml") or low.endswith("/feed") or "/feed/" in low:
            return False
        return True

    async def _add_to_queue(self, url: str) -> bool:
        """加入佇列並判斷網域保護 (Spider Trap 防護)"""
        url = (url or "").split("#", 1)[0].strip()
        if not url.startswith(("http://", "https://")):
            return False
        if not self._is_page_url(url):
            return False
        domain = self._extract_domain(url)

        if domain.endswith('.onion') or domain.endswith('.i2p'):
            return False

        async with self.db.execute('SELECT status FROM url_state WHERE url = ?', (url,)) as cursor:
            if await cursor.fetchone() is not None:
                return False

        async with self.db.execute('SELECT count FROM domain_stats WHERE domain = ?', (domain,)) as cursor:
            row = await cursor.fetchone()
            count = row[0] if row else 0

        if count >= MAX_URLS_PER_DOMAIN:
            await self.db.execute('INSERT OR IGNORE INTO url_state (url, domain, status) VALUES (?, ?, 2)', (url, domain))
            return False

        await self.db.execute('INSERT INTO url_state (url, domain, status) VALUES (?, ?, 0)', (url, domain))
        await self.db.execute('INSERT INTO domain_stats (domain, count) VALUES (?, ?) ON CONFLICT(domain) DO UPDATE SET count = count + 1', (domain, count + 1))
        await self.db.commit()
        return True

    async def _get_next_url(self) -> Optional[str]:
        """原子領取下一筆 URL，避免 8 worker 搶到同一條。"""
        async with self._queue_lock:
            async with self.db.execute(
                "SELECT url FROM url_state WHERE status = 0 ORDER BY added_at ASC LIMIT 1"
            ) as cursor:
                row = await cursor.fetchone()
            if not row:
                return None
            url = row[0]
            cur = await self.db.execute(
                "UPDATE url_state SET status = 1 WHERE url = ? AND status = 0",
                (url,),
            )
            await self.db.commit()
            if cur.rowcount == 0:
                return None
            return url

    async def _get_queue_size(self) -> int:
        async with self.db.execute('SELECT COUNT(*) FROM url_state WHERE status = 0') as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    async def get_queue_stats(self) -> Dict[str, Any]:
        """main.py /api/v1/monitor/status 需要（原版無此方法，僅此相容補丁）"""
        visited = 0
        if self.db:
            async with self.db.execute(
                'SELECT COUNT(*) FROM url_state WHERE status != 0'
            ) as cursor:
                row = await cursor.fetchone()
                visited = row[0] if row else 0
        return {
            "queue_size": await self._get_queue_size(),
            "visited_count": visited,
            "found_count": len(self.found_shops),
            "db_connected": self.db is not None,
        }

    def _save_intel(self, res: Dict):
        # 記憶體只留摘要，避免 found_shops 堆 base64 導致 OOM（INFRA-02）
        slim_mem = {
            "url": res.get("url") or "",
            "tier": res.get("tier", "?"),
            "score": res.get("score", 0),
            "matched": list(res.get("matched") or [])[:30],
            "fingerprints": list(res.get("fingerprints") or [])[:20],
        }
        self.found_shops.append(slim_mem)
        for fp in slim_mem["fingerprints"]:
            self.learned_fingerprints.add(fp)

        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        url = slim_mem["url"]
        tier = slim_mem["tier"]
        score = slim_mem["score"]

        # 1) NLP／文字摘要（JSONL 追加，不含全文）
        append_nlp_record(self.record_paths, {
            "timestamp": ts,
            "url": url,
            "tier": tier,
            "score": score,
            "matched": slim_mem["matched"],
            "fingerprints": slim_mem["fingerprints"],
            "text_len": len(res.get("text_content") or ""),
            "source": "24H",
        })
        # 2) 圖片摘要（JSONL 追加，不含 base64）
        products = res.get("product_images") or []
        append_images_record(self.record_paths, {
            "timestamp": ts,
            "url": url,
            "tier": tier,
            "score": score,
            "has_screenshot": bool(
                (res.get("screenshot_b64") or "").strip()
                or (res.get("full_screenshot_base64") or "").strip()
            ),
            "product_images": [
                {"filename": (p.get("filename") if isinstance(p, dict) else f"image_{i:02d}")}
                for i, p in enumerate(products, start=1)
                if p
            ],
            "source": "24H",
        })
        # 3) 造訪總表
        append_visited(
            self.record_paths, url, source="24H", tier=str(tier), score=score
        )

        payload = finalize_webhook_payload(
            build_webhook_payload_from_result(res, task_type="automated_24h")
        )
        ok, errors = validate_webhook_payload(payload)
        if ok:
            asyncio.create_task(self.webhook.send_result(payload))
        else:
            logging.warning(
                f"[WEBHOOK] 封包不完整，略過發送 ({'; '.join(errors)}) "
                f"| url={payload.get('url')}"
            )

    def _gen_queries(self) -> List[str]:
        products = self.config.get("keyword_groups", {}).get("A_Product", [])
        payments = self.config.get("keyword_groups", {}).get("C_Payment_Contact", [])
        dorks = self.config.get("keyword_groups", {}).get("E_Advanced_Dorks", [])

        queries = list(dorks)
        if products and payments:
            product_samples = random.sample(products, min(8, len(products)))
            payment_samples = random.sample(payments, min(5, len(payments)))
            for product in product_samples:
                template = random.choice(NL_TEMPLATES)
                payment = random.choice(payment_samples)
                queries.append(template.format(product=product, payment=payment))

        if self.learned_fingerprints:
            for fp in random.sample(list(self.learned_fingerprints), min(len(self.learned_fingerprints), 3)):
                queries.append(f'inurl:"{fp}" "{random.choice(products) if products else ""}"')

        return queries

    async def harvester_track(self):
        logging.info("[TRACK A - v2] Harvester 啟動：執行多語義智能搜尋 (SQLite驅動)...")
        try:
            while not self._is_shutting_down:
                try:
                    queries = self._gen_queries()
                    if not queries:
                        await asyncio.sleep(60)
                        continue
                    q = random.choice(queries)
                    found = await self.crawler.search_harvester(q, max_results=10)

                    added = 0
                    for u in found:
                        if await self._add_to_queue(u):
                            added += 1

                    q_size = await self._get_queue_size()
                    logging.info(f"[TRACK A] 搜尋完成。新發現: {added} 筆。當前待爬隊列長度: {q_size}")

                    interval = self.config.get("search_interval_seconds", 300)
                    jitter = random.uniform(0.9, 1.1)
                    await asyncio.sleep(interval * jitter)
                except Exception as e:
                    logging.error(f"[TRACK A] 循環錯誤: {e}")
                    await asyncio.sleep(60)
        finally:
            logging.info("[TRACK A] Harvester 停止...")

    async def investigator_track(self):
        max_workers = self.config.get("max_workers", 5)
        logging.info(f"[TRACK B - v2] Investigator 啟動：執行 {max_workers} 軌非同步採集...")

        async def work(worker_id):
            while not self._is_shutting_down:
                url = await self._get_next_url()
                if url is None:
                    # 資料庫空了，休息一下再問
                    await asyncio.sleep(5)
                    continue

                try:
                    if not self._is_page_url(url):
                        logging.info(f"   [SKIP] 非網頁檔，略過: {url}")
                        continue
                    # 完全沿用您原版的爬蟲分析邏輯
                    res = await self.crawler.crawl(url, lightweight=False)
                    if res and res.get("tier") != "SKIP":
                        self._save_intel(res)
                        logging.info(
                            f"[TRACK B] 入庫+送封包 {res.get('tier')} "
                            f"score={res.get('score')} | {url}"
                        )
                        for link in res.get("links", []):
                            if not any(
                                se in link.lower()
                                for se in ("google.", "bing.", "duckduckgo.", "gibiru.")
                            ):
                                await self._add_to_queue(link)
                    else:
                        # SKIP 也記造訪，方便對帳
                        append_visited(
                            self.record_paths,
                            url,
                            source="24H",
                            tier=str((res or {}).get("tier") or "SKIP"),
                            score=(res or {}).get("score", 0),
                            status="skip",
                        )
                except Exception as e:
                    logging.error(f"[TRACK B] 處理失敗 {url}: {e}")
                    append_visited(
                        self.record_paths, url, source="24H", status="error"
                    )
                finally:
                    await self._mark_url_done(url)

        workers = [asyncio.create_task(work(i)) for i in range(max_workers)]
        await asyncio.gather(*workers)

    async def run(self):
        try:
            await self._init_db()
            await self._finalize_stale_in_progress()
            max_workers = self.config.get("max_workers", 5)
            await self.crawler.init()
            logging.info(f"V46.0 Fully Optimized Dual-Track Predator Engine Active (SQLite Mode). Workers: {max_workers}")

            await asyncio.gather(
                self.harvester_track(),
                self.investigator_track()
            )
        except asyncio.CancelledError:
            self._is_shutting_down = True
            logging.info("Shutting down workers...")
        finally:
            if self.db:
                await self.db.close()
            await self.crawler.close()
            logging.info("Crawler and DB resources released.")

async def main():
    engine = DualTrackEngine()
    try:
        await engine.run()
    except KeyboardInterrupt:
        engine._is_shutting_down = True

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logging.info("Mission Aborted. All processes terminated.")

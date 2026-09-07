
## 已廢棄 — 請使用 engine_v2.py + main.py。此檔仍指向舊 Record 路徑，勿直接執行。

## 這是engine_v2.py 的基礎 複製品，這不是24h 執行檔


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
from webhook_helper import get_webhook_helper

# 配置日誌
os.makedirs("Record", exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("Record/operation_v2.log", encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

NL_TEMPLATES = [
    "buy {product} online discreet shipping",
    "order {product} with {payment} no prescription",
    "{product} for sale accept {payment}",
    "where to buy {product} discreetly online",
    "best {product} online shop {payment}",
    "{product} anonymous shipping trusted vendor",
    "shop {product} fast delivery USA {payment}",
    "{product} quality trusted plug {payment}",
    "legit {product} ship anywhere {payment}",
    "stealth {product} worldwide delivery {payment}",
    "{product} vendor reviews {payment}",
    "cheap {product} real deal {payment}",
]

# 防 Spider Trap 設定: 單一主網域最多爬取頁面數
MAX_URLS_PER_DOMAIN = 50 

class DualTrackEngine:
    def __init__(self, config_path="config.json"):
        self.config_path = config_path
        self.config = self._load_config()
        self.crawler = AntiDetectionCrawler(
            headless=self.config.get("headless", True),
            config=self.config
        )

        self.db_path = "Record/monitor_state.db"
        self.webhook = get_webhook_helper(self.config)
        self.found_shops = []
        self.learned_fingerprints = set()

        output_dirs = self.config.get("output_dirs", {})
        self.jpg_dir = output_dirs.get("test_jpg", "testjpg")
        self.html_dir = output_dirs.get("test_html", "testHTML")
        os.makedirs(self.jpg_dir, exist_ok=True)
        os.makedirs(self.html_dir, exist_ok=True)

        self.shop_file = "Record/Potential_Shops.txt"
        self.report_file = "Record/intel_report.json"
        
        self.db: Any = None
        self._is_shutting_down = False

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
    
    async def _add_to_queue(self, url: str) -> bool:
        """加入佇列並判斷網域保護 (Spider Trap 防護)"""
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
        """分配下一個要爬取的網址給 Worker"""
        async with self.db.execute('SELECT url FROM url_state WHERE status = 0 ORDER BY added_at ASC LIMIT 1') as cursor:
            row = await cursor.fetchone()
            if row:
                url = row[0]
                await self.db.execute('UPDATE url_state SET status = 1 WHERE url = ?', (url,))
                await self.db.commit()
                return url
        return None

    async def _get_queue_size(self) -> int:
        async with self.db.execute('SELECT COUNT(*) FROM url_state WHERE status = 0') as cursor:
            row = await cursor.fetchone()
            return row[0] if row else 0

    def _save_intel(self, res: Dict):
        self.found_shops.append(res)
        for fp in res.get("fingerprints", []):
            self.learned_fingerprints.add(fp)
        with open(self.report_file, "w", encoding="utf-8") as f:
            json.dump(self.found_shops, f, indent=2, ensure_ascii=False)
        
        # 從 url 提取正確的 domain，僅用於本地存檔確認
        from urllib.parse import urlparse
        domain_name = urlparse(res.get("url", "")).netloc or "unknown"
        
        from webhook_helper import (
            build_webhook_payload_from_result,
            finalize_webhook_payload,
        )

        payload = finalize_webhook_payload(
            build_webhook_payload_from_result(res, task_type="automated_24h")
        )
        asyncio.create_task(self.webhook.send_result(payload))

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
                q = template.format(product=product, payment=payment)
                queries.append(q)

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
                    # 完全沿用您原版的爬蟲分析邏輯
                    res = await self.crawler.crawl(url, lightweight=False)
                    if res and res.get("tier") != "SKIP":
                        self._save_intel(res)
                        
                        # 把發現的新網頁加進 SQLite 排隊
                        for link in res.get("links", []):
                            if not any(se in link.lower() for se in ["google.", "bing.", "duckduckgo."]):
                                await self._add_to_queue(link)
                except Exception as e:
                    logging.error(f"[TRACK B] 處理失敗 {url}: {e}")

        workers = [asyncio.create_task(work(i)) for i in range(max_workers)]
        await asyncio.gather(*workers)

    async def run(self):
        try:
            await self._init_db()
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
"""
用 visited.txt 重建 24H 引擎的 monitor_state.db。

為什麼需要這支：`visited.txt` 只有 append_visited 在寫，**沒有任何地方讀它來去重**。
24H 引擎的去重狀態全在 monitor_state.db 的 url_state 表
（engine_v2.py:157 的 `SELECT status FROM url_state WHERE url = ?`），
那個檔跟 volume 一起沒了的話，爬蟲會把已經爬過的網址整批重爬。

重爬不會在資料庫產生重複資料——suspect_websites.url 是 UNIQUE，
但那代表 /api/crawler/report/ 會丟 IntegrityError、被 crawler.py 的
broad except 接住後回 500，爬蟲重試三次再寫死信檔。等於白跑而且什麼都沒更新。

domain_stats 也要一起重建：count >= MAX_URLS_PER_DOMAIN(50) 的網域會被擋，
不還原的話那些早就爬滿的網域會重新開放，行為跟原本不一樣。
"""
import os
import re
import sqlite3
import sys
from collections import Counter

import tldextract

VISITED = os.getenv("VISITED_PATH", "/out/visited.txt")
DB = os.getenv("MONITOR_DB", "/r/monitor_state.db")
LINE = re.compile(r"^\[([\d\- :]+)\]\s+source=(\S+).*?\|\s*(\S+)\s*$")

# engine_v2.py 的 status：0=待爬 1=進行中 2=已完成
DONE = 2


def domain_of(url):
    ext = tldextract.extract(url)
    return f"{ext.domain}.{ext.suffix}" if ext.suffix else ext.domain


def main():
    urls, ts = {}, {}
    with open(VISITED, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            m = LINE.match(line)
            if not m:
                continue
            when, _src, url = m.groups()
            if url.startswith(("http://", "https://")):
                urls.setdefault(url, domain_of(url))
                ts.setdefault(url, when)
    print(f"visited.txt 解析出 {len(urls)} 個網址", flush=True)

    counts = Counter(urls.values())
    print(f"涵蓋 {len(counts)} 個網域，"
          f"其中 {sum(1 for c in counts.values() if c >= 50)} 個已達單網域上限", flush=True)

    conn = sqlite3.connect(DB)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""CREATE TABLE IF NOT EXISTS url_state (
        url TEXT PRIMARY KEY, domain TEXT, status INTEGER DEFAULT 0,
        added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")
    conn.execute("""CREATE TABLE IF NOT EXISTS domain_stats (
        domain TEXT PRIMARY KEY, count INTEGER DEFAULT 0)""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_status ON url_state(status)")

    conn.executemany(
        "INSERT OR IGNORE INTO url_state (url, domain, status, added_at) VALUES (?,?,?,?)",
        [(u, d, DONE, ts.get(u)) for u, d in urls.items()],
    )
    conn.executemany(
        "INSERT INTO domain_stats (domain, count) VALUES (?,?) "
        "ON CONFLICT(domain) DO UPDATE SET count = excluded.count",
        list(counts.items()),
    )
    conn.commit()

    n = conn.execute("SELECT COUNT(*) FROM url_state WHERE status=2").fetchone()[0]
    d = conn.execute("SELECT COUNT(*) FROM domain_stats").fetchone()[0]
    pending = conn.execute("SELECT COUNT(*) FROM url_state WHERE status=0").fetchone()[0]
    conn.close()
    print(f"寫入 {DB}：已完成 {n} 筆、待爬 {pending} 筆、網域 {d} 個")


if __name__ == "__main__":
    sys.exit(main())

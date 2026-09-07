"""
把 suspect_websites.html_content 重新丟給 NLP 服務，補回 ai_analysis_results
的 nlp_details（關鍵字）與 nlp_score。

兩個要修的東西：

1. 還原時 nlp_details 沒有備份，填的是「還原：原始文字明細未備份」佔位字串。
2. 更糟的是：還原時如果爬蟲記錄檔有 matched 欄位，就拿它填 nlp_details——
   那是爬蟲的正則比對結果（price_pattern、Add to cart、schema.org/Product），
   不是模型抽的關鍵字。同一個欄位混了兩種來源而且沒有標示，會誤導判讀。

NLP 服務收到 /predict 之後會自己推一份給後端的 /api/nlp/report/，
所以這裡只要送文字就好，不用自己寫資料庫。

一次讀一筆，不把 854 筆網頁文字一起載入。
"""
import os
import sys
import time

import pymysql
import requests

NLP = os.getenv("NLP_PREDICT_URL", "http://nlp:8000/predict")
DELAY = float(os.getenv("REPLAY_DELAY", "0.2"))
LIMIT = int(os.getenv("REPLAY_LIMIT", "0"))
MIN_CHARS = 200          # 太短的頁面抽不出東西，跳過


def connect():
    return pymysql.connect(
        host="mysql", port=3306, user=os.environ["DB_USER"],
        password=os.environ["DB_PASSWORD"], database=os.environ["DB_NAME"],
        charset="utf8mb4", autocommit=True,
    )


def main():
    conn = connect()
    with conn:
        c = conn.cursor()
        c.execute(
            "SELECT s.id FROM suspect_websites s "
            "JOIN ai_analysis_results a ON a.url = s.url "
            "WHERE CHAR_LENGTH(s.html_content) >= %s "
            "GROUP BY s.id ORDER BY s.id",
            (MIN_CHARS,),
        )
        ids = [r[0] for r in c.fetchall()]
        if LIMIT:
            ids = ids[:LIMIT]
        print(f"待重跑：{len(ids)} 筆", flush=True)

        ok = fail = 0
        t0 = time.time()
        for n, sid in enumerate(ids, 1):
            c.execute("SELECT url, html_content FROM suspect_websites WHERE id=%s", (sid,))
            url, text = c.fetchone()
            try:
                r = requests.post(NLP, json={"url": url, "text": text or ""}, timeout=180)
                ok += 1 if r.status_code == 200 else 0
                fail += 0 if r.status_code == 200 else 1
                if r.status_code != 200 and fail <= 5:
                    print(f"  ⚠️ {r.status_code} {r.text[:150]}", flush=True)
            except Exception as e:                                  # noqa: BLE001
                fail += 1
                if fail <= 5:
                    print(f"  ⚠️ {type(e).__name__}: {e}", flush=True)
            time.sleep(DELAY)

            if n % 50 == 0 or n == len(ids):
                el = time.time() - t0
                print(f"  {n}/{len(ids)}　成功 {ok} 失敗 {fail}　"
                      f"{n / el if el else 0:.1f} 筆/秒", flush=True)

        print(f"\n送出 {len(ids)} 筆，成功 {ok}，失敗 {fail}，"
              f"耗時 {time.time() - t0:.0f} 秒", flush=True)

        time.sleep(10)
        c.execute("SELECT SUM(nlp_details LIKE '還原%%'), COUNT(*) FROM ai_analysis_results")
        placeholder, total = c.fetchone()
        print(f"仍是佔位字串：{placeholder} / {total}")


if __name__ == "__main__":
    sys.exit(main())

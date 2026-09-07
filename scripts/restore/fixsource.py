"""
用 visited.txt 補回 task_source 與 created_at。

現存的 nlp_text.json / images.json 只涵蓋最近幾輪爬取（1182 個網址），
但備份檔裡有 3197 筆。剩下 2015 筆先前被當成 manual，
在 24 小時清單上看不到——那是這個系統的主畫面。
"""
import json, os, pymysql

idx = json.load(open("/out/visited_index.json", encoding="utf-8"))
conn = pymysql.connect(host="mysql", port=3306, user=os.environ["DB_USER"],
                       password=os.environ["DB_PASSWORD"], database=os.environ["DB_NAME"],
                       charset="utf8mb4", autocommit=False)
with conn:
    c = conn.cursor()
    c.execute("SELECT id, url, task_source, created_at FROM ai_analysis_results")
    src_fixed = ts_fixed = 0
    for rid, url, src, created in c.fetchall():
        v = idx.get(url)
        if not v:
            continue
        want = "[automated_24h] 爬蟲自動通報" if v["source"].upper().startswith("24") \
               else "[manual] 爬蟲自動通報"
        sets, args = [], []
        if src != want:
            sets.append("task_source=%s"); args.append(want); src_fixed += 1
        if created is None:
            sets.append("created_at=%s"); args.append(v["timestamp"]); ts_fixed += 1
        if sets:
            args.append(rid)
            c.execute(f"UPDATE ai_analysis_results SET {', '.join(sets)} WHERE id=%s", args)
    conn.commit()
    print(f"task_source 修正 {src_fixed} 筆，created_at 補上 {ts_fixed} 筆")

    c.execute("SELECT COUNT(*) FROM ai_analysis_results WHERE task_source LIKE '%[automated_24h]%'")
    print("24 小時清單筆數：", c.fetchone()[0])
    c.execute("SELECT COUNT(*) FROM ai_analysis_results WHERE created_at IS NULL")
    print("仍缺發現時間：", c.fetchone()[0])

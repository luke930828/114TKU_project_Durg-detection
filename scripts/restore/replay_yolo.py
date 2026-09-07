"""
把還原回 suspect_websites 的商品圖重新丟給 YOLO，補回 ai_analysis_results
的 representative_image_base64 / class_metadata / yolo_score / yolo_details。

前端只讀 representative_image_base64（AIDetection.tsx:144），那是 YOLO 挑的
代表圖，爬蟲記錄檔裡沒有，只能重跑模型產生。

送出的 payload 跟 utils.dispatch_to_ai_engines 完全一樣（同樣的 task_id 格式與
total_images），YOLO 收滿整批才會回報一次；回報走的是 /api/ai_result/report/，
也就是這一輪剛加上驗證的那個端點——順便當成 SEC-01 的實地驗證。

一次只處理一個網址、一張圖一個請求，不把整批圖讀進記憶體。
"""
import json
import os
import sys
import time
import uuid

import pymysql
import requests

YOLO = os.getenv("YOLO_API_URL", "http://yolo:5000/api/v1/predict/trigger")
LIMIT = int(os.getenv("REPLAY_LIMIT", "0"))          # 0 = 全部
ONLY = os.getenv("REPLAY_URL", "")                   # 指定單一網址測試
# 每張之間停一下。YOLO 收到請求就丟進背景任務，沒有佇列上限——
# 4665 張一次灌進去，等於把幾百 MB 的 base64 同時堆在記憶體裡，
# 而且第一次 fuse() 會被併發撞出 'Conv' object has no attribute 'bn'。
DELAY = float(os.getenv("REPLAY_DELAY", "0.25"))


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
        if ONLY:
            c.execute("SELECT id FROM suspect_websites WHERE url=%s", (ONLY,))
        else:
            # 只挑「有圖、而且 YOLO 根本沒跑過」的。
            #
            # 不能用「沒有代表圖」當條件——YOLO 只在信心度 >= 0.5 時才產生
            # 代表圖（yolo/app/main.py:155），所以跑過但沒圖是正常結果。
            # 用那個條件的話，1357 筆裡有 1276 筆會被一遍遍重跑而且永遠
            # 不會「跑完」，實際需要補的只有 81 筆。
            #
            # 真正沒跑過的特徵是 yolo_details 還停在建檔時的佔位字串。
            c.execute(
                "SELECT s.id FROM suspect_websites s "
                "JOIN ai_analysis_results a ON a.url = s.url "
                "WHERE JSON_LENGTH(s.images_data) > 0 "
                "  AND (a.yolo_details IS NULL "
                "       OR a.yolo_details = '' "
                "       OR a.yolo_details LIKE '影像分析中%%' "
                "       OR a.yolo_details LIKE '還原%%') "
                "GROUP BY s.id ORDER BY s.id"
            )
        ids = [r[0] for r in c.fetchall()]
        if LIMIT:
            ids = ids[:LIMIT]
        print(f"待處理網址：{len(ids)}", flush=True)

        ok = fail = sent = 0
        t0 = time.time()
        for n, sid in enumerate(ids, 1):
            # 一次只把一筆的圖讀進來
            c.execute("SELECT url, images_data FROM suspect_websites WHERE id=%s", (sid,))
            url, blob = c.fetchone()
            try:
                pics = json.loads(blob or "[]")
            except json.JSONDecodeError:
                pics = []
            if not pics:
                continue

            task = str(uuid.uuid4())[:8]
            for i, b64 in enumerate(pics):
                try:
                    r = requests.post(YOLO, timeout=120, json={
                        "task_id": f"{task}_{i}",
                        "url": url,
                        "image_base64": b64,
                        "total_images": len(pics),
                        "priority": 0,
                    })
                    if r.status_code == 200:
                        ok += 1
                    else:
                        fail += 1
                        if fail <= 5:
                            print(f"  ⚠️ {r.status_code} {r.text[:160]}", flush=True)
                except Exception as e:                            # noqa: BLE001
                    fail += 1
                    if fail <= 5:
                        print(f"  ⚠️ {type(e).__name__}: {e}", flush=True)
                sent += 1
                time.sleep(DELAY)

            if n % 25 == 0 or n == len(ids):
                el = time.time() - t0
                rate = sent / el if el else 0
                print(f"  {n}/{len(ids)} 網址　{sent} 張　"
                      f"成功 {ok} 失敗 {fail}　{rate:.1f} 張/秒", flush=True)

        print(f"\n送出 {sent} 張，成功 {ok}，失敗 {fail}，"
              f"耗時 {time.time() - t0:.0f} 秒", flush=True)

        time.sleep(10)          # 等最後幾批回報寫入
        c.execute("SELECT COUNT(*) FROM ai_analysis_results "
                  "WHERE representative_image_base64 IS NOT NULL "
                  "  AND representative_image_base64 <> ''")
        print("已有代表圖的分析結果：", c.fetchone()[0])
        c.execute("SELECT COUNT(*) FROM ai_analysis_results "
                  "WHERE yolo_details LIKE '影像分析中%%' OR yolo_details LIKE '還原%%'")
        print("仍未跑過 YOLO：", c.fetchone()[0])


if __name__ == "__main__":
    sys.exit(main())

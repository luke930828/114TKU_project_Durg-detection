"""
把 08-30 的分析結果備份與爬蟲記錄檔灌回 drug_prevention_db。

風險分級不在這裡重寫一套：直接 import 後端的 utils.py。
這個專案已經因為「同一套門檻寫在三個地方」出過事（BUG-01），
還原腳本沒有理由變成第四個。

images.jsonl 有 771 MB，一律逐行讀，不整份載入。
"""
import json
import os
import sys

import pymysql

sys.path.insert(0, "/app")
from utils import calculate_multimodal_risk_100_scale  # noqa: E402

TSV = "/out/risk_level_before_20260830_024341.tsv"
RESTORE_NOTE = "還原自爬蟲記錄檔 2026-08-31"
# 單筆 row 的圖片上限。MySQL 8 的 max_allowed_packet 預設 64 MB，
# 一筆超過就會整個連線被砍掉，而不是只有那一筆失敗。
MAX_IMAGES_BYTES = 32 * 1024 * 1024


def connect():
    return pymysql.connect(
        host="mysql", port=3306,
        user=os.environ["DB_USER"], password=os.environ["DB_PASSWORD"],
        database=os.environ["DB_NAME"], charset="utf8mb4", autocommit=False,
    )


def task_type_of(rec):
    """24H → automated_24h，其餘當手動。爬蟲原本就是這樣標的。"""
    return "automated_24h" if (rec.get("source") or "").upper().startswith("24") else "manual"


def jsonl(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main():
    index = json.load(open("/out/index.json", encoding="utf-8"))
    print(f"爬蟲索引：{len(index)} 個網址", flush=True)

    rows = []
    with open(TSV, encoding="utf-8") as f:
        header = f.readline().rstrip("\n").split("\t")
        assert header[:5] == ["id", "url", "nlp_score", "yolo_score", "risk_score"], header
        for line in f:
            p = line.rstrip("\n").split("\t")
            if len(p) < 5 or not p[1]:
                continue
            rows.append((int(p[0]), p[1], int(p[2] or 0), int(p[3] or 0)))
    print(f"備份檔：{len(rows)} 筆分析結果", flush=True)

    conn = connect()
    with conn:
        cur = conn.cursor()

        # ---------- suspect_websites：文字內容 ----------
        n = 0
        for rec in jsonl("/out/text.jsonl"):
            url = rec["url"]
            cur.execute(
                "INSERT INTO suspect_websites "
                "(url, title, keywords_found, reported_by, created_at, html_content, images_data) "
                "VALUES (%s,%s,%s,%s,%s,%s,'[]') "
                "ON DUPLICATE KEY UPDATE html_content=VALUES(html_content), "
                "keywords_found=VALUES(keywords_found)",
                (url[:768], f"[{task_type_of(rec)}] 爬蟲自動通報"[:100],
                 ", ".join(rec.get("keywords") or [])[:500],
                 RESTORE_NOTE[:50], rec.get("timestamp"), rec.get("text_content") or ""),
            )
            n += 1
            if n % 200 == 0:
                conn.commit()
                print(f"  文字 {n}", flush=True)
        conn.commit()
        print(f"suspect_websites 文字：{n} 筆", flush=True)

        # ---------- suspect_websites：商品圖 ----------
        n = imgs = trimmed = 0
        for rec in jsonl("/out/images.jsonl"):
            url = rec["url"]
            pics = rec.get("images") or []
            blob = json.dumps(pics, ensure_ascii=False)
            while len(blob.encode("utf-8")) > MAX_IMAGES_BYTES and len(pics) > 1:
                pics = pics[:-1]
                blob = json.dumps(pics, ensure_ascii=False)
                trimmed += 1
            cur.execute(
                "INSERT INTO suspect_websites "
                "(url, title, keywords_found, reported_by, created_at, html_content, images_data) "
                "VALUES (%s,%s,'',%s,%s,'',%s) "
                "ON DUPLICATE KEY UPDATE images_data=VALUES(images_data)",
                (url[:768], f"[{task_type_of(rec)}] 爬蟲自動通報"[:100],
                 RESTORE_NOTE[:50], rec.get("timestamp"), blob),
            )
            n += 1
            imgs += len(pics)
            if n % 50 == 0:
                conn.commit()
                print(f"  圖片 {n}（累計 {imgs} 張）", flush=True)
        conn.commit()
        print(f"suspect_websites 圖片：{n} 筆／{imgs} 張"
              f"{f'（因單筆過大捨去 {trimmed} 張）' if trimmed else ''}", flush=True)

        # ---------- ai_analysis_results ----------
        # 保留原本的 id，備份檔裡的順序就是當初的發現順序。
        n = 0
        for rid, url, nlp, yolo in rows:
            rec = index.get(url, {})
            score, level = calculate_multimodal_risk_100_scale(nlp, yolo)
            kw = ", ".join(rec.get("keywords") or [])
            cur.execute(
                "INSERT INTO ai_analysis_results "
                "(id, url, yolo_details, yolo_score, nlp_details, nlp_score, "
                " risk_score, risk_level, task_source, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (rid, url[:768], "還原：原始影像明細未備份"[:500], yolo,
                 (kw or "還原：原始文字明細未備份")[:500], nlp,
                 score, level, f"[{task_type_of(rec)}] 爬蟲自動通報"[:100],
                 rec.get("timestamp")),
            )
            n += 1
            if n % 500 == 0:
                conn.commit()
                print(f"  分析結果 {n}/{len(rows)}", flush=True)
        conn.commit()
        print(f"ai_analysis_results：{n} 筆", flush=True)

        # ---------- 留一筆稽核紀錄 ----------
        cur.execute("SELECT user_id FROM users WHERE account='admin' LIMIT 1")
        who = cur.fetchone()
        cur.execute(
            "INSERT INTO audit_logs (user_id, action_type, action_timestamp, details) "
            "VALUES (%s, %s, NOW(), %s)",
            (who[0] if who else "SYSTEM", "資料還原",
             ("mysql volume 重建後還原：分析結果與蒐證資料取自 "
              "data/backups/risk_level_before_20260830_024341.tsv 與爬蟲 Record volume，"
              "風險分級依現行門檻重算。yolo/nlp 明細、YOLO 代表圖、稽核歷史、"
              "使用者帳號與白名單未備份，無法還原。")[:500]),
        )
        conn.commit()

        print("\n=== 還原後 ===")
        for t in ("users", "whitelist_websites", "suspect_websites",
                  "ai_analysis_results", "audit_logs"):
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            print(f"  {t:22} {cur.fetchone()[0]:>6}")

        cur.execute("SELECT risk_level, COUNT(*) FROM ai_analysis_results "
                    "GROUP BY risk_level ORDER BY 2 DESC")
        print("風險分級分布：")
        for lv, c in cur.fetchall():
            print(f"  {lv:26} {c:>6}")


if __name__ == "__main__":
    sys.exit(main())

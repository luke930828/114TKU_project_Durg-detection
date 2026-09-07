#!/usr/bin/env python3
"""
⚠️ 已被 tests/ 取代，保留是為了 make smoke 與 make up-staged 還在用。

    新的整合測試：make test          （tests/integration/test_03_pipeline.py 涵蓋這裡全部內容再加上更多）
    資安測試：    make test-security

已知問題：下面第 4 步送的是 X-Token: <帳號名>，那是專案改用 JWT 之前的舊驗證方式，
現在必定 401。因為預設不帶 --admin-account 會跳過這步，所以一直沒人發現。
這行刻意不修——它本身就是「測試沒人在跑」的證據，由 tests/ 那套涵蓋。

整合冒煙測試 —— 單機 compose up 之後跑這個，確認四個模組串起來是通的。

它不需要真的去爬網站或跑推論，而是直接模擬各模組會送出的 payload，
驗證「後端有沒有正確把 YOLO 分數和 NLP 分數統整成同一筆 risk_score」。

這正是單機整合測試唯一真正該驗的東西：模組之間的介面契約。

用法：
    python scripts/smoke_test.py
    python scripts/smoke_test.py --base-url http://node-backend.xxx.ts.net:8000
    python scripts/smoke_test.py --admin-account super_admin   # 要驗報表端點才需要
"""
import argparse
import sys
import time
import uuid

import requests

PASS = "\033[32m✔\033[0m"
FAIL = "\033[31m✘\033[0m"

failures = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    print(f"  {PASS if ok else FAIL} {name}" + (f"  → {detail}" if detail else ""))
    if not ok:
        failures.append(name)
    return ok


def wait_for_backend(base: str, timeout: int = 120) -> bool:
    print(f"\n等待後端就緒（最多 {timeout} 秒）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if requests.get(f"{base}/", timeout=3).status_code == 200:
                print(f"  {PASS} 後端已就緒")
                return True
        except requests.RequestException:
            pass
        print("  .", end="", flush=True)
        time.sleep(3)
    print(f"\n  {FAIL} 後端一直沒起來。跑 docker compose logs backend 看看。")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--admin-account", default=None,
                    help="有 admin 帳號的話會一併驗證報表端點")
    ap.add_argument("--timeout", type=int, default=30)
    args = ap.parse_args()
    base = args.base_url.rstrip("/")

    if not wait_for_backend(base):
        return 1

    # 每次用不同網址，避免撞到上次測試留下的紀錄
    test_url = f"https://smoke-test.invalid/{uuid.uuid4().hex[:8]}"
    print(f"\n測試網址：{test_url}")

    # ---------- 1. 爬蟲送原始資料 ----------
    print("\n[1/4] 模擬爬蟲通報")
    try:
        r = requests.post(
            f"{base}/api/crawler/report/",
            json={
                "task_type": "smoke_test",
                "timestamp": "2026-01-01T00:00:00",
                "keywords": ["測試關鍵字"],
                "url": test_url,
                "text_content": "這是整合測試用的假內容，不含任何真實資料。",
                "product_images_b64": [],
            },
            timeout=args.timeout,
        )
        check("爬蟲端點接受 payload", r.status_code == 200, f"HTTP {r.status_code}")
    except requests.RequestException as e:
        check("爬蟲端點可連線", False, str(e))
        return 1

    # ---------- 2. NLP 回報 ----------
    print("\n[2/4] 模擬 NLP 引擎回報（分數 60）")
    r = requests.post(
        f"{base}/api/nlp/report/",
        json={"url": test_url, "risk_score": 60, "nlp_keywords": ["測試詞A", "測試詞B"]},
        timeout=args.timeout,
    )
    check("NLP 端點接受 payload", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------- 3. YOLO 回報 ----------
    print("\n[3/4] 模擬 YOLO 引擎回報（分數 80）")
    r = requests.post(
        f"{base}/api/ai_result/report/",
        json={
            "url": test_url,
            "risk_score": 80,
            "yolo_objects": ["test_object"],
            "class_metadata": {"test_object": 1},
            "representative_image_detections": [
                {"class": "test_object", "confidence": 0.9}
            ],
        },
        timeout=args.timeout,
    )
    check("YOLO 端點接受 payload", r.status_code == 200, f"HTTP {r.status_code}")

    # ---------- 4. 確認統整結果 ----------
    # 這步是整個測試的重點：兩個引擎各自獨立送分數進來，
    # 後端有沒有正確合併成同一筆？先到的那筆有沒有被後到的覆蓋掉？
    print("\n[4/4] 驗證多模態統整結果")
    if not args.admin_account:
        print("  （沒給 --admin-account，跳過報表端點驗證）")
        print("  提示：這步才真正驗到「YOLO + NLP 有沒有合併成同一筆」，建議補上。")
    else:
        r = requests.get(
            f"{base}/api/crawler/report/",
            headers={"X-Token": args.admin_account},
            timeout=args.timeout,
        )
        if check("報表端點可讀取", r.status_code == 200, f"HTTP {r.status_code}"):
            rows = [x for x in r.json().get("data", []) if x.get("url") == test_url]
            if check("找得到這筆測試紀錄", len(rows) == 1, f"找到 {len(rows)} 筆"):
                row = rows[0]
                check("YOLO 分數有寫入", row.get("yolo_score") == 80,
                      f"實際 {row.get('yolo_score')}")
                check("NLP 分數有寫入", row.get("nlp_score") == 60,
                      f"實際 {row.get('nlp_score')}")
                check("兩者已統整成綜合分數",
                      isinstance(row.get("risk_score"), int) and row["risk_score"] > 0,
                      f"risk_score={row.get('risk_score')}, level={row.get('risk_level')}")
                # 後到的 YOLO 不該把先到的 NLP 洗掉
                check("後到的引擎沒有覆蓋掉先到的",
                      row.get("nlp_score") == 60 and row.get("yolo_score") == 80)

    # ---------- 結果 ----------
    print("\n" + "=" * 44)
    if failures:
        print(f"{FAIL} {len(failures)} 項失敗：")
        for f in failures:
            print(f"    - {f}")
        print("\n看詳細錯誤：docker compose logs backend")
        return 1
    print(f"{PASS} 全部通過 —— 模組間的介面契約沒問題")
    print("=" * 44)
    return 0


if __name__ == "__main__":
    sys.exit(main())

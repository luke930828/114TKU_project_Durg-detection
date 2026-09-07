"""把 suspect_websites.images_data 裡的 base64 圖片搬成檔案，欄位只留路徑。

理由見 app/image_store.py。

先把檔案寫好、確認讀得回來、才改欄位——中途中斷不會壞：已搬好的是路徑、
還沒搬的仍是 base64，image_store.load_field() 兩種都吃。重跑會跳過已完成的。

用法
────
    python images_to_files.py --dry-run        只統計，不寫任何東西
    python images_to_files.py --limit 20       先搬 20 筆試
    python images_to_files.py                  全部搬
    python images_to_files.py --verify         只檢查已搬的檔案在不在
"""
import argparse
import json
import sys
import time

sys.path.insert(0, "/app")

import database
import image_store

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=0, help="只處理前幾筆，0 = 全部")
ap.add_argument("--batch", type=int, default=50, help="每幾筆 commit 一次")
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--verify", action="store_true",
                help="不搬東西，只檢查已搬的紀錄檔案是否都在")
args = ap.parse_args()

db = database.SessionLocal()
S = database.SuspectWebsite


def classify(images_data):
    """這筆是還沒搬（base64）、已經搬好（路徑），還是空的？"""
    try:
        items = json.loads(images_data or "[]")
    except (TypeError, ValueError):
        return "壞掉的 JSON", []
    items = [i for i in items if isinstance(i, str) and i]
    if not items:
        return "沒有圖", []
    if all(image_store.is_stored_path(i) for i in items):
        return "已搬好", items
    if any(image_store.is_stored_path(i) for i in items):
        return "混合", items
    return "還沒搬", items


# ---------------------------------------------------------------- 盤點
ids = [r.id for r in db.query(S.id).order_by(S.id).all()]
if args.limit:
    ids = ids[: args.limit]

print(f"總共 {len(ids)} 筆網頁紀錄")

if args.verify:
    missing = checked = 0
    for sid in ids:
        row = db.query(S.id, S.images_data).filter(S.id == sid).first()
        state, items = classify(row.images_data)
        if state != "已搬好":
            continue
        for rel in items:
            checked += 1
            if not (image_store.IMAGE_ROOT / rel).exists():
                missing += 1
                print(f"  遺失 id={sid} {rel}")
    print(f"檢查 {checked} 個檔案，遺失 {missing} 個")
    db.close()
    sys.exit(1 if missing else 0)

if args.dry_run:
    from collections import Counter
    stat = Counter()
    entries = 0
    total_chars = 0
    for sid in ids:
        row = db.query(S.id, S.images_data).filter(S.id == sid).first()
        state, items = classify(row.images_data)
        stat[state] += 1
        if state in ("還沒搬", "混合"):
            entries += len(items)
            total_chars += len(row.images_data or "")
    print()
    for k, v in stat.most_common():
        print(f"  {k:<12} {v:>6} 筆")
    print(f"\n要搬的圖片共 {entries} 張，base64 合計 {total_chars/1024/1024:.0f} MB")
    print(f"存成檔案後約 {total_chars*0.75/1024/1024:.0f} MB（去掉 base64 的 33% 膨脹，"
          f"重複的圖還會再合併）")
    db.close()
    sys.exit(0)

# ---------------------------------------------------------------- 搬遷
moved = skipped = failed = 0
freed_chars = 0
t0 = time.time()

for n, sid in enumerate(ids, 1):
    row = db.query(S).filter(S.id == sid).first()
    if row is None:
        continue
    state, items = classify(row.images_data)
    if state in ("已搬好", "沒有圖", "壞掉的 JSON"):
        skipped += 1
    else:
        before = len(row.images_data or "")
        try:
            paths = []
            for item in items:
                # 混合狀態：已經是路徑的就原樣留著，不要重新解碼
                paths.append(item if image_store.is_stored_path(item)
                             else image_store.save_base64(item))

            # 改欄位之前先確認檔案真的讀得回來。
            # 先清欄位再發現檔案沒寫成功的話，那些圖就永遠找不回來了。
            for rel in paths:
                if not (image_store.IMAGE_ROOT / rel).exists():
                    raise OSError(f"檔案沒寫出來：{rel}")

            row.images_data = json.dumps(paths, ensure_ascii=False)
            moved += 1
            freed_chars += before - len(row.images_data)
        except Exception as err:
            db.rollback()
            failed += 1
            print(f"  id={sid} 搬遷失敗，欄位保持原樣：{err}")
            continue

    if n % args.batch == 0:
        db.commit()
        rate = n / max(time.time() - t0, 0.001)
        left = (len(ids) - n) / max(rate, 0.001)
        print(f"  進度 {n}/{len(ids)}　已搬 {moved}　跳過 {skipped}　失敗 {failed}"
              f"　釋出 {freed_chars/1024/1024:.0f} MB　預估剩 {left/60:.0f} 分",
              flush=True)

db.commit()
db.close()
print(f"\n完成：搬遷 {moved} 筆、跳過 {skipped} 筆、失敗 {failed} 筆")
print(f"欄位共釋出約 {freed_chars/1024/1024:.0f} MB")
print("注意：MySQL 不會自動把空間還給作業系統，要跑 OPTIMIZE TABLE 才會真的縮小。")

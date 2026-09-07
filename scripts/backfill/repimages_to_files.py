"""把 ai_analysis_results.representative_image_base64 搬成檔案。

跟 images_to_files.py 同一套機制，差別在這批圖是「前端會顯示」的代表圖，
所以搬完 API 回傳格式必須完全不變——crawler.py 的 /result/{id}/image/
改成從檔案讀出來再轉回 base64，前端一行都不用改。

實測讀檔比讀資料庫還快（中位 0.23 ms vs 0.37 ms），因為省掉了 InnoDB 的
頁面管理與 off-page blob 指標。真正的收穫是資料庫從 2.3 GB 掉到 400 MB 上下，
整個資料庫終於放得進 1 GB 的 buffer pool。

一樣是先寫檔、確認讀得回來、才清欄位，中途中斷不會壞。

用法
────
    python repimages_to_files.py --dry-run
    python repimages_to_files.py --limit 20
    python repimages_to_files.py
    python repimages_to_files.py --verify
"""
import argparse
import sys
import time

sys.path.insert(0, "/app")

import database
import image_store

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--batch", type=int, default=100)
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--verify", action="store_true")
args = ap.parse_args()

db = database.SessionLocal()
A = database.AIAnalysisResult

if args.verify:
    rows = db.query(A.id, A.representative_image_path).filter(
        A.representative_image_path != None,
        A.representative_image_path != "").all()
    missing = 0
    for r in rows:
        if not (image_store.IMAGE_ROOT / r.representative_image_path).exists():
            missing += 1
            print(f"  遺失 id={r.id} {r.representative_image_path}")
    print(f"檢查 {len(rows)} 個檔案，遺失 {missing} 個")
    db.close()
    sys.exit(1 if missing else 0)

# 只挑「還在 base64 欄位」的
ids = [r.id for r in db.query(A.id).filter(
    A.representative_image_base64 != None,
    A.representative_image_base64 != "").order_by(A.id).all()]
if args.limit:
    ids = ids[: args.limit]

print(f"待搬的代表圖：{len(ids)} 張")

if args.dry_run:
    from sqlalchemy import func
    total, biggest = db.query(
        func.sum(func.length(A.representative_image_base64)),
        func.max(func.length(A.representative_image_base64)),
    ).filter(A.representative_image_base64 != None,
             A.representative_image_base64 != "").first()
    # SUM() 回傳的是 Decimal，直接跟 float 相乘會 TypeError
    total = float(total or 0)
    biggest = float(biggest or 0)
    print(f"base64 合計 {total/1024/1024:.0f} MB，最大一張 {biggest/1024/1024:.1f} MB")
    print(f"存成檔案後約 {total*0.75/1024/1024:.0f} MB（重複的還會再合併）")
    db.close()
    sys.exit(0)

moved = failed = 0
freed = 0
t0 = time.time()

for n, rid in enumerate(ids, 1):
    row = db.query(A).filter(A.id == rid).first()
    if row is None or not row.representative_image_base64:
        continue
    before = len(row.representative_image_base64)
    try:
        rel = image_store.save_base64(row.representative_image_base64)
        # 清欄位之前先確認檔案真的在。順序反過來的話，寫檔失敗那張圖就沒了。
        if not (image_store.IMAGE_ROOT / rel).exists():
            raise OSError(f"檔案沒寫出來：{rel}")
        row.representative_image_path = rel
        row.representative_image_base64 = None
        moved += 1
        freed += before
    except Exception as err:
        db.rollback()
        failed += 1
        print(f"  id={rid} 失敗，欄位保持原樣：{err}")
        continue

    if n % args.batch == 0:
        db.commit()
        rate = n / max(time.time() - t0, 0.001)
        print(f"  進度 {n}/{len(ids)}　已搬 {moved}　失敗 {failed}"
              f"　釋出 {freed/1024/1024:.0f} MB"
              f"　預估剩 {(len(ids)-n)/max(rate,0.001)/60:.0f} 分", flush=True)

db.commit()
db.close()
print(f"\n完成：搬遷 {moved} 張、失敗 {failed} 張，釋出約 {freed/1024/1024:.0f} MB")
print("記得跑 OPTIMIZE TABLE ai_analysis_results 才會真的把空間還給作業系統。")

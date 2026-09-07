"""把卡在「影像分析中...」的網址，用原本存下來的圖片重新派給 YOLO。

原始圖片還在 suspect_websites.images_data —— 爬蟲送來時後端就先落庫了，
派給 YOLO 只是另一條路徑。所以 YOLO 死掉那段時間掉的只是「分析結果」，
不是「證據」，可以補跑。

用法：
    python backfill.py --dry-run          只看會處理哪些，不送
    python backfill.py --limit 1          先跑一筆試
    python backfill.py --pace 6           每筆間隔 6 秒（控制 YOLO 的負載）
"""
import argparse, json, os, sys, time
sys.path.insert(0, "/app")
import requests
import database

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--pace", type=float, default=6.0, help="每筆之間睡幾秒")
ap.add_argument("--dry-run", action="store_true")
ap.add_argument("--min-age-min", type=int, default=10,
                help="只補這麼久以前的，避免動到還在處理中的")
args = ap.parse_args()

YOLO_API_URL = os.environ["YOLO_API_URL"]

db = database.SessionLocal()
rows = db.query(database.AIAnalysisResult, database.SuspectWebsite).join(
    database.SuspectWebsite,
    database.SuspectWebsite.url == database.AIAnalysisResult.url,
).filter(
    database.AIAnalysisResult.yolo_details == "影像分析中...",
    database.AIAnalysisResult.created_at < database.func.now() - database.text(
        f"INTERVAL {args.min_age_min} MINUTE"),
).all()

targets = []
for ai, sus in rows:
    try:
        images = json.loads(sus.images_data or "[]")
    except Exception:
        images = []
    images = [i for i in images if isinstance(i, str) and i]
    if images:
        targets.append((ai.url, images))
db.close()

if args.limit:
    targets = targets[: args.limit]

total_imgs = sum(len(i) for _, i in targets)
print(f"要補跑 {len(targets)} 個網址，共 {total_imgs} 張圖")
print(f"預估耗時 約 {len(targets) * args.pace / 60:.0f} 分鐘（每筆間隔 {args.pace}s）")
if args.dry_run:
    for url, imgs in targets[:5]:
        print(f"   {len(imgs):2d} 張  {url[:75]}")
    sys.exit(0)

def post_with_retry(url, payload, attempts=4):
    """連線失敗就退避重試。

    補跑動輒跑幾十分鐘，期間服務被重啟（有人 docker compose up、容器被
    OOM 砍掉、Docker Desktop 重開）是常態。沒有重試的話，一次短暫的中斷就
    讓整批停在半路——2026-09-04 實測連續兩次都是這樣，一次停在 60/395。

    只重試連線層的錯誤：連不上、DNS 解析不到、逾時。HTTP 回應碼直接回傳，
    那是對方收到了但不接受，重送結果一樣。
    """
    delay = 3
    for i in range(attempts):
        try:
            return requests.post(url, json=payload, timeout=15)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            if i == attempts - 1:
                print(f"   連線失敗 {attempts} 次，放棄這一張：{e.__class__.__name__}")
                return None
            print(f"   [重試 {i + 1}/{attempts - 1}] 連不上，{delay}s 後再試"
                  f"（服務可能正在重啟）", flush=True)
            time.sleep(delay)
            delay *= 2
    return None


ok = fail = 0
for n, (url, images) in enumerate(targets, 1):
    # task_id 要用新的批次代號，不然會跟舊批次的殘留狀態混在一起
    batch = f"bf{int(time.time())%100000:05d}{n:04d}"
    sent = 0
    for idx, b64 in enumerate(images):
        r = post_with_retry(YOLO_API_URL, {
            "task_id": f"{batch}_{idx}", "url": url,
            "image_base64": b64, "total_images": len(images), "priority": 0,
        })
        if r is not None and r.status_code == 200:
            sent += 1
    if sent == len(images):
        ok += 1
    else:
        fail += 1
    if n % 20 == 0 or n == len(targets):
        print(f"  進度 {n}/{len(targets)}  成功 {ok}  失敗 {fail}", flush=True)
    time.sleep(args.pace)

print(f"\n派發完成：成功 {ok}、失敗 {fail}。YOLO 會在背景陸續回報。")

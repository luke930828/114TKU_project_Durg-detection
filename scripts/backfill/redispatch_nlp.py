"""把卡在「文字分析中...」的網址，用原本存下來的網頁文字重新送 NLP。

原文還在 suspect_websites.html_content —— 爬蟲送來時後端就先落庫了，
派給 NLP 是另一條路徑。所以 NLP 連不上那段時間掉的只是「分析結果」，
不是「證據」，可以補跑。
"""
import argparse, os, sys, time
sys.path.insert(0, "/app")
import requests, database

ap = argparse.ArgumentParser()
ap.add_argument("--limit", type=int, default=0)
ap.add_argument("--pace", type=float, default=1.5)
ap.add_argument("--dry-run", action="store_true")
args = ap.parse_args()

NLP_PREDICT_URL = os.environ["NLP_PREDICT_URL"]

db = database.SessionLocal()
rows = db.query(database.AIAnalysisResult, database.SuspectWebsite).join(
    database.SuspectWebsite,
    database.SuspectWebsite.url == database.AIAnalysisResult.url,
).filter(database.AIAnalysisResult.nlp_details == "文字分析中...").all()
targets = [(a.url, s.html_content) for a, s in rows if (s.html_content or "").strip()]
db.close()

if args.limit:
    targets = targets[: args.limit]
print(f"要補跑 {len(targets)} 個網址，預估 {len(targets)*args.pace/60:.0f} 分鐘")
if args.dry_run:
    for u, t in targets[:5]:
        print(f"   {len(t):6d} 字  {u[:70]}")
    sys.exit(0)

ok = fail = 0
for n, (url, text) in enumerate(targets, 1):
    try:
        # report 用預設 True：讓 NLP 照原本的路徑自己回寫，行為跟第一次派發一樣
        r = requests.post(NLP_PREDICT_URL, json={"url": url, "text": text}, timeout=40)
        ok += 1 if r.status_code == 200 else 0
        fail += 0 if r.status_code == 200 else 1
    except Exception as e:
        fail += 1
        print(f"   失敗 {url[:55]}：{e.__class__.__name__}")
    if n % 25 == 0 or n == len(targets):
        print(f"  進度 {n}/{len(targets)}  成功 {ok}  失敗 {fail}", flush=True)
    time.sleep(args.pace)
print(f"\n完成：成功 {ok}、失敗 {fail}")

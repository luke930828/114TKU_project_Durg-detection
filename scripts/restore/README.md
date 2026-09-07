# 資料庫還原

2026-08-31 `deploy_mysql-data`、`deploy_crawler-record`、`deploy_hf-cache`
三個 volume 同時消失（原因不明，`docker events` 沒保留、Docker Desktop
沒開自動清理、磁碟也沒滿）。這幾支腳本是那次把資料救回來用的，留著以防再發生。

**先看有沒有 `data/backups/*.sql.gz`。** 有的話直接還原那個就好，不用跑下面這些：

```bash
set -a; . ./.env.local; set +a
zcat data/backups/drug_prevention_db_YYYYMMDD_HHMM.sql.gz \
  | docker exec -i -e P="$DB_PASSWORD" -e U="$DB_USER" -e D="$DB_NAME" deploy-mysql-1 \
      sh -c 'exec mysql -h127.0.0.1 --protocol=TCP -u"$U" -p"$P" --default-character-set=utf8mb4 "$D"'
```

備份用 `make backup`。**定期跑，不要只靠 Docker volume。**

---

## 沒有 dump 時的完整還原流程

四支腳本都在容器裡跑，因為要用 compose 的內部網路連 mysql、
還要 import 後端的 `utils.py`（風險分級只有那一份，不在這裡重寫第二套）。

### 0. 資料來源

| 來源 | 內容 |
|---|---|
| `data/backups/risk_level_before_*.tsv` | `ai_analysis_results` 的網址與分數 |
| `data/backups/crawl_records_*/` | 從爬蟲 volume 萃取出的網頁文字、商品圖、造訪紀錄 |
| 爬蟲 volume `deploy_crawler-record` | 原始檔（`nlp_text.json`、`images.json`、`visited.txt`）|

### 1. `extract.py` — 從爬蟲 volume 萃取

只有爬蟲 volume 還在時才需要跑。輸出 `text.jsonl`、`images.jsonl`、`index.json`。

```bash
docker run --rm --memory=1g \
  -v deploy_crawler-record:/r:ro -v "$PWD/data/backups/crawl_records_$(date +%Y%m%d)":/out \
  python:3.12-slim python /out/extract.py
```

刻意輸出 JSONL 而不是一個大 JSON：`images.json` 是 1 GB，
整份 `json.load()` 要 3~4 GB 記憶體——那正是 INFRA-02 把機器搞到 OOM 的寫法。
第一版就是這樣寫的，下游直接被 OOM killer 殺掉。

### 2. `restore.py` — 灌回 mysql

```bash
set -a; . ./.env.local; set +a
docker run --rm --network deploy_default --memory=1g \
  -v "$PWD/data/backups/crawl_records_YYYYMMDD":/out \
  -v "$PWD/modules/backend/app":/app:ro \
  -e DB_USER -e DB_PASSWORD -e DB_NAME \
  python:3.12-slim sh -c 'pip install -q pymysql cryptography requests && python /out/restore.py'
```

TSV 要先複製進 `/out`。風險分級用現行門檻重算，不沿用 TSV 裡那欄
（那是舊規則算的，中文還是亂碼——當初匯出沒指定 utf8mb4）。

### 3. `fixsource.py` — 補回來源與發現時間

`task_source` 少了 `[automated_24h]` 的話，24 小時清單（系統主畫面）會看不到那筆。
現存的 json 只涵蓋最近幾輪爬取，其餘要靠 `visited.txt` 補。

```bash
docker run --rm --network deploy_default \
  -v "$PWD/data/backups/crawl_records_YYYYMMDD":/out \
  -e DB_USER -e DB_PASSWORD -e DB_NAME \
  python:3.12-slim sh -c 'pip install -q pymysql cryptography && python /out/fixsource.py'
```

### 4. `replay_yolo.py` — 重新產生代表圖

前端只讀 `ai_analysis_results.representative_image_base64`，那是 YOLO 挑出來的，
爬蟲記錄檔裡沒有，只能重跑模型產生。

**只起 yolo，不要用 `make full`**——爬蟲容器一啟動就自動開爬
（`modules/crawler/app/main.py:69` 的 lifespan），INFRA-02 沒修之前會再 OOM 一次。

```bash
docker compose -f deploy/docker-compose.yml --env-file .env.local --profile full up -d yolo
docker run --rm --network deploy_default \
  -v "$PWD/data/backups/crawl_records_YYYYMMDD":/out \
  -e DB_USER -e DB_PASSWORD -e DB_NAME \
  python:3.12-slim sh -c 'pip install -q pymysql cryptography requests && python /out/replay_yolo.py'
```

`REPLAY_DELAY`（預設 0.25 秒）不要拿掉：YOLO 收到請求就丟背景任務、沒有佇列上限，
一次灌幾千張等於把幾百 MB 的 base64 同時堆在記憶體裡，而且第一次 `fuse()`
會被併發撞出 `'Conv' object has no attribute 'bn'`。

代表圖只在偵測信心度 ≥ 0.5 時才會產生（`modules/yolo/app/main.py:155`），
所以不是每一筆都會有圖——2026-08-31 那次是 1132 筆有商品圖的網址裡 805 筆拿到圖。

腳本會跳過已經有代表圖的，中斷後直接重跑即可。

---

## 還原不了的東西

使用者帳號、白名單、稽核日誌歷史，以及整頁截圖
（`screenshot_b64` / `full_screenshot_base64`——後端本來就沒存這兩個欄位）。
爬蟲的排程狀態 `monitor_state.db` / `queue.db` 也沒有，下次啟動會從頭開始。

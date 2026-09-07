# 補跑腳本

AI 引擎連不上的那段時間，分析結果會靜靜掉光，那些網址永遠停在
「影像分析中...」或「文字分析中...」。這兩支腳本把它們補回來。

**掉的只是「分析結果」，不是「證據」**——爬蟲送來時後端就先把原始網頁文字
與圖片落庫了（`suspect_websites.html_content` / `images_data`），派給 AI 引擎
是另一條路徑。所以拿原始資料重新派一次就補得回來。

## 用法

腳本要在 backend 容器裡跑（需要 `database` 模組與服務網路）：

```bash
COMPOSE="docker compose -f deploy/docker-compose.yml"

# 先看會處理哪些，不送
$COMPOSE cp scripts/backfill/redispatch_yolo.py backend:/tmp/
$COMPOSE exec -T backend python -u /tmp/redispatch_yolo.py --dry-run

# 真的跑（--pace 是每筆之間睡幾秒，用來控制引擎的負載）
$COMPOSE exec -T backend python -u /tmp/redispatch_yolo.py --pace 6
$COMPOSE exec -T backend python -u /tmp/redispatch_nlp.py  --pace 1.5
```

## 挑 pace 的方法

看引擎的處理能力與爬蟲的產出，兩者相加不要超過容量：

* YOLO + GPU OCR 約 **153 張/分**（1920px，最壞情況）
* 爬蟲產出約 **90 張/分**
* 所以補跑最多再吃 60 張/分 → 一頁約 4 張圖的話，`--pace 4` 以上都安全

跑太快的下場是引擎的請求佇列在記憶體裡積壓，撞到容器上限被 OOM 砍掉，
手上沒做完的批次全部消失——那正是一開始要補跑的原因。

## 注意

* 不要用 `nohup ... &` 掛在 shell 上，shell 收掉程序就跟著死（實測停在 60/510）。
  用 `docker compose exec` 前景跑，或丟進 tmux / screen。
* 跑之前先確認引擎是活的（`make verify`），不然只是把資料再掉一次。
* **補跑期間不要重建服務。** 尤其是這行：

      docker compose up -d --force-recreate yolo      # ⚠️ 會連 backend 一起重建

  compose 會連同相依服務一起處理，而 yolo 是 depends_on: backend——腳本正是
  跑在 backend 容器裡，exec 連線會當場斷掉。要只動一個服務就加 `--no-deps`：

      docker compose up -d --force-recreate --no-deps yolo

  腳本本身已經有重試，短暫中斷撐得過去，但容器被「重建」是換了一個新容器，
  裡面的行程一定會死，重試救不回來。

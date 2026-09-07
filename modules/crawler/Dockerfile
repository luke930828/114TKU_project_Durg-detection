# ============================================================
# crawler 模組 —— Playwright + Playwright-stealth 的爬蟲服務
#
# 用官方 Playwright image：已經內建對應版本的 Chromium + 系統依賴
# （libgl、字型等），不用自己在 slim image 上補一堆 apt 套件。
# tag 版本要跟 requirements.txt 的 playwright==1.42.0 對齊，
# 版本不一致 pip 會裝新版 playwright 但瀏覽器二進位還是舊的，跑不動。
# ============================================================
FROM mcr.microsoft.com/playwright/python:v1.42.0-jammy

WORKDIR /app

# tzdata 一定要配 DEBIAN_FRONTEND=noninteractive：它的 postinst 會跳出
# 「Please select the geographic area」的互動選單，在 build 裡沒有人回答，
# 整個 build 就無聲地卡在那裡——看起來像編譯很慢，其實是在等輸入。
RUN DEBIAN_FRONTEND=noninteractive apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ /app/

# config.json 裡的 record_paths 預設寫到 Record/，模組重啟資料就不見，
# 建議在 docker-compose 幫這個服務掛一個 volume 到 /app/Record

EXPOSE 8001

# timeout 給 15s 不是 5s：這個容器在跑 Chromium 抓網頁，滿載時 CPU 被吃光，
# /health 本身雖然只回一行字，curl 卻排不到 CPU，5 秒常常回不來。
# 2026-09-03 實際看到的情形是 log 裡每次 /health 都 200 OK，
# healthcheck 卻一直 "exceeded timeout (5s)"，容器被標成 unhealthy——
# 服務其實好好的，被誤判的是健康檢查自己。
#
# retries 也從 3 拉到 5：偶爾一次慢不代表死了，連續五次（約 75 秒）
# 都回不來才算數。
HEALTHCHECK --interval=15s --timeout=15s --start-period=20s --retries=5 \
    CMD curl -f http://localhost:8001/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8001"]

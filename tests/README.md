# 整合測試與資安稽核

不進版控（`.gitignore` 的「內部用」區塊）。

## 快速開始

```bash
make test              # 起環境 + 跑全部 + 產生報告
cat tests/report/SECURITY_REPORT.md
```

第一次會 build 前端 image，約 2-3 分鐘；之後幾秒。

## 指令

| 指令 | 用途 |
|---|---|
| `make test` | 起環境、跑全部測試、產生報告 |
| `make test-integration` | 只跑模組間介面契約（**應該全綠**） |
| `make test-security` | 只跑資安，印出完整待修清單 |
| `make test-up` / `make test-down` | 只起 / 只收環境 |
| `make test-full` | 用真實 NLP/YOLO 跑（需要 GPU 與 `models/best.pt`） |

## 怎麼讀結果

```
integration/  ......................    ← 全部要綠。紅的代表模組間介面真的斷了
security/     XXXXXXxXX..XXXXX          ← X 是 XFAIL = 漏洞還在（預期中）
```

| 符號 | 意思 |
|---|---|
| `.` | 通過 |
| `X` | XFAIL — 已知漏洞，還沒修 |
| `x` | XPASS — **修好了**，報告會自動標成 ✅ |
| `F` | 真的壞了，要看 |

資安測試斷言的是「**正確的行為**」，所以修好之後不用回來改測試 —— 它會自己從
XFAIL 變成 XPASS。這是刻意的設計，讓報告可以自我維護。

## 環境長什麼樣

`tests/docker-compose.test.yml` 疊在 `deploy/docker-compose.yml` 上：

- **真的**：mysql、backend、frontend + nginx → 驗到真的跨容器 HTTP、真的 MySQL、真的 proxy
- **stub**：nlp、yolo、crawler（`tests/stubs/`）→ 用網路別名頂替

為什麼要 stub：真的 NLP 要下載約 1GB 模型，真的 YOLO 要 NVIDIA GPU 與權重，
而且兩者推論分數不固定，沒辦法斷言。stub 回固定分數
（NLP 0.6 → 後端算成 60，YOLO 80 → 綜合 68），讓「後端有沒有正確合併」變成可驗證的事。

stub 額外提供 `/__stub/calls`，測試可以檢查後端究竟派發了什麼出去 ——
SSRF 測試就是靠這個確認惡意網址有沒有真的被轉給爬蟲。

## 檔案

```
vulns.py          發現清單的單一事實來源（測試與報告共用）
conftest.py       fixtures + known_vuln + 報告產生器
helpers.py        輪詢、查詢結果等共用工具
stubs/            假的 nlp / yolo / crawler
integration/      模組間介面契約
security/         資安，用 @known_vuln 標記已知漏洞
report/           自動產生，每次跑覆寫
```

## 新增一項發現

1. 在 `vulns.py` 加條目（ID、嚴重度、標題、影響、修法）
2. 寫測試，斷言**正確**的行為，掛上 `@known_vuln("SEC-xx")`
3. 跑一次，報告自動長出那一節

修好之後**不要**從 `vulns.py` 刪掉 —— 留著才看得到它從 🔴 變成 ✅。

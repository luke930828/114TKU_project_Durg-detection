# ============================================================
# 四種情境 = 四個指令
#
# Windows 沒有 make 的話，直接看每個目標底下那行指令複製來用就好，
# 或裝 Git Bash / WSL。
# ============================================================

# CI 沒有 .env.local（那裡面是真的密碼，不該進版控），
# 所以讓它可以覆寫：make test ENV_FILE=.env.ci
ENV_FILE     ?= .env.local
COMPOSE      := docker compose -f deploy/docker-compose.yml
DEV_OVERRIDE := -f deploy/docker-compose.dev.yml

.DEFAULT_GOAL := help
.PHONY: help dev local full demo tailscale logs ps stop clean rebuild check \
	smoke up-staged models test test-up test-down test-security test-full \
	backup restore-help

help: ## 顯示這份說明
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

# ---------- 情境一：寫程式 ----------
dev: ## 開發模式：程式碼掛載 + 熱重載，存檔就生效
	$(COMPOSE) $(DEV_OVERRIDE) --env-file $(ENV_FILE) up -d
	@echo ""
	@echo "  後端 API  → http://localhost:8000/docs"
	@echo "  前端畫面  → http://localhost:5173"
	@echo "  資料庫    → localhost:3306"
	@echo ""
	@echo "  改 .py 或 .tsx 存檔就會自動重載，不用重跑這個指令。"

# ---------- 情境二：單機整合 ----------
local: ## 單機：後端 + DB + 前端（爬蟲/YOLO/NLP 還沒好時用這個）
	$(COMPOSE) --env-file $(ENV_FILE) up -d --build

full: ## 單機：全部六個服務一起跑
	$(COMPOSE) --env-file $(ENV_FILE) --profile full up -d --build

# ---------- 情境三：跨機測試 ----------
tailscale: ## 跨機：只跑本機負責的模組，位址指向 tailnet
	$(COMPOSE) --env-file .env.tailscale up -d --build
	@echo ""
	@echo "  本機 tailnet 位址："
	@tailscale ip -4 2>/dev/null || echo "  （tailscale 指令找不到，確認有裝且已登入）"

# ---------- 情境四：展示 ----------
demo: ## 展示日：前一天先跑這個預熱，現場才不用等下載
	$(COMPOSE) --env-file $(ENV_FILE) --profile full pull
	$(COMPOSE) --env-file $(ENV_FILE) --profile full build
	@echo ""
	@echo "  image 都在本機了。展示現場執行 make full 即可，"
	@echo "     就算沒網路也能起來。"

# ---------- 日常 ----------
logs: ## 追蹤所有服務的即時紀錄
	$(COMPOSE) logs -f --tail=100

ps: ## 看每個服務的健康狀態
	$(COMPOSE) ps

stop: ## 停止（資料保留）
	$(COMPOSE) --profile full down

recreate: ## Docker Desktop 或 WSL 重開過就跑這個（重建容器，讓掛載重新解析）
	@echo "重新建立所有容器（不是 restart）。"
	@echo "restart 會沿用舊的掛載解析結果——容器會「起來但少東西」。"
	@echo ""
	$(COMPOSE) --env-file $(ENV_FILE) --profile full up -d --force-recreate
	@echo ""
	@# 要等服務真的就緒再檢查。YOLO 載入權重加 EasyOCR 要幾十秒，
	@# 容器剛 Started 就去問 model_loaded 一定是 false——那是啟動中，不是壞掉，
	@# 但檢查結果會顯示成紅色的 ❌，看的人會以為 recreate 沒有用。
	@echo "→ 等服務就緒（YOLO 載入模型要幾十秒）"
	@for i in $$(seq 1 30); do \
	   unhealthy=$$($(COMPOSE) ps --format '{{.Service}} {{.Status}}' 2>/dev/null \
	     | grep -c "health: starting" || true); \
	   [ "$$unhealthy" = "0" ] && break; \
	   printf "."; sleep 10; \
	 done; echo ""
	@echo ""
	@$(MAKE) --no-print-directory verify

verify: ## 檢查服務是不是「起來了但少東西」（掛載、模型、憑證）
	@echo "→ 容器狀態"
	@$(COMPOSE) ps --format '   {{.Service}}\t{{.Status}}' 2>/dev/null || true
	@echo ""
	@echo "→ YOLO 的模型權重（掛載沒進來的話這裡會是空的）"
	@$(COMPOSE) exec -T yolo sh -c 'ls /models/best.pt >/dev/null 2>&1 \
	  && echo "   ✅ /models/best.pt 在" \
	  || echo "   ❌ /models/ 是空的 —— 掛載沒進來，跑 make recreate"' 2>/dev/null \
	  || echo "   ⚠️  yolo 沒在跑"
	@echo ""
	@echo "→ YOLO 的模型有沒有真的載入"
	@$(COMPOSE) exec -T backend sh -c 'curl -fsS -m 15 http://yolo:5000/health' 2>/dev/null \
	  | grep -q '"model_loaded":true' \
	  && echo "   ✅ model_loaded=true" \
	  || echo "   ❌ 模型沒載入 —— 每張圖都會失敗，但端點照樣回 200"
	@echo ""
	@echo "→ 前端的憑證（掛載沒進來的話 HTTPS 會整個消失）"
	@$(COMPOSE) exec -T frontend sh -c 'ls /etc/nginx/certs/fullchain.pem >/dev/null 2>&1 \
	  && echo "   ✅ 憑證在" \
	  || echo "   ❌ /etc/nginx/certs/ 是空的 —— 只會跑 HTTP，跑 make recreate"' 2>/dev/null \
	  || echo "   ⚠️  frontend 沒在跑"
	@echo ""
	@# GPU 掉過的話，WSL2 的 VM 常常會跟著整個當掉——表現出來是「Docker 突然
	@# 連不上、六個容器一起消失」。事後很難查，因為那時 dmesg 已經跟著重來了。
	@# dxgkrnl 是 WSL 的 GPU 直通驅動，正常情況下一次開機只會註冊一次；
	@# 出現第二次代表 GPU 裝置中途掉了又重接。
	@echo "→ GPU 有沒有中途掉過（WSL 的 GPU 直通）"
	@n=$$(dmesg 2>/dev/null | grep -c "registering driver dxgkrnl" || echo 0); \
	  if [ "$$n" = "1" ]; then echo "   ✅ 正常（註冊 1 次）"; \
	  elif [ "$$n" = "0" ]; then echo "   ⚠️  讀不到 dmesg（權限或非 WSL 環境）"; \
	  else echo "   ❌ GPU 中途重置過 $$n 次 —— 重負載時的 GPU 掉線，VM 可能跟著當掉"; fi
	@echo ""
	@echo "→ HTTPS 實際回應"
	@code=$$(curl -sk -m 10 -o /dev/null -w '%{http_code}' https://127.0.0.1/ 2>/dev/null); \
	  [ "$$code" = "200" ] && echo "   ✅ HTTPS $$code" || echo "   ❌ HTTPS $$code（憑證掛載或 nginx 設定有問題）"

rebuild: ## 只重建某個模組：make rebuild M=backend
	@test -n "$(M)" || (echo "用法：make rebuild M=backend" && exit 1)
	$(COMPOSE) --env-file $(ENV_FILE) build --no-cache $(M)
	$(COMPOSE) --env-file $(ENV_FILE) up -d $(M)

clean: ## ⚠️ 停止並刪掉資料庫資料，整個重來
	@echo "這會刪掉資料庫裡所有資料。"
	@read -p "確定嗎？打 yes 繼續：" ans && [ "$$ans" = "yes" ]
	$(COMPOSE) --profile full down -v

check: ## 部署前檢查：確認沒有把秘密或大檔加進 git
	@echo "→ 檢查有沒有密碼進 git..."
	@# 比對檔名而不是「含有 password 這個字」——後者會把
	@# app/password.py（密碼雜湊模組）、crawler/password.py（登入牆偵測）
	@# 這種正當原始碼也標成密碼外洩。
	@! git ls-files | grep -iE '(^|/)(\.env|.*密碼.*|.*secret.*|.*credential.*)(\.[a-z]+)?$$|\.(key|pem|pfx|p12)$$' \
		|| (echo "  ❌ 上面這些檔案不該進 git！" && exit 1)
	@echo "  ✅ 沒有"
	@echo "→ 檢查有沒有大檔..."
	@! git ls-files | xargs -I{} sh -c 'test -f "{}" && find "{}" -size +5M' 2>/dev/null | grep . \
		|| (echo "  ⚠️  上面這些檔案超過 5MB，考慮改用 Release assets" )
	@echo "→ 檢查有沒有寫死的 IP..."
	@! grep -rn '100\.[0-9]\+\.[0-9]\+\.[0-9]\+' modules/ --include='*.py' --include='*.ts' --include='*.tsx' \
		|| (echo "   還有寫死的 tailnet IP，應該改用 config.py" && exit 1)
	@echo "  沒有"

# ---------- 測試 ----------
TEST_COMPOSE := $(COMPOSE) -f tests/docker-compose.test.yml
TEST_DB       := drug_prevention_test
# 用絕對路徑，不然 python 會抱怨 sys.prefix 有 ../ 而印出一堆 RuntimeWarning
PYTEST       := set -a; . ./$(ENV_FILE); set +a; export TEST_DB_NAME=$(TEST_DB); \
                cd tests && $(CURDIR)/.venv/bin/python -m pytest

test-up: ## 起測試環境（獨立測試資料庫 + stub 頂替 NLP/YOLO/爬蟲，不需要 GPU）
	@# 真的 nlp/yolo/crawler 如果還跑著（make full 起的），它們的「服務名」會跟
	@# stub 的「網路別名」撞在一起，Docker DNS 會輪流解析到兩邊——後端有時打到
	@# 真引擎、有時打到 stub，分數就不確定了，測試會莫名其妙地時好時壞。先收掉。
	@-$(COMPOSE) --profile full rm -sf nlp yolo crawler >/dev/null 2>&1 || true
	@# 測試用獨立 schema，不碰開發／展示的資料。
	@# mysql image 的 initdb 只在第一次建 volume 時跑，所以要自己建。
	$(TEST_COMPOSE) --env-file $(ENV_FILE) up -d mysql
	@echo "→ 等 MySQL healthy..."
	@until [ "$$($(TEST_COMPOSE) ps -q mysql | xargs docker inspect -f '{{.State.Health.Status}}')" = "healthy" ]; do sleep 2; done
	@set -a; . ./$(ENV_FILE); set +a; \
	  $(TEST_COMPOSE) exec -T mysql sh -c 'exec mysql -h 127.0.0.1 --protocol=TCP --default-character-set=utf8mb4 \
	    -uroot -p"$$MYSQL_ROOT_PASSWORD" \
	    -e "CREATE DATABASE IF NOT EXISTS $(TEST_DB) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci; \
	        GRANT ALL PRIVILEGES ON $(TEST_DB).* TO \"$$MYSQL_USER\"@\"%\"; FLUSH PRIVILEGES;"' \
	  && echo "  ✅ 測試資料庫 $(TEST_DB) 就緒"
	$(TEST_COMPOSE) --env-file $(ENV_FILE) up -d --build
	@echo ""
	@echo "  後端 http://localhost:8000　前端 http://localhost:8080"
	@echo "  資料庫 $(TEST_DB)（與 $${DB_NAME:-drug_prevention_db} 完全分開）"

test-down: ## 收掉測試環境，並丟掉測試資料庫（不動開發資料）
	@set -a; . ./$(ENV_FILE); set +a; \
	  $(TEST_COMPOSE) exec -T mysql sh -c 'exec mysql -h 127.0.0.1 --protocol=TCP --default-character-set=utf8mb4 \
	    -uroot -p"$$MYSQL_ROOT_PASSWORD" \
	    -e "DROP DATABASE IF EXISTS $(TEST_DB)"' 2>/dev/null && echo "  已丟棄 $(TEST_DB)" || true
	$(TEST_COMPOSE) down

test: test-up ## 跑完整測試套件，並產生 tests/report/SECURITY_REPORT.md
	@$(PYTEST) || true
	@echo ""
	@echo "  報告：tests/report/SECURITY_REPORT.md"

test-security: ## 只跑資安測試，印出完整待修清單
	@$(PYTEST) -m security -rxX || true

test-integration: ## 只跑整合測試（模組間介面契約，應該全綠）
	@$(PYTEST) -m integration

test-full: ## 用真實的 NLP/YOLO/爬蟲跑（需要 GPU 與 models/best.pt）
	$(COMPOSE) --env-file $(ENV_FILE) --profile full up -d --build
	@$(PYTEST) -m fullstack

backup: ## 備份資料庫到 data/backups/（Docker volume 不是備份）
	@# 2026-08-31 三個 volume 同時消失過一次，原因至今不明。
	@# 蒐證資料重建一次要跑爬蟲加 YOLO，不是幾分鐘的事——定期跑這個。
	@mkdir -p data/backups
	@set -a; . ./$(ENV_FILE); set +a; \
	  out=data/backups/$${DB_NAME}_$$(date +%Y%m%d_%H%M).sql.gz; \
	  $(COMPOSE) exec -T -e P="$$DB_PASSWORD" -e U="$$DB_USER" -e D="$$DB_NAME" mysql \
	    sh -c 'exec mysqldump -h127.0.0.1 --protocol=TCP -u"$$U" -p"$$P" \
	           --single-transaction --no-tablespaces --default-character-set=utf8mb4 \
	           --routines --events "$$D"' 2>/dev/null | gzip > $$out; \
	  gzip -t $$out && zcat $$out | tail -2 | grep -q "Dump completed" \
	    && echo "  ✅ $$out（$$(du -h $$out | cut -f1)）" \
	    || (echo "  ❌ 備份不完整，已刪除" && rm -f $$out && exit 1)

restore-help: ## 資料庫壞掉／volume 不見時怎麼救
	@echo "  先看 data/backups/ 有沒有 .sql.gz，有的話照 scripts/restore/README.md 灌回去。"
	@echo "  沒有的話那份 README 也寫了從爬蟲記錄檔重建的完整流程。"
	@ls -lh data/backups/*.sql.gz 2>/dev/null | tail -5 || echo "  ⚠️  目前沒有任何 dump"

smoke: ## 整合冒煙測試：驗證模組間的介面契約
	python3 scripts/smoke_test.py --base-url http://localhost:8000

up-staged: ## 分階段起：一次加一個模組，壞了才知道是誰
	$(COMPOSE) --env-file $(ENV_FILE) up -d mysql
	@echo "→ 等 MySQL healthy..." && sleep 5
	$(COMPOSE) --env-file $(ENV_FILE) up -d backend
	@echo "→ 後端起來了，先驗一次" && sleep 5 && python3 scripts/smoke_test.py || true
	$(COMPOSE) --env-file $(ENV_FILE) --profile full up -d nlp
	$(COMPOSE) --env-file $(ENV_FILE) --profile full up -d yolo
	$(COMPOSE) --env-file $(ENV_FILE) --profile full up -d crawler
	$(COMPOSE) --env-file $(ENV_FILE) up -d frontend
	@$(COMPOSE) ps

models: ## 下載模型權重（照 models/MODELS.txt 的清單）
	./scripts/download_models.sh

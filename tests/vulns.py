"""
發現清單的單一事實來源。

測試用 @known_vuln("SEC-01") 引用這裡的條目，報告產生器也讀同一份，
所以測試與報告永遠對得起來，不會有一邊改了另一邊忘記改的情況。

修好某一項之後不要從這裡刪掉——測試會從 xfail 變成 xpass，
報告會自動把它標成「已修復」，那正是我們想看到的進度。
"""

CRITICAL = "Critical"
HIGH = "High"
MEDIUM = "Medium"
LOW = "Low"

VULNS = {
    # ---------------- Critical ----------------
    "SEC-01": dict(
        severity=CRITICAL,
        title="三個機器對機器端點完全無驗證",
        where="routers/crawler.py:25، routers/ai_engine.py:13,71".replace("،", ","),
        impact="任何能連到後端的人都可以竄改任意網址的風險評分、灌入偽造蒐證資料、"
               "把已知毒品網站洗成 0 分。INTERNAL_API_TOKEN 被送進全部五個容器，"
               "但沒有任何一行程式碼讀它——服務間驗證從來沒有接上。",
        fix="在 dependencies.py 加一個 verify_internal_token 依賴，比對 "
            "os.environ['INTERNAL_API_TOKEN']（用 hmac.compare_digest），"
            "掛到三個 report 端點上；crawler/nlp/yolo 送出時帶同一個 header。",
    ),
    "SEC-02": dict(
        severity=CRITICAL,
        title="密碼使用未加鹽的單輪 SHA-256",
        where="dependencies.py:10, routers/auth.py:9, routers/users.py:10, main.py:10",
        impact="無鹽單輪雜湊可用彩虹表直接反查，password123 這類常見密碼秒破。"
               "同一個函式在四個檔案各複製一份，改的時候很容易漏掉其中一處。",
        fix="requirements.txt already 有 passlib 與 bcrypt，只是從沒 import。"
            "改用 passlib.context.CryptContext(schemes=['bcrypt'])，"
            "四處的重複實作收斂成 dependencies.py 一份。舊帳號在下次登入成功時重新雜湊。",
    ),
    "SEC-03": dict(
        severity=CRITICAL,
        title="預設管理員 admin / password123 每次啟動自動建立",
        where="main.py:14-37",
        impact="帳密寫死在原始碼裡，任何看得到 repo 的人都知道。"
               "系統不強制改密，也不會在介面上警告。",
        fix="改成啟動時產生隨機密碼並印在 log，或要求首次登入必須改密"
            "（User 加 must_change_password 欄位）。至少要在 README 明確標示。",
    ),
    "SEC-04": dict(
        severity=CRITICAL,
        title="JWT 沒有 exp，token 永久有效且無法撤銷",
        where="routers/auth.py:36",
        impact="token payload 只有 {'sub': account}。一旦外洩就是永久通行證，"
               "改密碼、停權、刪帳號都無法讓它失效。",
        fix="簽發時加 exp（建議 8 小時）與 jti；get_current_user 讓 PyJWT 驗 exp"
            "（預設就會驗，只要 payload 裡有）。需要立即撤銷的話再加一張 token 黑名單。",
    ),
    "SEC-05": dict(
        severity=CRITICAL,
        title="軟刪除的使用者，舊 token 仍可通過驗證",
        where="dependencies.py:39-58 vs routers/users.py:124",
        impact="delete_user 只設 is_deleted=True，沒動 is_active，"
               "而 get_current_user 只檢查 is_active。被刪除的人只要手上還有 token，"
               "就能繼續存取所有端點。配合 SEC-04（token 不過期）等於刪不掉人。",
        fix="get_current_user 的查詢加上 is_deleted == False，或直接改成 "
            "filter(account==..., is_deleted==False, is_active==True)。",
    ),

    # ---------------- High ----------------
    "SEC-06": dict(
        severity=HIGH,
        title="CORS 全開，且忽略已設定的 CORS_ORIGINS",
        where="main.py:62-68",
        impact="allow_origins=['*'] 搭配 allow_credentials=True 是規格上無效的組合。"
               "而且 .env.local 明明設了 CORS_ORIGINS=http://localhost:8080，程式完全沒讀。",
        fix="讀 os.getenv('CORS_ORIGINS') 並用逗號切開傳給 allow_origins，"
            "拿掉萬用字元。",
    ),
    "SEC-07": dict(
        severity=HIGH,
        title="SSRF：掃描端點不驗證使用者提供的網址",
        where="routers/scan.py:52-60",
        impact="url 直接轉給爬蟲抓取，不檢查 scheme、不擋內網網段。"
               "可用來探測內網、打雲端 metadata（169.254.169.254）、讀 file://。"
               "前端雖有網址格式檢查，但直接打 API 就繞過了。",
        fix="用 urllib.parse 驗證 scheme 只允許 http/https，解析主機名後拒絕 "
            "私有網段（10/8、172.16/12、192.168/16、127/8、169.254/16）與 localhost。",
    ),
    "SEC-08": dict(
        severity=HIGH,
        title="後端完全沒有密碼強度檢查",
        where="routers/users.py:18-22",
        impact="UserCreate 沒有任何 field_validator，POST /api/users/ 接受空字串密碼。"
               "8 碼規則只寫在前端 inputSecurity.ts，直接打 API 就繞過。",
        fix="UserCreate.password 加 field_validator，把前端 "
            "getPasswordValidationMessage 的規則搬到後端（長度、大小寫、數字、符號）。"
            "前端保留即時提示，後端負責真正把關。",
    ),
    "SEC-09": dict(
        severity=HIGH,
        title="XSS 防護放錯層：輸入端用黑名單，還會弄壞資料",
        where="schemas.py:6-19",
        impact="只擋 <script>、javascript:、onload=、onerror=。"
               "<img src=x onmouseover=1>、<script >、<svg onfocus=> 全部放行。"
               "而且未驗證的 crawler 端點寫入的 title/keywords/text_content 完全不過濾，"
               "管理員開報表頁就中招——儲存型 XSS。",
        fix="拿掉黑名單。輸入端只做長度與型別驗證，跳脫留給輸出端"
            "（React 預設就會跳脫，真正要注意的是 dangerouslySetInnerHTML）。"
            "後端回應加 Content-Security-Policy。",
    ),
    "SEC-10": dict(
        severity=HIGH,
        title="登入無速率限制，且可列舉帳號",
        where="routers/auth.py:20-33",
        impact="沒有次數限制也沒有鎖定，可無限暴力破解（配合 SEC-02 的弱雜湊更嚴重）。"
               "而且帳號不存在與帳號被凍結回不同訊息，可用來判斷帳號是否存在。",
        fix="四個失敗分支統一回 401「帳號或密碼錯誤」；"
            "加上以 IP + 帳號為鍵的失敗計數，超過門檻暫時鎖定；失敗登入寫進 audit_logs。",
    ),
    "SEC-22": dict(
        severity=HIGH,
        title="nginx 把整個後端暴露在 8080，backend 綁 127.0.0.1 是假的安全感",
        where="deploy/docker-compose.yml, modules/frontend/nginx.conf",
        impact="backend 綁 127.0.0.1:8000 看似只有本機能連，但 frontend 是 '8080:80'"
               "（綁所有介面），nginx 的 location /api/ 未經任何過濾轉給 backend:8000。"
               "任何連得到 8080 的人都能打到每一個後端端點，包含 SEC-01 那三個無驗證的。",
        fix="nginx 明確擋掉不該從外部進來的路徑（三個 /report/ 端點），"
            "或把 frontend 也綁 127.0.0.1，對外用反向代理另外控管。",
    ),

    # ---------------- Medium ----------------
    "SEC-11": dict(
        severity=MEDIUM,
        title="錯誤回應洩漏內部細節",
        where="routers/scan.py:72,76, routers/crawler.py:115",
        impact="把 str(e) 直接放進回應，會洩漏內部主機名、IP、SQLAlchemy 訊息，"
               "甚至連線字串片段。",
        fix="回一句通用訊息，詳細錯誤只寫進伺服器 log。",
    ),
    "SEC-12": dict(
        severity=MEDIUM,
        title="一般人員即可匯出全部蒐證資料",
        where="routers/export.py:14",
        impact="Excel 匯出用的是 get_current_user，任何登入者都能把整張 "
               "ai_analysis_results 下載成檔案帶走。對一個存放毒品案件蒐證資料的"
               "系統來說，「誰能整包帶走」跟「誰能查詢」是兩件事。",
        fix="匯出改成 verify_admin。"
            "24 小時清單刻意維持開放給一般人員——那是系統主畫面，擋掉他們就沒事可做了；"
            "而且 SEC-16 修完之後清單已經一頁最多 200 筆、不含 base64，"
            "不再是「整包帶走」的路徑。清單那邊驗的是「有界」而不是「限管理員」。",
    ),
    "SEC-13": dict(
        severity=MEDIUM,
        title="role 是自由字串，權限操作缺少自我保護",
        where="routers/users.py:74-107",
        impact="role 沒有白名單，可寫進任意字串。toggle-status 與 role 都沒有"
               "「不能對自己動手」的檢查（delete 有），也沒防止凍結 super_admin "
               "或刪掉最後一個管理員。",
        fix="role 改用 Enum 或 Literal['一般人員','系統管理員']；"
            "兩個 PUT 補上 target != current_admin 的檢查；"
            "刪除/降級前確認系統至少還留一個管理員。",
    ),
    "SEC-14": dict(
        severity=MEDIUM,
        title="登入跳脫、建立不跳脫，含特殊字元的帳號永久無法登入",
        where="schemas.py:27-30 vs routers/users.py:18",
        impact="UserLogin.account 在查資料庫前被 html.escape()，但 UserCreate 建立時不跳脫。"
               "帳號 a&b 存進去是 a&b，登入時卻拿 a&amp;b 去比對，永遠對不上。"
               "這是資料正確性問題，不是資安問題，但根因是同一個錯放的防護。",
        fix="拿掉 UserLogin 的 escape（見 SEC-09），輸入端不要改動資料。",
    ),
    "SEC-15": dict(
        severity=MEDIUM,
        title="缺少安全標頭，JWT 存在 localStorage",
        where="modules/frontend/nginx.conf, app/src/auth.ts:6",
        impact="沒有 CSP、HSTS、X-Frame-Options、X-Content-Type-Options。"
               "token 放 localStorage，配合 SEC-09 的 XSS 可直接被 JS 讀走。",
        fix="nginx 補上安全標頭；token 改存 httpOnly cookie（需同步調整 CSRF 防護），"
            "或至少先把 CSP 上好降低 XSS 竊取風險。",
    ),
    "SEC-16": dict(
        severity=MEDIUM,
        title="請求大小與分頁參數未設限",
        where="routers/crawler.py:117-122",
        impact="html_content / images_data 是 LONGTEXT，nginx 放行 50MB，"
               "無驗證端點可被灌爆儲存空間。page/limit 沒有範圍檢查，"
               "limit=999999 會把整張含 base64 圖片的表倒出來，page=-1 直接 500。",
        fix="Pydantic 欄位加 max_length；page/limit 用 Query(ge=1, le=100)；"
            "nginx 對 /api/ 調低 client_max_body_size。",
    ),
    "SEC-17": dict(
        severity=MEDIUM,
        title="稽核日誌有明顯缺口",
        where="dependencies.py:33, routers/users.py:139",
        impact="登入、登出、掃描、匯出都不寫紀錄——正好是數位證據系統最需要留痕的動作。"
               "查詢還硬上限 100 筆且沒有分頁，久了就查不到舊紀錄。",
        fix="在 auth/scan/export 補上 log_audit_action；查詢端點加分頁與時間區間篩選。",
    ),

    # ---------------- Low ----------------
    "SEC-20": dict(
        severity=LOW,
        title="硬編碼 tailnet IP 作為預設值",
        where="app/utils.py:29-31, routers/scan.py:52, modules/yolo/app/main.py",
        impact="環境變數沒設時會靜靜連到某台特定機器，正式環境可能把資料送錯地方。"
               "`make check` 就是在抓這個，目前會失敗。",
        fix="拿掉預設值，改成沒設就啟動失敗（跟 DB_PASSWORD / JWT_SECRET_KEY 一致）。",
    ),
    "SEC-21": dict(
        severity=LOW,
        title="CI 沒有測試與掃描，應用程式直接用 MySQL root",
        where=".github/workflows/, deploy/docker-compose.yml",
        impact="沒有任何自動化把關。應用程式用 root 連資料庫，"
               "一旦 SQL injection 或連線資訊外洩，影響範圍是整個資料庫。",
        fix="加一個 PR 觸發的 workflow 跑這套測試；"
            "MySQL 另建一個只有該資料庫權限的應用帳號。",
    ),
}


def get(vid):
    if vid not in VULNS:
        raise KeyError(f"vulns.py 沒有 {vid} 這個條目")
    return VULNS[vid]


SEVERITY_ORDER = [CRITICAL, HIGH, MEDIUM, LOW]


# ---------------- 執行環境 / 部署 ----------------
VULNS["INFRA-01"] = dict(
    severity=LOW,
    title="前端容器的 healthcheck 永遠失敗（IPv6 localhost）",
    where="modules/frontend/Dockerfile, modules/frontend/nginx.conf",
    impact="nginx.conf 只寫 listen 80（IPv4，實際綁 0.0.0.0:80），"
           "但容器的 /etc/hosts 讓 localhost 同時對應 127.0.0.1 與 ::1，"
           "healthcheck 的 wget 會先試 ::1 而被拒絕。"
           "結果是 frontend 永遠顯示 unhealthy——make ps 上一片紅，"
           "真的出問題時反而看不出來。目前沒有服務 depends_on frontend 的健康狀態，"
           "所以還不會擋到啟動，但只要有人加上去就會卡死。",
    fix="healthcheck 改用 http://127.0.0.1/ ，或在 nginx.conf 補一行 listen [::]:80;",
)


VULNS["INFRA-02"] = dict(
    severity=HIGH,
    title="爬蟲每爬一頁重寫整個記錄檔，導致記憶體耗盡與服務中斷",
    where="modules/crawler/app/record_paths.py:128-140, engine_v2.py:225,237",
    impact="append_images_record 與 append_nlp_record 的作法是「把整個檔案讀進記憶體 → "
           "append 一筆 → 整份重寫回去」，而 engine_v2 在每爬一頁時都各呼叫一次。"
           "存的還是 base64 截圖與商品圖，所以檔案成長極快。"
           "\n\n2026-08-29 實測：images.json 已達 934 MB，爬蟲容器佔用 4.5 GB 記憶體、"
           "CPU 160%，每爬一頁產生約 1.9 GB 磁碟 I/O（讀 934 MB + 寫 934 MB）。"
           "當天記憶體耗盡，OOM killer 殺掉 nlp 與 yolo 容器（Exited 137），"
           "Playwright 的 Chromium 一併崩潰，WSL 寫出一個 191.7 GB 的核心傾印檔，"
           "差點把整顆 C 槽塞爆。"
           "\n\n這是自我造成的阻斷服務：檔案越大每頁越慢、記憶體越高，成長是加速的。"
           "而且那份 base64 資料早就完整寫進 MySQL 了，爬蟲端這份完全重複。",
    fix="1. 改成 JSONL 追加寫入：open(path, 'a') 後寫一行 json.dumps(record)，"
        "每頁 I/O 從 1.9 GB 降到幾 KB，記憶體降到接近零。"
        "\n2. 不要在爬蟲端存 base64——資料已經在 MySQL，這裡只留 URL 與檔名即可。"
        "\n3. 加上檔案輪替或保留上限，避免任何單一記錄檔無限成長。",
)

VULNS["INFRA-03"] = dict(
    severity=MEDIUM,
    title="容器沒有記憶體上限，單一模組失控就會拖垮整台機器",
    where="deploy/docker-compose.yml",
    impact="只有 yolo 設了 memory: 12G，backend / crawler / nlp / mysql 都沒有任何限制。"
           "任何一個模組吃光記憶體，OOM killer 會去殺「它挑中的」程序，"
           "而不是肇事的那個——2026-08-29 就是爬蟲吃掉 4.5 GB，結果被殺的是 nlp 和 yolo。"
           "在 WSL2 上更嚴重，因為預設只分到主機一半的記憶體（本機是 15 GB），"
           "而且沒有 .wslconfig 可以調整，崩潰傾印也沒有大小上限。",
    fix="每個服務都設 mem_limit（例如 crawler 2G、nlp 4G、backend 1G），"
        "讓失控的模組先被殺掉，而不是拖累其他人。"
        "WSL2 另外建立 .wslconfig 設定 memory 與 swap，並關閉或限制崩潰傾印。",
)


VULNS["ML-01"] = dict(
    severity=HIGH,
    title="模型評估失真：驗證集與訓練資料同源，量不出真實表現",
    where="src/bert_train/train_bert.py, data/processed/",
    impact="正樣本 337 筆全部來自 data/Positive Sample/raw/（單一來源，警政蒐集），"
           "負樣本來自另外兩個資料夾。不同來源的網頁在模板、語言、抓取方式上有系統性差異，"
           "模型只要學會分辨「這份 HTML 來自哪個資料夾」就能拿到 0.99——完全不必理解內容。"
           "\n\n實測對照（2026-08-30，217 筆人工標註的真實爬取網頁，"
           "與訓練資料網域零重疊）："
           "\n    同源驗證集    ROC-AUC 0.998"
           "\n    真實評估集    ROC-AUC 0.879（現行線上模型）"
           "\n\n差距 0.12。而且同一份資料訓出來的新模型，三種文字抽取方式全部輸給現行模型："
           "\n    full（含 title）  0.867      clean（去導覽）0.839      body  0.823"
           "\n訓練資料的天花板已經到了。"
           "\n\n危險之處在於：如果只看同源驗證集，會以為新模型 0.998 很成功而推上線，"
           "實際上它比現行的還差。這個評估集是唯一能擋下這種錯誤的東西。"
           "\n\n關於「模型只讀前 256 個 token」（訓練與推論一致，這點是對的）："
           "近七成網頁的前 300 字全是導覽選單，導覽樣板佔文字的 64%，"
           "看起來像個明顯的缺陷。實測後發現不是。"
           "\n\n用現行模型、不重新訓練，只改變餵進去的那段文字，在同一份評估集上比較："
           "\n    A. head（現況）          AUC 0.893  acc 0.801  precision 0.750"
           "\n    B. title + head          AUC 0.898  acc 0.842  precision 0.797   ← 唯一多指標同時改善"
           "\n    C. 去導覽樣板             AUC 0.863  acc 0.781  漏報 2→15"
           "\n    D. 分段取最高分           AUC 0.860  acc 0.714  precision 崩到 0.671"
           "\n    E. 分段取平均             AUC 0.879  acc 0.786  漏報 2→9"
           "\n    F. 關鍵字密度最高段        AUC 0.908  acc 0.816"
           "\n\n結論與直覺相反："
           "\n  · 讀更多沒有比較好。D 讓模型讀完整頁，只要任何一段誤觸就整站高分，"
           "正常網站的頁尾、廣告、推薦商品區都會誤觸，precision 直接崩掉。"
           "\n  · 去掉導覽反而更差。選單本身就帶訊號（INDICA / SATIVA / MOONROCKS "
           "常常直接寫在選單裡），拿掉等於丟資訊。這也解釋了為什麼訓練時的 clean 模式"
           "（0.839）輸給 full（0.867）。"
           "\n  · F 分數最高但不該採用。它先用正則挑出「毒品關鍵字最多的段落」，"
           "那個正則本身就是個粗糙分類器，隱語與新興毒品名稱不在表裡就會漏掉——"
           "把系統的辨識能力綁在一份要人工維護的關鍵字表上，比模型本身更脆弱。"
           "\n\n所以輸入策略的天花板也到了。模型的瓶頸確實在訓練資料，不在讀多少 token。",
    fix="1. 把 data/eval_sample/ 那 217 筆人工標註當作唯一驗收標準，"
        "任何模型改動都用它比較，不要看同源驗證集的分數。"
        "\n2. 要真的改善模型，得先擴充正樣本：來源要多元（不能全來自同一批蒐集），"
        "數量至少數千筆。這是以月為單位的工作。"
        "\n3. train_bert.py 已補上 compute_metrics 與警語，"
        "避免有人拿 0.99 當成果。"
        "\n4.（已記錄，尚未實作）爬蟲的 text_content 加上網頁標題："
        "modules/crawler/app/crawler.py:227 與 crawl_core.py:31 目前是 "
        "document.body.innerText，抓不到 <head> 裡的 <title>，"
        "但訓練資料是 soup.get_text() 掃整份文件、含 title——這是個真實的 train/serve 落差。"
        "改成 document.title + '. ' + document.body.innerText 即可，兩處各一行。"
        "效益誠實地說不大（AUC +0.005、accuracy +0.041），但零成本零風險。"
        "\n\n⚠️ 順帶一提：系統的瓶頸不在模型。同一天把判定規則從加權平均改成門檻判定，"
        "recall 從 0.653 拉到 0.983——效益遠大於任何模型調整。"
        "\n這輪實驗最大的價值不是找到改善，而是排除了四個看似合理的方向。",
)

VULNS["ML-02"] = dict(
    severity=MEDIUM,
    title="NLP 關鍵字抽取回傳的是 subword 碎片與停用詞",
    where="modules/nlp/app/main.py（extract_keywords）",
    impact="XLM-RoBERTa 用 SentencePiece 切詞，dispensary 會被切成 ▁di+spen+sa+ry，"
           "而 extract_keywords 直接把單一 token decode 出來當關鍵字，"
           "所以畫面上長期出現 ana、pensa、ed、BU、va、Edi 這種看不懂的碎片。"
           "又沒有停用詞表，而 CLS 的 attention 天生會集中在功能詞上（attention sink），"
           "the / and / of 一直霸佔前幾名。"
           "實測 87 筆真正跑過模型的結果：長度 ≤3 的碎片佔 58%、停用詞 29%、"
           "純標點或數字 9%——前五名幾乎沒有一個是有用的資訊。"
           "這個欄位是承辦人員判讀「模型為什麼判定這是毒品網站」的唯一依據，"
           "顯示不出理由等於無法人工覆核。",
    fix="用 SentencePiece 的 ▁ 字首標記把碎片組回完整的字，分數取該字所有碎片的平均；"
        "加上停用詞與電商樣板用語（cart / HOME / SITEWIDE）的過濾表，"
        "寫死在檔案裡不要用 nltk 或 spacy——那兩個啟動時要下載語料，容器沒網路就起不來；"
        "attention 只取最後四層，前面幾層還在做位置與語法，一起平均等於用雜訊稀釋訊號。"
        "註：試過改用「出現次數加總」壓過樣板用語，實測更糟——重複最多次的正是橫幅與頁尾"
        "（SITEWIDE、FREE SHIPPING、MONDAY–FRIDAY），六個網頁裡三個變差，"
        "所以是列表過濾而不是改計分方式。",
)

VULNS["INFRA-04"] = dict(
    severity=MEDIUM,
    title="爬蟲把商品圖 base64 在記錄檔裡再存一份，磁碟成長 45 MB/分",
    where="modules/crawler/app/record_paths.py（slim_images_record / slim_nlp_record）",
    impact="商品圖在 receive_crawler_raw_data 已經寫進 suspect_websites.images_data"
           "（routers/crawler.py:78），記錄檔裡是同一份資料的第二份。"
           "實測 219 筆的組成：product_images 426.8 MB（92.5%）、"
           "screenshot_b64 17.4 MB（3.8%）、full_screenshot_base64 17.4 MB（3.8%）。"
           "實機量到 images.json 五分鐘長 223 MB，約 45 MB/分、一小時 2.7 GB。"
           "記錄檔改成 JSONL（INFRA-02）之後記憶體不再成長，但磁碟照樣會被塞爆——"
           "那是兩個不同的問題。",
    fix="記錄檔只留摘要（網址、時間、分級、圖片檔名、有沒有截圖），base64 一律不寫；"
        "再配合 JSONL 檔案輪替（jsonl_max_bytes / jsonl_backup_count）控制總量。"
        "實測 713 MB + 89 MB 的舊檔轉成摘要後只剩 0.35 MB + 0.60 MB，1321 筆完整保留。"
        "代價是記錄檔不再兼任「webhook 送失敗時的本機備份」——2026-08-31 三個 volume "
        "消失時就是靠它救回 4665 張商品圖，所以 make backup 要確實執行。"
        "另外整頁截圖（screenshot_b64）後端從來沒收，記錄檔是唯一一份，"
        "換格式前務必先把舊檔備份出來。",
)

VULNS["INFRA-05"] = dict(
    severity=HIGH,
    title="容器沒有關閉 core dump，Chromium 崩一次寫出 172 GB",
    where="deploy/docker-compose.yml（ulimits）",
    impact="WSL 的 /proc/sys/kernel/core_pattern 是 |/wsl-capture-crash。"
           "core_pattern 導向管線時，核心不會套用 RLIMIT_CORE 的大小上限，"
           "所以一個崩潰的行程會把整個位址空間寫成傾印檔，沒有任何煞車。"
           "2026-08-31 爬蟲的 Chromium 崩潰，傾印檔以 370 MB/s 成長，"
           "七分鐘寫到 172 GB，C 槽從 580 GB 可用掉到 366 GB——"
           "再 26 分鐘就會塞爆整顆磁碟。2026-08-29 那次是 191.7 GB，"
           "當時直接把 Docker Desktop 弄掛。"
           "兩次都不是崩潰本身造成災難，是沒有上限的傾印檔。",
    fix="compose 每個服務加 ulimits.core: 0。管線模式下 RLIMIT_CORE=0 是核心的"
        "特例判斷（do_coredump 會直接 goto fail），所以這一行真的擋得掉。"
        "這是止血，不是修好崩潰本身——Chromium 為什麼崩要另外查。",
)

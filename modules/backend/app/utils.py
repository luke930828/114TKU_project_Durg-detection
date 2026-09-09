import os
import time
import uuid
from urllib.parse import urlparse

import requests

# 分級門檻。crawler.py 的 24 小時清單也讀這裡，不要各寫一套
# （以前 utils 用 74/35、crawler 用 85/75，同一筆資料會有兩個答案）。
# 門檻是對「校準後的分數」設的（見 nlp/app/calibration.py），可以照字面當機率讀。
# 換算前是 90/5，命中率完全一樣（極高 95.2%、高 90.9%、中 57.1%），只是尺度不同。
# 換模型時門檻和校準參數要一起重新量，它們綁在一起。
NLP_HIGH = 85        # 到這個機率就一定要送人工覆核
NLP_MEDIUM = 30      # 「要不要進覆核清單」的下限，低於它不會被人看到
YOLO_CONFIRM = 30    # 影像也附和到這個程度，覆核優先度往上提


def calculate_multimodal_risk_100_scale(nlp_raw_score: int, yolo_raw_score: int):
    """
    依 NLP 與 YOLO 的原始分數（0~100）算出綜合分數與風險等級。

    為什麼不再用加權平均
    ────────────────────
    原本是 0.6×NLP + 0.4×YOLO > 74 才算極高風險。問題是純文字的販售頁、
    訂單查詢頁、FAQ 頁本來就沒有商品圖可辨識，YOLO 給 0 分是正確的——
    但 0.6×100 + 0.4×0 = 60，低於 74，整個網站就被放行了。

    用 217 筆人工標註的真實網頁量過（data/eval_sample/）：
        舊規則 0.6n+0.4y > 74   recall 0.653　漏掉 41 個毒品網站
        改用   NLP >= 90         recall 0.975　漏掉  3 個
    而且把 YOLO 加進判定條件完全沒有幫助（漏報一樣是 3 個，
    precision 卻從 0.772 掉到 0.706，還多 14 件要人工看）。

    所以 YOLO 不參與「要不要覆核」的判定，改用來排覆核的優先順序：
    NLP 高分且 YOLO 也附和的那批，實際命中率 81%；YOLO 沒附和的是 69%。

    綜合分數仍以加權算出並保留，因為前端與報表要拿它排序；
    但風險等級不再由它決定。
    """
    # 排序分數＝文字分數本身，不再是加權平均。
    #
    # 加權平均實測比單用文字差很多（ROC-AUC 0.861 vs 0.938，完整網頁文字）：
    # 影像分數單獨的 ROC-AUC 只有 0.38~0.53，混進去等於在好訊號裡摻雜訊，
    # 而且權重給得越高排序越差。
    #
    # 影像仍然有用，但用在「同一級之內誰先看」——那是 ORDER BY 的次要鍵
    # （見 crawler.py），跟「把兩個分數相加」是完全不同的操作。
    # 這也讓排序與判定終於用同一套邏輯：判定早就是「文字決定要不要看、
    # 影像決定先看誰」，只有排序還停在加權平均。
    combined = int(nlp_raw_score)

    if nlp_raw_score >= NLP_HIGH and yolo_raw_score >= YOLO_CONFIRM:
        risk_level = "極高風險"                      # 兩個引擎都指向毒品
    elif nlp_raw_score >= NLP_HIGH:
        risk_level = "高風險 (優先人工覆核)"          # 文字確定，影像沒東西可看
    # 不要加回 `or yolo_raw_score >= NLP_HIGH`：YOLO 不參與「要不要覆核」的判定。
    # 實測那條規則只產生誤報——YOLO 在乾淨的商品照上很容易把保健食品、化妝品
    # 看成毒品，而文字完全沒有訊號時，那幾乎一定是誤判。
    elif nlp_raw_score >= NLP_MEDIUM:
        risk_level = "中風險 (建議人工覆核)"
    else:
        risk_level = "低風險"

    return combined, risk_level


# needs_review() 已移除：它是第二套分級標準，內容正是實測後刪掉的
# 「YOLO 單獨高分也送覆核」。全專案沒有任何地方呼叫它，但名字取得像「就是這個」，
# 留著遲早會被撿回去用。要判斷等級請用 calculate_multimodal_risk_100_scale()。


# 服務之間的呼叫要重試。
# compose 一直有設 HTTP_TIMEOUT / HTTP_RETRIES，但程式碼從來沒讀過——設了等於沒設。
# 容器重建、服務重啟、暫時性的 DNS 解析失敗都是常態；沒有重試的話，那段時間的
# 分析會靜靜地掉，網址永遠停在「文字分析中...」，而且沒有任何地方會告訴你。
HTTP_TIMEOUT = int(os.getenv("HTTP_TIMEOUT", "10"))
HTTP_RETRIES = int(os.getenv("HTTP_RETRIES", "2"))


def post_with_retry(url: str, *, json=None, headers=None, timeout=None, retries=None):
    """POST 並在連線失敗時重試。回傳 Response，全部失敗則回傳 None。

    只重試「連線層」的錯誤（連不上、DNS 解析不到、逾時）。4xx/5xx 直接回傳，
    那是對方收到了但不接受，重送幾次結果一樣。
    """
    timeout = HTTP_TIMEOUT if timeout is None else timeout
    attempts = (HTTP_RETRIES if retries is None else retries) + 1
    delay = 2
    last = None
    for i in range(attempts):
        try:
            return requests.post(url, json=json, headers=headers, timeout=timeout)
        except (requests.exceptions.ConnectionError,
                requests.exceptions.Timeout) as e:
            last = e
            if i < attempts - 1:
                print(f"[重試 {i + 1}/{attempts - 1}] {url} 連線失敗，{delay}s 後再試：{e.__class__.__name__}")
                time.sleep(delay)
                delay *= 2
    print(f"[錯誤] {url} 連續 {attempts} 次都連不上，放棄：{last}")
    return None


def dispatch_to_ai_engines(url: str, html_content: str, images: list):
    """
    背景任務：將資料派發給 NLP 與 YOLO 引擎
    """
    # 讀取環境變數網址 (預設值為單機開發測試用)
    # 不給預設值。以前這裡寫死了某台機器的 tailnet IP，環境變數沒設時
    # 會靜靜地把蒐證資料送到那台機器去——那比啟動失敗嚴重得多。
    # 跟 DB_PASSWORD / JWT_SECRET_KEY 一樣，沒設就直接爆掉。
    NLP_PREDICT_URL = os.environ["NLP_PREDICT_URL"]
    YOLO_API_URL = os.environ["YOLO_API_URL"]
    BACKEND_NLP_REPORT_URL = os.getenv("BACKEND_NLP_REPORT_URL", "http://127.0.0.1:8000/api/nlp/report/")
    # /api/nlp/report/ 現在要驗證了，後端打自己也不例外。
    # 走 HTTP 回推自己這件事本身有點怪（BUG-03 的重複寫入就是這樣來的），
    # 但在改掉之前，它一樣得帶 token，不然這條路徑會靜靜地全部 401。
    INTERNAL_HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}
    
    generated_task_id = str(uuid.uuid4())[:8]

    # 第一階段：派發給 NLP 的任務 (文字)
    try:
        nlp_payload = {
            "url": url,
            "text": html_content
        }
        print("準備將文字派發給 NLP...")

        response = post_with_retry(NLP_PREDICT_URL, json=nlp_payload)

        if response is not None and response.status_code == 200:
            nlp_result = response.json()
            print(f"NLP 分析完成！收到結果：{nlp_result}")
            
            score_float = nlp_result.get("score", 0)
            risk_score_int = int(score_float * 100)
            
            internal_payload = {
                "url": url,
                "risk_score": risk_score_int,
                "nlp_keywords": nlp_result.get("keywords", [])
            }
            # 同步回傳給後端資料庫
            requests.post(BACKEND_NLP_REPORT_URL, json=internal_payload,
                          headers=INTERNAL_HEADERS, timeout=10)
            print("NLP 結果已成功同步至資料庫！")
            
    except requests.exceptions.Timeout:
        print("呼叫 NLP 逾時！")
    except Exception as e:
        print(f"派發至 NLP 引擎失敗: {e}")

    # 第二階段：派發給 YOLO 的任務 (圖片)
    #
    # 沒有圖片時要明確寫「無影像可分析」，不能讓 yolo_details 留在「影像分析中...」。
    # 那個字串是所有地方判斷「還在跑」的依據（scan.py 用它決定要不要重新派發、
    # 前端用它決定要不要繼續輪詢），一頁本來就沒有商品圖時它永遠不會被覆蓋，
    # 畫面就會一直轉。「沒有圖可分析」是確定的結果，不是中間狀態。
    if not images:
        print("這一頁沒有可分析的圖片，直接把 YOLO 結果記成「無影像」。")
        try:
            requests.post(
                BACKEND_NLP_REPORT_URL.replace("/api/nlp/report/", "/api/ai_result/report/"),
                json={
                    "url": url,
                    "risk_score": 0,
                    "yolo_objects": [],
                    "is_valid_drug": False,
                    "class_metadata": {},
                    "representative_image_base64": None,
                    "representative_image_detections": [],
                    "no_images": True,
                },
                headers=INTERNAL_HEADERS, timeout=10)
        except Exception as e:
            print(f"回報「無影像」失敗：{e}")

    if images and len(images) > 0:
        print(f"準備將 {len(images)} 張圖片逐一派發給 YOLO...")
        
        for index, single_image_str in enumerate(images):
            try:
                yolo_payload = {
                    "task_id": f"{generated_task_id}_{index}", 
                    "url": url,
                    "image_base64": single_image_str,
                    "total_images": len(images),  
                    "priority": 0
                }
                response = post_with_retry(YOLO_API_URL, json=yolo_payload, timeout=5)
                if response is None:
                    print(f"    第 {index+1} 張派發失敗（連不上 YOLO），這張圖的分析會缺。")
                    continue
                print(f"    第 {index+1} 張派發成功！對方回應: {response.text}")
            except Exception as e:
                print(f"    第 {index+1} 張圖片派發至 YOLO 失敗: {e}")

def registrable_domain(url: str) -> str:
    """
    取出用來比對白名單的網域。www. 視為同一個站。

    不用 tldextract：後端沒這個相依，而且白名單比的是「使用者輸入的那個站」，
    不需要處理 co.uk 這種多段字尾——momo.com.tw 與 www.momo.com.tw
    要視為同一個，剩下的交給完整網域字串比對就夠準了。
    """
    try:
        host = (urlparse(url if "//" in url else f"//{url}").hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def domain_sql_expr(column):
    """registrable_domain() 的 SQL 版本，讓網域比對可以交給資料庫做。

    Python 版一次只能處理一個字串，要比對整張表就得先把資料撈回來，
    那在有 base64 欄位的表上非常貴。規則兩邊必須一致：
    小寫、去掉 scheme／路徑／埠號、www. 視為同一個站。
    """
    from sqlalchemy import case, func
    host = func.lower(func.substring_index(
        func.substring_index(func.substring_index(column, "//", -1), "/", 1),
        ":", 1))
    return case((host.like("www.%"), func.substring(host, 5)), else_=host)


def is_whitelisted(db, url: str):
    """
    這個網址所屬的網域在不在白名單裡。找到就回傳那一筆，否則 None。

    以前是拿完整網址做等值比對，等於只擋得住白名單裡「一模一樣」的那一頁。
    實測 https://www.momoshop.com.tw/ 加了白名單之後，
    https://www.momoshop.com.tw/goods/GoodsDetail.jsp?i_code=12345 照樣被分析——
    momo 有幾十萬個商品頁，一頁一頁加是不可能的，白名單形同無效。
    改成比對網域。
    """
    import database
    target = registrable_domain(url)
    if not target:
        return None
    for row in db.query(database.WhitelistWebsite).all():
        if registrable_domain(row.url) == target:
            return row
    return None


def purge_analysis_for_domain(db, domain: str) -> int:
    """把某個網域底下所有的 AI 分析結果刪掉，回傳刪除筆數。

    為什麼加了白名單就要清掉
    ──────────────────────
    白名單是用網域比對的，意思是「這個站是正常的，不要再判它」。但先前
    只刪掉使用者按的那一筆，同網域其餘的還留在待確認清單裡——實測
    cathinonelabs.com 在待確認有 48 筆、rocklandcannabisdispensary.com 45 筆。
    使用者把站標成正常之後，畫面上還掛著 47 筆要他處理，而且處理不掉
    （網域已經在白名單，再按也沒有新東西可加）。

    留著也沒有意義：那些分數是「這個站可疑」算出來的，而人已經判定它不可疑。
    下次爬蟲遇到這個網域會直接放行，不會重新產生。

    網域比對交給資料庫做（domain_sql_expr），不要把候選資料撈回 Python。
    先前是 LIKE 粗篩後逐筆比對，而 .all() 會把整列連 representative_image_base64
    這種 LONGTEXT 一起載入——實測一個 50 筆的網域要 1.5 秒，只取 id/url 是 0.08 秒，
    交給 SQL 判斷是 0.02 秒。加白名單是承辦人員會連續按很多次的動作，
    每一次都全表掃描又搬一堆圖片回來，畫面就會頓住。
    """
    import database
    if not domain:
        return 0
    removed = db.query(database.AIAnalysisResult).filter(
        domain_sql_expr(database.AIAnalysisResult.url) == domain
    ).delete(synchronize_session=False)
    return removed


def is_blacklisted(db, url: str):
    """這個網址所屬的網域在不在人工黑名單裡。找到就回傳那一筆，否則 None。"""
    import database
    target = registrable_domain(url)
    if not target:
        return None
    for row in db.query(database.BlacklistWebsite).all():
        if registrable_domain(row.url) == target:
            return row
    return None


# LIKE 的萬用字元。搜尋關鍵字是使用者輸入的，不跳脫的話 q=% 會把整張表撈出來，
# q=a_b 會誤命中 axb——實測 q=% 在 6695 筆的表上回傳全部 6695 筆。
# 反斜線要第一個換，不然後面換出來的跳脫字元會被重複處理。
LIKE_SPECIAL = (("\\", "\\\\"), ("%", "\\%"), ("_", "\\_"))


def like_pattern(keyword: str) -> str:
    """把使用者的關鍵字轉成安全的 LIKE 樣式（呼叫端要配 escape="\\"）。"""
    text = (keyword or "").strip()
    for src, dst in LIKE_SPECIAL:
        text = text.replace(src, dst)
    return f"%{text}%"


# OCR 文字要「合併」網頁文字後再判一次，不是單獨送。
# 單獨送的話模型拿到的是一袋沒有上下文的碎片，會把普通商品頁判成高分；
# 而且「分數比較高才覆蓋」還會把原本從網頁文字算出來的關鍵字整組蓋掉。
# 圖片裡的字跟網頁上的字本來就是同一頁的內容，一起判才有上下文，
# 出來也只有一組分數與關鍵字，不會有兩套答案。
OCR_MIN_CONFIDENCE = 0.5     # EasyOCR 低於這個值的多半是雜訊（實測 0.36 那段是 'SHIPPIHGORLDWIIDE'）
OCR_MIN_CHARS = 2            # 一兩個字元的碎片沒有語意
OCR_MIN_TOTAL_CHARS = 10     # 全部串起來還不到 10 個字就別浪費一次推論
# 合併後用 512（XLM-R 的上限），不是預設的 256。
# OCR 文字會佔掉一部分額度，還用 256 的話等於「用更少的網頁內容」重判一次，
# 分數會莫名其妙地掉。
MERGED_MAX_LENGTH = 512


def ocr_texts_to_sentence(ocr_results) -> str:
    """把 OCR 的結果整理成一段可以餵給 NLP 的文字。

    只留信心夠高、長度夠的片段。順序照 EasyOCR 給的（大致由上到下、由左到右），
    比重新排序更接近人看到的版面。
    """
    if not ocr_results:
        return ""
    texts = (ocr_results or {}).get("detected_texts") or []
    kept = []
    for item in texts:
        text = (item.get("text") or "").strip()
        if len(text) < OCR_MIN_CHARS:
            continue
        if (item.get("confidence") or 0) < OCR_MIN_CONFIDENCE:
            continue
        kept.append(text)
    return " ".join(kept)


def rescore_with_ocr_text(url: str, ocr_results):
    """把「圖片裡的文字 + 網頁文字」合併後再送 NLP 判一次。"""
    ocr_sentence = ocr_texts_to_sentence(ocr_results)
    if len(ocr_sentence) < OCR_MIN_TOTAL_CHARS:
        return

    import database
    db = database.SessionLocal()
    try:
        suspect = db.query(database.SuspectWebsite).filter(
            database.SuspectWebsite.url == url).first()
        page_text = (suspect.html_content or "") if suspect else ""
    finally:
        db.close()

    # OCR 放前面。就算 512 還是被截，先進去的是圖片文字——那是這次重判「多出來」
    # 的東西；網頁文字第一次就已經判過了，被截掉的部分不算損失。
    combined = f"{ocr_sentence}\n{page_text}".strip()

    NLP_PREDICT_URL = os.environ["NLP_PREDICT_URL"]
    BACKEND_NLP_REPORT_URL = os.getenv(
        "BACKEND_NLP_REPORT_URL", "http://127.0.0.1:8000/api/nlp/report/")
    INTERNAL_HEADERS = {"X-Internal-Token": os.environ["INTERNAL_API_TOKEN"]}

    try:
        # report=False：讓後端自己回寫，才有機會標註哪些關鍵字是從圖片讀到的。
        resp = requests.post(
            NLP_PREDICT_URL,
            json={"url": url, "text": combined[:20000],
                  "report": False, "max_length": MERGED_MAX_LENGTH},
            timeout=20,
        )
        if resp.status_code != 200:
            print(f"[OCR+文字→NLP] {url} 回應 {resp.status_code}，略過")
            return
        result = resp.json()
    except Exception as e:
        print(f"[OCR+文字→NLP] {url} 呼叫 NLP 失敗：{e}")
        return

    merged_score = int(float(result.get("score", 0)) * 100)

    # 只有「真的出現在圖片文字裡」的關鍵字才標註來源。
    # 全部都標的話會誤導——合併之後大部分關鍵字其實來自網頁本文。
    keywords = []
    for k in (result.get("keywords") or [])[:100]:
        keywords.append(f"[圖片文字] {k}" if k and k in ocr_sentence else k)

    print(f"[OCR+文字→NLP] {url} 合併重判 {merged_score} 分")
    try:
        requests.post(
            BACKEND_NLP_REPORT_URL,
            json={"url": url, "risk_score": merged_score, "nlp_keywords": keywords},
            headers=INTERNAL_HEADERS, timeout=10,
        )
    except Exception as e:
        print(f"[OCR+文字→NLP] {url} 回寫失敗：{e}")

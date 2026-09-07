"""輸入大小與分頁參數的邊界。"""
import pytest
from conftest import known_vuln

pytestmark = pytest.mark.security


@known_vuln("SEC-16")
def test_oversized_payload_rejected(internal, unique_url):
    """
    html_content 是 LONGTEXT，nginx 放行 50MB。

    SEC-01 修好之後外人已經灌不進來了，但這裡仍然要驗——內部服務被打下來
    （或 token 外洩）時，欄位大小還是最後一道防線。所以刻意用 internal 打，
    用 anon 的話會停在 401，看起來過了其實沒驗到。
    """
    big = "A" * (3 * 1024 * 1024)      # 3MB
    r = internal.post("/api/crawler/report/", json={
        "task_type": "flood", "url": unique_url, "text_content": big,
        "keywords": [], "product_images_b64": []})
    assert r.status_code in (400, 413, 422), (
        f"3MB 的內容被照單全收（HTTP {r.status_code}）"
    )


@known_vuln("SEC-16")
def test_limit_parameter_capped(admin):
    """limit=999999 會把整張含 base64 圖片的表倒出來。"""
    r = admin.get("/api/crawler/automated_24h_list/", params={"limit": 999999})
    assert r.status_code in (400, 422) or \
        r.json()["pagination"]["limit"] <= 100, (
        f"limit 沒有上限：{r.json().get('pagination')}"
    )


@known_vuln("SEC-16")
@pytest.mark.parametrize("page", [0, -1, -999])
def test_negative_page_handled(admin, page):
    r = admin.get("/api/crawler/automated_24h_list/", params={"page": page})
    assert r.status_code in (400, 422), f"page={page} 沒有被擋（HTTP {r.status_code}）"


@known_vuln("SEC-16")
def test_long_task_type_does_not_crash(internal, unique_url):
    """
    BUG-07：suspect_websites.title 是 String(100)，
    但由不受限的 task_type 組成，長輸入會觸發 MySQL DataError → 500。
    """
    r = internal.post("/api/crawler/report/", json={
        "task_type": "X" * 500, "url": unique_url, "text_content": "x",
        "keywords": [], "product_images_b64": []})
    assert r.status_code != 500, (
        "過長的 task_type 造成伺服器 500（欄位長度未驗證）"
    )


def _audit_count(db, action):
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM audit_logs WHERE action_type LIKE %s",
                  (f"%{action}%",))
        return c.fetchone()["n"]


@known_vuln("SEC-17")
def test_login_is_audited(db, make_user):
    """
    登入、登出、掃描、匯出本來都不留紀錄——正好是數位證據系統最該留痕的動作。
    """
    before = _audit_count(db, "登入")
    make_user()                          # fixture 內部會登入一次
    assert _audit_count(db, "登入") > before, "登入沒有寫進 audit_logs"


@known_vuln("SEC-17")
def test_failed_login_is_audited(db, make_user):
    """猜密碼的嘗試也要留痕，不然看不出有沒有人在暴力破解。"""
    import requests
    from conftest import BACKEND

    account, _, _ = make_user()
    before = _audit_count(db, "登入失敗")
    requests.post(f"{BACKEND}/api/login/",
                  json={"account": account, "password": "definitely-wrong"}, timeout=30)
    assert _audit_count(db, "登入失敗") > before, "失敗的登入嘗試沒有留紀錄"


@known_vuln("SEC-17")
def test_logout_is_audited(db, make_user):
    _, _, api = make_user()
    before = _audit_count(db, "登出")
    r = api.post("/api/logout/")
    assert r.status_code == 200, f"沒有登出端點（HTTP {r.status_code}）"
    assert _audit_count(db, "登出") > before, "登出沒有留紀錄"


@known_vuln("SEC-17")
def test_scan_is_audited(admin, db, unique_url):
    before = _audit_count(db, "網址掃描")
    admin.post("/api/scan_target/", json={"url": unique_url})
    assert _audit_count(db, "網址掃描") > before, "網址掃描沒有留紀錄"


@known_vuln("SEC-17")
def test_export_is_audited(admin, db):
    """誰在什麼時候把整批蒐證資料帶走了——這是最該留的一筆。"""
    before = _audit_count(db, "匯出")
    r = admin.get("/api/export/ai_results_excel/")
    assert r.status_code in (200, 404), f"匯出端點異常：{r.status_code}"
    if r.status_code == 404:
        pytest.skip("目前沒有資料可匯出")
    assert _audit_count(db, "匯出") > before, "匯出資料沒有留下稽核紀錄"


@known_vuln("SEC-16")
def test_search_keyword_length_capped(admin):
    """搜尋關鍵字要有長度上限，不然一個請求就能塞任意長的字串進 LIKE。"""
    long_q = "A" * 10000
    for endpoint in ("/api/blacklist/", "/api/whitelist/",
                     "/api/crawler/automated_24h_list/"):
        r = admin.get(endpoint, params={"q": long_q})
        assert r.status_code in (400, 422), (
            f"{endpoint} 接受了 10000 字元的搜尋關鍵字（HTTP {r.status_code}）"
        )


@known_vuln("SEC-16")
@pytest.mark.parametrize("path,body,why", [
    ("/api/nlp/report/",
     {"url": "A" * 100_000, "risk_score": 1, "nlp_keywords": []},
     "url 超過 varchar(768)"),
    ("/api/nlp/report/",
     {"url": "https://t.invalid/", "risk_score": 1,
      "nlp_keywords": ["k" * 100_000]},
     "單一關鍵字超長"),
    ("/api/nlp/report/",
     {"url": "https://t.invalid/", "risk_score": 1,
      "nlp_keywords": ["k"] * 5000},
     "關鍵字數量過多"),
    ("/api/nlp/report/",
     {"url": "https://t.invalid/", "risk_score": 99999, "nlp_keywords": []},
     "分數超出 0~100"),
    ("/api/ai_result/report/",
     {"url": "A" * 100_000, "risk_score": 1, "yolo_objects": []},
     "url 超過 varchar(768)"),
    ("/api/ai_result/report/",
     {"url": "https://t.invalid/", "risk_score": 1,
      "yolo_objects": ["o" * 100_000]},
     "單一物件名稱超長"),
])
def test_ai_report_fields_are_bounded(internal, path, body, why):
    """
    兩個 AI 回報端點的每個欄位都要有上限。

    這兩個模型原本一個上限都沒有——實測 1 MB 的 url 讓請求 500
    （ai_analysis_results.url 是 varchar(768)，寫入時 MySQL 丟 DataError），
    50 個 1 MB 的關鍵字串接後寫進 varchar(500) 的 nlp_details 也是 500。

    注意 List 的 max_length 限的是「項目數量」不是「每個字串的長度」，
    兩者都要擋，第一次只加了前者所以 50×1MB 還是穿過去。

    端點雖然要 internal token，但那個 token 存在五個容器裡，
    任何一個被打下來就等於可以往資料庫塞任意大的資料。
    """
    r = internal.post(path, json=body)
    assert r.status_code in (400, 422), (
        f"{path} 收下了不合理的輸入（{why}），HTTP {r.status_code}"
    )


@known_vuln("SEC-16")
def test_export_date_format_validated(admin):
    """
    匯出的日期參數要驗格式。

    沒驗的話 start_date="' OR 1=1--" 會讓 MySQL 在比較時丟例外、請求 500。
    SQLAlchemy 有參數化所以注入不會成立，但 500 對使用者毫無資訊，
    而且那是「把使用者輸入直接當日期用」的徵兆。
    """
    for bad in ("abc", "' OR 1=1--", "2026-13-45", "2026/08/01"):
        r = admin.get("/api/export/ai_results_excel/", params={"start_date": bad})
        assert r.status_code == 400, (
            f"start_date={bad!r} 沒有被擋（HTTP {r.status_code}）"
        )
    # 正常的日期還是要能用
    r = admin.get("/api/export/ai_results_excel/", params={"start_date": "2026-01-01"})
    assert r.status_code == 200, f"合法日期被擋掉了（HTTP {r.status_code}）"


@known_vuln("SEC-16")
def test_bucket_only_accepts_known_values(admin):
    """
    bucket 要限定合法值。

    不限的話打錯字（bucket=blaclist）會靜靜地回傳全部資料，
    呼叫端以為自己有過濾、其實沒有——那比直接報錯難查得多。
    """
    for bad in ("<script>", "blaclist", "'; DROP TABLE users; --", "all"):
        r = admin.get("/api/crawler/automated_24h_list/",
                      params={"bucket": bad, "limit": 1})
        assert r.status_code == 422, f"bucket={bad!r} 沒有被擋（HTTP {r.status_code}）"

    for good in ("blacklist", "pending"):
        r = admin.get("/api/crawler/automated_24h_list/",
                      params={"bucket": good, "limit": 1})
        assert r.status_code == 200, f"bucket={good} 被誤擋（HTTP {r.status_code}）"

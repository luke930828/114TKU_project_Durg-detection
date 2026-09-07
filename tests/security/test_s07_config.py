"""設定面：CORS、安全標頭、錯誤訊息、網路暴露。"""
from pathlib import Path

import pytest
import requests
import urllib3

# 測試環境用自簽憑證，不驗證是刻意的
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from conftest import BACKEND, WEB, known_vuln

pytestmark = pytest.mark.security

REPO = Path(__file__).resolve().parents[2]


@known_vuln("SEC-06")
def test_cors_not_wildcard():
    """allow_origins=['*'] 搭配 allow_credentials=True 是無效且不安全的組合。"""
    r = requests.options(
        f"{BACKEND}/api/login/",
        headers={"Origin": "https://evil.example.com",
                 "Access-Control-Request-Method": "POST"},
        timeout=30)
    allowed = r.headers.get("access-control-allow-origin", "")
    assert allowed != "*", "CORS 對所有來源開放"
    assert "evil.example.com" not in allowed, "CORS 回應把任意來源都當成允許"


@known_vuln("SEC-06")
def test_cors_origins_env_is_used():
    """.env.local 設了 CORS_ORIGINS=http://localhost:8080，程式卻沒讀。"""
    src = (REPO / "modules/backend/app/main.py").read_text(encoding="utf-8")
    assert "CORS_ORIGINS" in src, (
        "main.py 沒有讀 CORS_ORIGINS，compose 傳進去的設定完全沒有作用"
    )


@known_vuln("SEC-11")
def test_errors_do_not_leak_internals(internal, unique_url):
    """
    crawler.py:115 把 str(e) 直接放進回應。

    用超長的 task_type 觸發：suspect_websites.title 是 String(100)，
    但由不受限的 task_type 組成，寫入時 MySQL 會丟 DataError，
    回應裡就會出現完整的 SQL 語句與參數。

    （不要用「掃描不存在的主機」來觸發——那條路徑會先被歷史紀錄短路掉，
    測不到真正的錯誤處理。）
    """
    # 用 internal 而不是 anon：SEC-01 修好之後 anon 會停在 401，
    # 根本走不到錯誤處理那段，測試會變成綠的但什麼也沒驗到。
    r = internal.post("/api/crawler/report/", json={
        "task_type": "X" * 500, "url": unique_url, "text_content": "x",
        "keywords": [], "product_images_b64": []})

    body = r.text.lower()
    leaks = [w for w in ("traceback", "sqlalchemy", "pymysql", "insert into",
                         "[sql:", "[parameters:", "/app/")
             if w in body]
    assert not leaks, (
        f"錯誤回應洩漏內部細節 {leaks}：\n{r.text[:400]}"
    )


@known_vuln("SEC-15")
@pytest.mark.parametrize("header", [
    "content-security-policy",
    "x-content-type-options",
    "x-frame-options",
])
def test_security_headers_present(header):
    # 放了憑證之後 8080 會 301 轉到 HTTPS，而轉址回應上沒有安全標頭——
    # 要驗的是「使用者最後拿到的那個回應」。不跟著轉址的話，
    # 這三項會在 HTTPS 一啟用就集體退回待修，而程式其實完全正常。
    r = requests.get(f"{WEB}/", timeout=30, allow_redirects=True, verify=False)
    assert header in {k.lower() for k in r.headers}, (
        f"缺少安全標頭 {header}（最終位址 {r.url}）"
    )


@known_vuln("SEC-22")
def test_internal_endpoints_not_exposed_through_nginx():
    """
    backend 綁 127.0.0.1:8000 看起來只有本機能連，但 frontend 是 8080:80
    （綁所有介面），nginx 的 /api/ 未經過濾轉給 backend。
    任何連得到 8080 的人都能打到那三個無驗證端點。
    """
    body = {"url": "https://via-nginx.invalid/x",
            "risk_score": 1, "nlp_keywords": []}
    r = requests.post(f"{WEB}/api/nlp/report/", json=body, timeout=30,
                      allow_redirects=False, verify=False)
    if r.status_code in (301, 302, 307, 308):
        # 301 之後 POST 會被降級成 GET，所以對轉址後的位址重送一次 POST。
        # 不重送的話這個測試會在 HTTPS 模式下拿到 301 就誤判成「有擋住」。
        r = requests.post(r.headers["Location"], json=body, timeout=30, verify=False)
    assert r.status_code in (401, 403, 404), (
        f"透過前端的 8080 可以直接寫入內部回報端點（HTTP {r.status_code}）"
    )


@known_vuln("SEC-20")
def test_no_hardcoded_tailnet_ip():
    """make check 就是在抓這個，目前會失敗。"""
    import re
    hits = []
    for p in (REPO / "modules").rglob("*.py"):
        if "node_modules" in str(p):
            continue
        for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
            if re.search(r"100\.\d+\.\d+\.\d+", line):
                hits.append(f"{p.relative_to(REPO)}:{i}")
    assert not hits, "程式碼裡還有寫死的 tailnet IP 當預設值：\n  " + "\n  ".join(hits)


@known_vuln("SEC-21")
def test_ci_runs_tests():
    """沒有任何 workflow 會跑測試——修好的東西可能默默退回去。"""
    wf = REPO / ".github/workflows"
    files = list(wf.glob("*.yml")) + list(wf.glob("*.yaml")) if wf.exists() else []

    # 不要只認 "pytest" 這個字串。workflow 是透過 make 呼叫測試的，
    # 因為怎麼跑測試的知識該只留在 Makefile 一份，不該在 CI 再抄一遍
    # （抄一遍就是下一個「兩套標準」的來源，BUG-01 就是這樣來的）。
    entrypoints = ("pytest", "make test-integration", "make test-security", "make test ")
    runs_tests = any(
        any(e in f.read_text(encoding="utf-8") for e in entrypoints) for f in files
    )
    assert runs_tests, "CI 沒有任何跑測試的 workflow"


@known_vuln("SEC-21")
def test_app_does_not_run_as_db_root(db):
    """
    應用程式用的資料庫帳號不該是 root。

    刻意在跑起來的系統上驗，而不是去比對 compose 檔裡的字串——
    設定檔寫 DB_USER=drugapp、實際 .env 還是給 root，這種情況只有
    連上去問 CURRENT_USER() 才看得出來。db fixture 用的就是後端那組憑證。
    """
    with db.cursor() as c:
        c.execute("SELECT CURRENT_USER() AS u")
        who = c.fetchone()["u"]
        c.execute("SHOW GRANTS")
        grants = [list(row.values())[0] for row in c.fetchall()]

    assert not who.startswith("root@"), f"應用程式直接用 MySQL root 連線（{who}）"

    too_much = [g for g in grants
                if "ALL PRIVILEGES ON *.*" in g.upper() or "WITH GRANT OPTION" in g.upper()]
    assert not too_much, (
        f"{who} 拿到了全域權限，跟 root 沒有差別：{too_much}"
    )

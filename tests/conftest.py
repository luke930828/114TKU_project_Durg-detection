"""
共用 fixtures 與 known_vuln 機制。

測試一律斷言「正確的行為」。已知漏洞用 @known_vuln("SEC-xx") 標記，
它內部套的是 xfail(strict=False)，所以：

    漏洞還在  → XFAIL，pytest 整體仍是 exit 0（CI 不會被既有問題卡死）
    漏洞修好  → XPASS，報告自動改標「已修復」

修好之後不用回來改測試，這是刻意的設計。
"""
import os
import sys
import time
import uuid
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent))
import vulns  # noqa: E402

BACKEND = os.getenv("TEST_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")
WEB = os.getenv("TEST_WEB_URL", "http://127.0.0.1:8080").rstrip("/")

# 初始管理員密碼由 ADMIN_INITIAL_PASSWORD 決定（見 main.py 的 _initial_admin_password）。
# 測試環境在 docker-compose.test.yml 固定成一個已知值，這裡讀同一個變數。
DEFAULT_ADMIN = ("admin", os.getenv("TEST_ADMIN_PASSWORD", "IntegrationTest!2026"))

# 服務間驗證的共用 token（SEC-01）。Makefile 的 PYTEST 目標會把 .env 整份
# source 進來，所以這裡讀得到跟後端容器同一組值。
INTERNAL_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")


# ============================================================
# known_vuln
# ============================================================
def known_vuln(vuln_id):
    """把測試標成「已知漏洞」。條目定義在 vulns.py。"""
    meta = vulns.get(vuln_id)

    def deco(fn):
        fn = pytest.mark.vuln_id(vuln_id)(fn)
        fn = pytest.mark.security(fn)
        return pytest.mark.xfail(
            reason=f"{vuln_id} [{meta['severity']}] {meta['title']}",
            strict=False,
        )(fn)

    return deco


# ============================================================
# 連線
# ============================================================
class Api:
    """薄薄一層 requests 包裝，帶 base url 與預設 token。"""

    def __init__(self, base, token=None, internal_token=None):
        self.base = base
        self.token = token
        # 機器對機器端點用的 token，跟使用者的 JWT 是兩回事，可以同時帶或都不帶。
        self.internal_token = internal_token
        self.s = requests.Session()

    def _headers(self, extra, auth):
        h = dict(extra or {})
        lower = {k.lower() for k in h}
        if auth and self.token and "x-token" not in lower:
            h["X-Token"] = self.token
        if self.internal_token and "x-internal-token" not in lower:
            h["X-Internal-Token"] = self.internal_token
        return h

    def request(self, method, path, *, auth=True, headers=None, **kw):
        kw.setdefault("timeout", 30)
        return self.s.request(
            method, f"{self.base}{path}", headers=self._headers(headers, auth), **kw
        )

    def get(self, p, **kw):
        return self.request("GET", p, **kw)

    def post(self, p, **kw):
        return self.request("POST", p, **kw)

    def put(self, p, **kw):
        return self.request("PUT", p, **kw)

    def delete(self, p, **kw):
        return self.request("DELETE", p, **kw)

    def with_token(self, token):
        return Api(self.base, token, internal_token=self.internal_token)


def _login(account, password):
    r = requests.post(
        f"{BACKEND}/api/login/",
        json={"account": account, "password": password},
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(f"登入 {account} 失敗：HTTP {r.status_code} {r.text[:200]}")
    return r.json()["access_token"]


@pytest.fixture(scope="session")
def stack_ready():
    """
    整組服務沒起來就直接停掉，不要讓幾十個測試各自逾時。

    刻意「不」用 autouse：有些測試只是靜態檢查程式碼或設定檔
    （例如掃有沒有寫死的 IP、compose 有沒有設記憶體上限），
    那些不需要服務跑著也該能驗。真正要打 API 的 fixture 才依賴這個。
    """
    # backend 之外也要等 stub。只等 backend 的話，剛 up -d 完就跑測試
    # 會撞到 stub 還沒聽埠——那種失敗每次位置都不一樣，最難查。
    targets = {
        "backend": f"{BACKEND}/health",
        "nlp-stub": "http://127.0.0.1:18000/health",
        "yolo-stub": "http://127.0.0.1:15000/health",
        "crawler-stub": "http://127.0.0.1:18001/health",
    }
    deadline = time.time() + 90
    pending = dict(targets)
    last = ""
    while pending and time.time() < deadline:
        for name, url in list(pending.items()):
            try:
                if requests.get(url, timeout=3).status_code == 200:
                    pending.pop(name)
            except requests.RequestException as e:
                last = f"{name}: {e}"
        if pending:
            time.sleep(2)

    if pending:
        pytest.exit(
            f"\n這些服務起不來：{'、'.join(pending)}\n"
            f"最後錯誤：{last}\n"
            f"先跑 make test-up，再看 make logs。\n",
            returncode=3,
        )


def _unfreeze_admin_via_db():
    """
    保險絲：直接從資料庫把預設 admin 解凍。

    測試如果不小心把 admin 凍結（它自己的 token 會立刻失效，就再也解不開），
    整套測試會從那一刻起全部失敗，而且失敗方式很有誤導性——
    看起來像「漏洞修好了」，其實只是登不進去。這裡在每輪開始前先確保它是通的。
    """
    try:
        import pymysql
    except ImportError:
        return False
    try:
        conn = pymysql.connect(
            host=os.getenv("TEST_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("TEST_DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.environ["DB_PASSWORD"],
            database=os.getenv("TEST_DB_NAME", "drug_prevention_test"),
            autocommit=True,
        )
    except Exception:
        return False
    with conn:
        with conn.cursor() as c:
            c.execute(
                "UPDATE users SET is_active=1, is_deleted=0 WHERE account=%s",
                (DEFAULT_ADMIN[0],),
            )
            fixed = c.rowcount > 0
            # 登入失敗次數是從 audit_logs 算的（見 routers/auth.py），
            # 所以「解鎖」就是把那些失敗紀錄清掉。
            c.execute(
                "DELETE a FROM audit_logs a JOIN users u ON u.user_id = a.user_id "
                "WHERE u.account = %s AND a.action_type IN ('登入失敗', '登入遭鎖定')",
                (DEFAULT_ADMIN[0],),
            )
            return fixed or c.rowcount > 0


@pytest.fixture(scope="session")
def admin_token(stack_ready):
    try:
        return _login(*DEFAULT_ADMIN)
    except RuntimeError:
        if _unfreeze_admin_via_db():
            print("\n⚠️  預設 admin 之前被測試鎖住了，已直接從資料庫解開。")
            return _login(*DEFAULT_ADMIN)
        raise


@pytest.fixture
def anon(stack_ready):
    """未登入的客戶端。"""
    return Api(BACKEND)


@pytest.fixture
def admin(admin_token):
    return Api(BACKEND, admin_token)


@pytest.fixture
def internal(stack_ready):
    """
    扮演 crawler / nlp / yolo 這些內部服務的客戶端，帶 X-Internal-Token。

    整合測試要模擬「引擎回報結果」這個正常流程，所以必須帶 token；
    資安測試要證明「外人不能回報」，那邊用 anon（不帶）。兩個 fixture
    刻意分開，免得哪天有人為了讓測試變綠就把 token 加進 anon——
    那會讓 SEC-01 的四個測試全部失去意義。
    """
    if not INTERNAL_TOKEN:
        pytest.fail(
            "INTERNAL_API_TOKEN 沒讀到。Makefile 的 PYTEST 會 source .env，"
            "直接跑 pytest 的話要自己 export。"
        )
    return Api(BACKEND, internal_token=INTERNAL_TOKEN)


@pytest.fixture
def web(stack_ready):
    """打 nginx（8080），驗證前端與 /api proxy。"""
    return Api(WEB)


@pytest.fixture
def unique_url():
    """每個測試用不同網址，測試之間不會互相污染。"""
    return f"https://itest-{uuid.uuid4().hex[:12]}.invalid/p"


@pytest.fixture
def make_user(admin):
    """建立一般人員，測試結束自動刪除。回傳 (account, password, Api)。"""
    created = []

    def _make(role="一般人員", password="Test1234!@#$", account=None):
        account = account or f"ituser_{uuid.uuid4().hex[:8]}"
        r = admin.post(
            "/api/users/",
            json={
                "account": account,
                "password": password,
                "role": role,
                "department": "整合測試",
            },
        )
        assert r.status_code == 200, f"建立測試帳號失敗：{r.status_code} {r.text[:200]}"
        created.append(account)
        return account, password, Api(BACKEND, _login(account, password))

    yield _make

    users = admin.get("/api/users/")
    if users.status_code == 200:
        by_account = {u["account"]: u["id"] for u in users.json()}
        for acc in created:
            if acc in by_account:
                admin.delete(f"/api/users/{by_account[acc]}")


@pytest.fixture(scope="session")
def db(stack_ready):
    """直接連資料庫，用來確認資料真的落庫（不是只有 API 回 200）。"""
    pymysql = pytest.importorskip("pymysql")
    try:
        conn = pymysql.connect(
            host=os.getenv("TEST_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("TEST_DB_PORT", "3306")),
            user=os.getenv("DB_USER", "root"),
            password=os.environ["DB_PASSWORD"],
            database=os.getenv("TEST_DB_NAME", "drug_prevention_test"),
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
        )
    except Exception as e:
        # 刻意用 fail 而不是 skip。靜默跳過比失敗更危險——依賴 db 的測試
        # 不會跑，報告就把已經修好的項目顯示成「待修」或「測試異常」，
        # 而畫面上只是多幾個 s，沒有人會注意到。
        # （實際發生過：少了 cryptography 這個相依，14 個測試靜靜跳過，
        #   SEC-02 與 SEC-17 從已修復退回待修。）
        pytest.fail(
            f"連不上測試資料庫：{e}\n"
            f"  資料庫是驗證「資料真的落庫」的唯一手段，連不上就不該當作通過。\n"
            f"  檢查：make test-up 有沒有跑完、tests/requirements-test.txt 是否都裝了。"
        )
    yield conn
    conn.close()


# ============================================================
# 報告產生
# ============================================================
_results = {}   # vuln_id -> "open" | "fixed" | "error"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    out = yield
    rep = out.get_result()
    m = item.get_closest_marker("vuln_id")
    if m:
        rep.vuln_id = m.args[0]
        rep.vuln_test = item.nodeid


def pytest_runtest_logreport(report):
    vid = getattr(report, "vuln_id", None)
    if not vid or report.when != "call":
        return
    if report.skipped and hasattr(report, "wasxfail"):
        status = "open"            # 斷言失敗 = 漏洞還在
    elif report.passed:
        status = "fixed"           # 斷言成立 = 已修復（xpass 或一般 pass）
    else:
        status = "error"           # 測試自己壞了
    # 同一個 ID 可能有多個測試；只要有一個還開著就算開著
    prev = _results.get(vid, {}).get("status")
    if prev != "open":
        _results[vid] = {"status": status, "test": getattr(report, "vuln_test", "")}


def pytest_sessionfinish(session, exitstatus):
    if not _results:
        return
    out = Path(__file__).parent / "report" / "SECURITY_REPORT.md"
    out.parent.mkdir(exist_ok=True)

    from datetime import datetime

    lines = [
        "# 資安稽核報告",
        "",
        f"產生時間：{datetime.now():%Y-%m-%d %H:%M:%S}　"
        f"（由 `pytest` 自動產生，每次跑測試覆寫）",
        "",
        "本報告由 `tests/security/` 的測試結果直接產生。每一項發現都對應一個實際執行過的測試，",
        "測試斷言的是「正確的行為」——所以「待修」代表該測試現在失敗，「已修復」代表它通過了。",
        "",
    ]

    counts = {}
    for vid, meta in vulns.VULNS.items():
        st = _results.get(vid, {}).get("status", "untested")
        counts.setdefault(meta["severity"], {"open": 0, "fixed": 0, "untested": 0})
        counts[meta["severity"]][st if st in ("open", "fixed") else "untested"] += 1

    lines += ["## 摘要", "", "| 嚴重度 | 待修 | 已修復 | 未涵蓋 |", "|---|---|---|---|"]
    for sev in vulns.SEVERITY_ORDER:
        c = counts.get(sev, {"open": 0, "fixed": 0, "untested": 0})
        lines.append(f"| {sev} | {c['open']} | {c['fixed']} | {c['untested']} |")
    lines.append("")

    icon = {"open": "🔴 待修", "fixed": "✅ 已修復", "untested": "⚪ 未涵蓋", "error": "⚠️ 測試異常"}
    for sev in vulns.SEVERITY_ORDER:
        items = [(k, v) for k, v in vulns.VULNS.items() if v["severity"] == sev]
        if not items:
            continue
        lines += [f"## {sev}", ""]
        for vid, meta in items:
            res = _results.get(vid, {})
            st = res.get("status", "untested")
            lines += [
                f"### {vid}　{meta['title']}",
                "",
                f"**狀態**：{icon[st]}　　**位置**：`{meta['where']}`",
                "",
                f"**影響**　{meta['impact']}",
                "",
                f"**建議修法**　{meta['fix']}",
                "",
            ]
            if res.get("test"):
                lines += [f"**驗證測試**　`{res['test']}`", ""]
            lines.append("---")
            lines.append("")

    lines += [
        "## 後續建議",
        "",
        "1. 依 Critical → High → Medium 順序修，每修一項重跑 `make test-security`，"
        "該項會從 🔴 自動變成 ✅。",
        "2. 加一個 PR 觸發的 CI workflow 跑這套測試，避免修好的東西再退回去（SEC-21）。",
        "3. `tests/integration/` 的失敗不是漏洞，是模組間介面契約真的斷了，要優先處理。",
        "",
    ]

    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"\n📄 稽核報告已產生：{out}")

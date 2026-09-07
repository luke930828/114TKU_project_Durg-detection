"""密碼政策、雜湊強度、暴力破解防護。"""
import hashlib
import time

import pytest
import requests
from conftest import BACKEND, DEFAULT_ADMIN, known_vuln

pytestmark = pytest.mark.security

WEAK = ["", "1", "123", "abc", "password", "12345678", "aaaaaaaa"]


@known_vuln("SEC-03")
def test_hardcoded_default_password_gone():
    """
    原本 main.py 寫死 admin / password123，每次啟動都會重建。
    那組帳密在公開的 repo 裡，等於任何看得到程式碼的人都有管理員權限。
    """
    r = requests.post(f"{BACKEND}/api/login/",
                      json={"account": "admin", "password": "password123"}, timeout=30)
    assert r.status_code == 401, "寫在原始碼裡的預設帳密 admin/password123 仍然可以登入"


@known_vuln("SEC-03")
def test_no_password_literal_in_source():
    """程式碼裡不該再出現任何可直接拿來登入的密碼字面值。"""
    import pathlib
    app = pathlib.Path(__file__).resolve().parents[2] / "modules/backend/app"
    hits = []
    for f in list(app.glob("*.py")) + list(app.glob("routers/*.py")):
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if "password123" in line and not line.lstrip().startswith("#"):
                hits.append(f"{f.name}:{i}")
    assert not hits, "原始碼裡還有寫死的密碼：" + ", ".join(hits)


@known_vuln("SEC-08")
@pytest.mark.parametrize("pw", WEAK, ids=[repr(p) for p in WEAK])
def test_weak_password_rejected_by_api(admin, pw):
    """
    密碼強度規則只寫在前端 inputSecurity.ts，直接打 API 就繞過。
    後端的 UserCreate 沒有任何 field_validator。
    """
    import uuid
    r = admin.post("/api/users/", json={
        "account": f"weak_{uuid.uuid4().hex[:8]}", "password": pw,
        "role": "一般人員", "department": "測試"})
    if r.status_code == 200:
        # 清掉，不要留垃圾帳號
        users = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}
        for acc, uid in users.items():
            if acc.startswith("weak_"):
                admin.delete(f"/api/users/{uid}")
    assert r.status_code in (400, 422), f"後端接受了弱密碼 {pw!r}"


@known_vuln("SEC-02")
def test_password_not_stored_as_plain_sha256(db, make_user):
    """
    無鹽 SHA-256 可以直接用彩虹表反查。
    這裡直接算一次 sha256，跟資料庫裡的值比對——一樣就代表確實是無鹽單輪。
    """
    account, password, _ = make_user()
    with db.cursor() as c:
        c.execute("SELECT password_hash FROM users WHERE account=%s", (account,))
        stored = c.fetchone()["password_hash"]

    naive = hashlib.sha256(password.encode()).hexdigest()
    assert stored != naive, (
        "password_hash 就是明文密碼的 sha256（無鹽、單輪），可用彩虹表反查。"
        "requirements.txt 已經有 passlib 與 bcrypt，只是從沒 import。"
    )


@known_vuln("SEC-02")
def test_password_hash_looks_like_modern_kdf(db, make_user):
    """bcrypt/argon2 的雜湊會有 $2b$ / $argon2 這種前綴，SHA-256 是 64 個十六進位字元。"""
    account, _, _ = make_user()
    with db.cursor() as c:
        c.execute("SELECT password_hash FROM users WHERE account=%s", (account,))
        stored = c.fetchone()["password_hash"]
    assert stored.startswith(("$2a$", "$2b$", "$2y$", "$argon2", "$pbkdf2")), (
        f"雜湊格式不是現代 KDF：{stored[:16]}...（長度 {len(stored)}）"
    )


@known_vuln("SEC-10")
def test_login_rate_limited(make_user):
    """
    連續失敗登入應該被限制或鎖定。

    ⚠️ 用拋棄式帳號，不要拿預設 admin 來試——鎖定是照帳號算的，
    把 admin 鎖住會讓後面所有需要管理員的測試一起失敗，
    而且要等 15 分鐘才會自己解開。
    """
    account, _, _ = make_user()
    codes = []
    for i in range(10):
        r = requests.post(f"{BACKEND}/api/login/",
                          json={"account": account, "password": f"WrongPass{i}!"},
                          timeout=30)
        codes.append(r.status_code)
        if r.status_code == 429:
            break
    assert 429 in codes, (
        f"連續 {len(codes)} 次錯誤密碼全部回 {set(codes)}，沒有速率限制或鎖定機制"
    )


@known_vuln("SEC-10")
def test_login_does_not_leak_account_existence(admin, make_user):
    """
    帳號不存在、帳號被凍結、密碼錯誤——都該回一模一樣的訊息，
    否則可以用來列舉系統裡有哪些帳號。
    """
    account, password, _ = make_user()
    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]
    admin.put(f"/api/users/{uid}/toggle-status")          # 凍結

    def msg(acc, pw):
        r = requests.post(f"{BACKEND}/api/login/",
                          json={"account": acc, "password": pw}, timeout=30)
        return r.json().get("detail", "")

    frozen = msg(account, password)
    unknown = msg("definitely_no_such_account_xyz", "whatever")
    admin.put(f"/api/users/{uid}/toggle-status")          # 解凍

    assert frozen == unknown, (
        f"回應訊息不同，可用來判斷帳號是否存在：\n"
        f"  被凍結的帳號 → {frozen!r}\n  不存在的帳號 → {unknown!r}"
    )

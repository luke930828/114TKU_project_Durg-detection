"""身分驗證：token 的簽發、驗證與撤銷。"""
import time

import jwt
import pytest
import requests
from conftest import BACKEND, Api, _login, known_vuln

pytestmark = pytest.mark.security

PROTECTED = "/api/crawler/automated_24h_list/"


# ---------- 這些應該本來就是對的 ----------
def test_no_token_rejected(anon):
    assert anon.get(PROTECTED, auth=False).status_code in (401, 422)


def test_garbage_token_rejected(anon):
    r = anon.get(PROTECTED, auth=False, headers={"X-Token": "not-a-jwt"})
    assert r.status_code == 401


def test_token_signed_with_wrong_secret_rejected(anon):
    forged = jwt.encode({"sub": "admin"}, "wrong-secret-key", algorithm="HS256")
    r = anon.get(PROTECTED, auth=False, headers={"X-Token": forged})
    assert r.status_code == 401, "用別的密鑰簽的 token 竟然通過了"


def test_alg_none_token_rejected(anon):
    """alg=none 是 JWT 最經典的繞過手法。"""
    forged = jwt.encode({"sub": "admin"}, key="", algorithm="none")
    r = anon.get(PROTECTED, auth=False, headers={"X-Token": forged})
    assert r.status_code == 401, "alg=none 的偽造 token 被接受了"


def test_token_for_nonexistent_user_rejected(anon, admin_token):
    """簽章正確但使用者不存在，也不該放行。"""
    import os
    secret = os.environ["JWT_SECRET_KEY"]
    forged = jwt.encode({"sub": "ghost_user_not_in_db"}, secret, algorithm="HS256")
    r = anon.get(PROTECTED, auth=False, headers={"X-Token": forged})
    assert r.status_code == 401


# ---------- 已知漏洞 ----------
@known_vuln("SEC-04")
def test_token_has_expiry(admin_token):
    """token 應該有 exp，不然外洩就是永久通行證。"""
    payload = jwt.decode(admin_token, options={"verify_signature": False})
    assert "exp" in payload, f"JWT 沒有 exp，永不過期：{payload}"


def test_expired_token_rejected(anon):
    """
    手動簽一張帶 exp 且已過期的 token，應該被擋。

    這條是「正向對照」，本來就會通過——PyJWT 只要看到 exp 就會驗。
    真正的問題是後端簽發時根本不放 exp，那由 test_token_has_expiry 負責。
    所以這條不掛 known_vuln，免得誤報 SEC-04 已修復。
    """
    import os
    secret = os.environ["JWT_SECRET_KEY"]
    expired = jwt.encode(
        {"sub": "admin", "exp": int(time.time()) - 3600}, secret, algorithm="HS256")
    r = anon.get(PROTECTED, auth=False, headers={"X-Token": expired})
    assert r.status_code == 401, "過期的 token 仍然可以使用"


@known_vuln("SEC-05")
def test_deleted_user_token_stops_working(admin, make_user):
    """
    被刪除的人，手上的 token 應該立刻失效。
    delete_user 只設 is_deleted=True，而 get_current_user 只查 is_active，
    所以被刪掉的人拿舊 token 還是暢行無阻。
    """
    account, _, api = make_user()
    assert api.get(PROTECTED).status_code == 200

    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]
    assert admin.delete(f"/api/users/{uid}").status_code == 200

    r = api.get(PROTECTED)
    assert r.status_code in (401, 403), (
        f"帳號已刪除，舊 token 仍可存取（HTTP {r.status_code}）"
    )


def test_deleted_user_cannot_login_again(admin, make_user):
    """
    被刪除的人不能重新登入——auth.py 有查 is_deleted，這條本來就會過。
    SEC-05 的破口在 token 那條路徑（get_current_user 沒查），
    由上面的 test_deleted_user_token_stops_working 負責，這條不掛 known_vuln。
    """
    account, password, _ = make_user()
    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]
    admin.delete(f"/api/users/{uid}")
    r = requests.post(f"{BACKEND}/api/login/",
                      json={"account": account, "password": password}, timeout=30)
    assert r.status_code == 401

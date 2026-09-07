"""登入、token、帳號狀態。"""
import pytest
import requests
from conftest import BACKEND, DEFAULT_ADMIN, Api, _login

pytestmark = pytest.mark.integration


def test_login_returns_token(anon):
    r = anon.post("/api/login/", auth=False,
                  json={"account": DEFAULT_ADMIN[0], "password": DEFAULT_ADMIN[1]})
    assert r.status_code == 200
    assert r.json()["access_token"]


def test_login_wrong_password(anon):
    r = anon.post("/api/login/", auth=False,
                  json={"account": DEFAULT_ADMIN[0], "password": "definitely-wrong"})
    assert r.status_code == 401


def test_login_unknown_account(anon):
    r = anon.post("/api/login/", auth=False,
                  json={"account": "no_such_user_here", "password": "x"})
    assert r.status_code == 401


def test_token_grants_access(admin):
    assert admin.get("/api/users/").status_code == 200


def test_no_token_rejected(anon):
    r = anon.get("/api/users/", auth=False)
    assert r.status_code in (401, 422)


def test_normal_user_can_login_and_scan(make_user):
    account, _, api = make_user()
    r = api.get("/api/crawler/automated_24h_list/")
    assert r.status_code == 200


def test_frozen_account_cannot_login(admin, make_user):
    account, password, _ = make_user()
    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]

    assert admin.put(f"/api/users/{uid}/toggle-status").status_code == 200
    r = requests.post(f"{BACKEND}/api/login/",
                      json={"account": account, "password": password}, timeout=30)
    assert r.status_code == 401, "帳號被凍結後仍然登得進去"

    admin.put(f"/api/users/{uid}/toggle-status")   # 解凍，讓 fixture 能清乾淨


def test_frozen_account_token_stops_working(admin, make_user):
    """已經拿到 token 的人被凍結之後，token 應該立刻失效。"""
    account, _, api = make_user()
    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]
    assert api.get("/api/crawler/automated_24h_list/").status_code == 200

    admin.put(f"/api/users/{uid}/toggle-status")
    r = api.get("/api/crawler/automated_24h_list/")
    assert r.status_code == 403, "帳號凍結後舊 token 還能用"

    admin.put(f"/api/users/{uid}/toggle-status")

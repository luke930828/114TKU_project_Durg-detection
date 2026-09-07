"""人員管理與稽核日誌。"""
import pytest

pytestmark = pytest.mark.integration


def test_create_and_list(admin, make_user):
    account, _, _ = make_user()
    assert account in [u["account"] for u in admin.get("/api/users/").json()]


def test_duplicate_account_rejected(admin, make_user):
    account, _, _ = make_user()
    r = admin.post("/api/users/", json={
        "account": account, "password": "Test1234!@#$",
        "role": "一般人員", "department": "整合測試"})
    assert r.status_code == 400


def test_role_change_takes_effect(admin, make_user):
    account, password, api = make_user()
    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]

    assert api.get("/api/users/").status_code == 403, "一般人員本來就不該看得到人員清單"
    assert admin.put(f"/api/users/{uid}/role",
                     json={"role": "系統管理員"}).status_code == 200

    from conftest import BACKEND, Api, _login
    promoted = Api(BACKEND, _login(account, password))
    assert promoted.get("/api/users/").status_code == 200, "升為管理員後仍然存取不到"


def test_soft_delete_hides_user(admin, make_user):
    account, _, _ = make_user()
    uid = {u["account"]: u["id"] for u in admin.get("/api/users/").json()}[account]
    assert admin.delete(f"/api/users/{uid}").status_code == 200
    assert account not in [u["account"] for u in admin.get("/api/users/").json()]


def test_cannot_delete_self(admin):
    me = next(u for u in admin.get("/api/users/").json() if u["account"] == "admin")
    r = admin.delete(f"/api/users/{me['id']}")
    assert r.status_code == 400


def test_audit_log_records_user_creation(admin, make_user, db):
    account, _, _ = make_user()
    r = admin.get("/api/users/audit-logs")
    assert r.status_code == 200
    logs = r.json()["data"]
    assert any(account in (l.get("details") or "") for l in logs), \
        "新增人員沒有留下稽核紀錄"


def test_audit_log_paginates(admin, make_user):
    """
    以前這個端點寫死 .limit(100) 又沒有分頁，超過 100 筆之後更早的紀錄
    就再也查不到——對要留存數位證據的系統來說，那等於稽核軌跡是斷的。
    """
    for _ in range(3):
        make_user()

    p1 = admin.get("/api/users/audit-logs", params={"page": 1, "limit": 2})
    assert p1.status_code == 200
    body = p1.json()
    assert len(body["data"]) == 2
    assert body["pagination"]["limit"] == 2
    assert body["pagination"]["current_page"] == 1
    assert body["pagination"]["total_count"] >= 3

    p2 = admin.get("/api/users/audit-logs", params={"page": 2, "limit": 2})
    assert p2.status_code == 200
    ids1 = {l["log_id"] for l in body["data"]}
    ids2 = {l["log_id"] for l in p2.json()["data"]}
    assert not (ids1 & ids2), "第一頁與第二頁的內容重疊了"


@pytest.mark.parametrize("params", [
    {"page": 0}, {"page": -1}, {"limit": 0}, {"limit": 99999},
], ids=["page=0", "page=-1", "limit=0", "limit=99999"])
def test_audit_log_rejects_bad_pagination(admin, params):
    assert admin.get("/api/users/audit-logs", params=params).status_code == 422


def test_audit_log_persisted_in_db(admin, make_user, db):
    account, _, _ = make_user()
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM audit_logs WHERE details LIKE %s",
                  (f"%{account}%",))
        assert c.fetchone()["n"] >= 1, "稽核紀錄沒有真的寫進資料庫"

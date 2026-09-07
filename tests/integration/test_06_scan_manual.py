"""手動掃描的三條路徑：白名單 / 歷史紀錄 / 派發爬蟲。"""
import pytest
import requests
from helpers import find_result, wait_both_engines, wait_for

pytestmark = pytest.mark.integration


def test_new_url_dispatches_to_crawler(admin, unique_url):
    r = admin.post("/api/scan_target/", json={"url": unique_url})
    assert r.status_code == 200
    assert r.json()["status"] == "processing"

    def crawler_got_it():
        calls = requests.get("http://127.0.0.1:18001/__stub/calls", timeout=10).json()
        return [c for c in calls["calls"] if c["payload"].get("url") == unique_url]

    assert wait_for(crawler_got_it, what="爬蟲收到手動掃描任務")


def test_completed_url_returns_history(admin, unique_url):
    """第二次掃同一個網址，應該直接回歷史紀錄，不再派發。"""
    admin.post("/api/scan_target/", json={"url": unique_url})
    wait_both_engines(admin, unique_url)

    r = admin.post("/api/scan_target/", json={"url": unique_url})
    assert r.status_code == 200
    body = r.json()
    assert body["source"] == "history", f"沒有走歷史紀錄：{body}"
    assert body["data"]["risk_score"] == 68


def test_requires_authentication(anon, unique_url):
    r = anon.post("/api/scan_target/", auth=False, json={"url": unique_url})
    assert r.status_code in (401, 422)

"""SSRF：/api/scan_target/ 把使用者提供的網址直接交給爬蟲抓取。"""
import pytest
import requests
from conftest import known_vuln

pytestmark = pytest.mark.security

DANGEROUS = [
    "http://169.254.169.254/latest/meta-data/",   # 雲端 metadata
    "http://127.0.0.1:8000/api/users/",           # 打自己
    "http://localhost:3306/",                     # 內部資料庫
    "http://10.0.0.1/",                           # 私有網段
    "http://192.168.1.1/",                        # 私有網段
    "http://172.16.0.1/",                         # 私有網段
    "file:///etc/passwd",                         # 本機檔案
    "gopher://127.0.0.1:3306/_",                  # 非 http scheme
    "http://backend:8000/api/crawler/report/",    # 內部服務名
]


@known_vuln("SEC-07")
@pytest.mark.parametrize("url", DANGEROUS, ids=range(len(DANGEROUS)))
def test_dangerous_url_rejected(admin, url):
    """
    正確行為：scheme 只允許 http/https，主機不得為 localhost 或私有網段。
    前端 URLAnalysis.tsx 有做格式檢查，但直接打 API 就繞過了。
    """
    r = admin.post("/api/scan_target/", json={"url": url})
    assert r.status_code in (400, 422), f"危險網址被接受了：{url}（HTTP {r.status_code}）"


@known_vuln("SEC-07")
def test_dangerous_url_not_forwarded_to_crawler(admin):
    """
    更關鍵的一步：確認惡意網址沒有真的被送到爬蟲去抓。

    ⚠️ 網址每次都要不一樣。scan.py 會先查歷史紀錄，
    上一輪跑過的網址第二次就直接回 history，根本不會走到派發那一步，
    測試就會假性通過（誤報成「已修復」）。
    """
    import uuid
    target = f"http://169.254.169.254/latest/meta-data/{uuid.uuid4().hex[:8]}"
    requests.post("http://127.0.0.1:18001/__stub/reset", timeout=10)
    admin.post("/api/scan_target/", json={"url": target})

    calls = requests.get("http://127.0.0.1:18001/__stub/calls", timeout=10).json()
    forwarded = [c for c in calls["calls"] if c["payload"].get("url") == target]
    assert not forwarded, (
        f"後端把雲端 metadata 位址轉給爬蟲去抓了：{target}"
    )


def test_malformed_url_rejected(admin):
    """完全不成形的字串應該被擋（這條期待本來就會過或至少不會 500）。"""
    r = admin.post("/api/scan_target/", json={"url": "not a url at all"})
    assert r.status_code != 500, "畸形網址造成伺服器 500"

"""
三個機器對機器端點：/api/crawler/report/、/api/nlp/report/、/api/ai_result/report/

這是整份清單裡最嚴重的一項。INTERNAL_API_TOKEN 被送進全部五個容器，
但後端沒有任何一行程式碼讀它——服務間驗證從來沒有接上。
"""
import pytest
from conftest import known_vuln
from helpers import find_result, wait_for

pytestmark = pytest.mark.security

REPORTS = [
    ("/api/crawler/report/",
     {"task_type": "attack", "url": "", "text_content": "x",
      "keywords": [], "product_images_b64": []}),
    ("/api/nlp/report/",
     {"url": "", "risk_score": 0, "nlp_keywords": []}),
    ("/api/ai_result/report/",
     {"url": "", "risk_score": 0, "yolo_objects": []}),
]


@known_vuln("SEC-01")
@pytest.mark.parametrize("path,body", REPORTS, ids=[p for p, _ in REPORTS])
def test_report_endpoints_require_auth(anon, unique_url, path, body):
    body = dict(body, url=unique_url)
    r = anon.post(path, auth=False, json=body)
    assert r.status_code in (401, 403), (
        f"{path} 不帶任何憑證就能寫入（HTTP {r.status_code}）"
    )


@known_vuln("SEC-01")
def test_attacker_cannot_rewrite_risk_score(anon, internal, admin, unique_url):
    """
    先讓系統把某網址判為高風險，再模擬攻擊者把它洗成 0 分。
    這等於任何人都能把已知的毒品網站從黑名單洗白。

    佈置階段用 internal（扮演真的引擎），攻擊階段才用 anon。
    兩段都用 anon 的話，SEC-01 修好之後連佈置都會被擋掉，
    測試會因為「找不到那筆紀錄」而失敗——看起來像漏洞還在，其實剛好相反。
    """
    internal.post("/api/nlp/report/",
                  json={"url": unique_url, "risk_score": 95, "nlp_keywords": ["毒品"]})
    internal.post("/api/ai_result/report/",
                  json={"url": unique_url, "risk_score": 95, "yolo_objects": ["毒品"]})
    row = wait_for(lambda: find_result(admin, unique_url), what="高風險紀錄")
    assert row["risk_score"] >= 90

    # 攻擊者出手
    r1 = anon.post("/api/nlp/report/", auth=False,
                   json={"url": unique_url, "risk_score": 0, "nlp_keywords": ["安全"]})
    r2 = anon.post("/api/ai_result/report/", auth=False,
                   json={"url": unique_url, "risk_score": 0, "yolo_objects": []})

    after = find_result(admin, unique_url)
    assert (r1.status_code in (401, 403) and r2.status_code in (401, 403)) \
        or after["risk_score"] >= 90, (
        f"未驗證的請求把風險分數從 {row['risk_score']} 洗成 {after['risk_score']}"
    )


@known_vuln("SEC-01")
def test_blacklist_evasion_via_magic_string(anon, admin, unique_url):
    """
    BUG-04：crawler.py:39 只要 text_content 含「非毒品」就直接歸檔為 0 分。
    在一個無驗證的端點上，這等於一個請求就能規避黑名單。
    """
    r = anon.post("/api/crawler/report/", auth=False, json={
        "task_type": "evasion", "url": unique_url,
        "text_content": "非毒品", "keywords": [], "product_images_b64": []})
    assert r.status_code in (401, 403), (
        "未驗證的請求可以送出「非毒品」關鍵字，讓任意網址直接被歸檔為 0 分"
    )


@known_vuln("SEC-01")
def test_internal_token_is_actually_checked(anon, unique_url):
    """
    帶一個錯的 INTERNAL_API_TOKEN 應該被拒。
    現在無論帶什麼、不帶什麼都會通過，代表這個變數根本沒被讀。
    """
    r = anon.post("/api/nlp/report/", auth=False,
                  headers={"X-Internal-Token": "obviously-wrong-token"},
                  json={"url": unique_url, "risk_score": 50, "nlp_keywords": []})
    assert r.status_code in (401, 403), (
        "帶錯誤的內部 token 仍然寫入成功——INTERNAL_API_TOKEN 沒有被驗證"
    )

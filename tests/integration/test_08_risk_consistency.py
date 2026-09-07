"""
風險分級。

BUG-01 原本是同一筆資料有兩套門檻：utils.py 用 74/35 算 risk_level，
crawler.py 用 85/75 算 24 小時清單的 status，68 分的資料在報表上是
「中風險」、在清單上卻是最低的 Monitored。

現在兩邊都以 risk_level 為準，門檻只定義在 utils.py 一處。

分級規則本身也依 217 筆人工標註的真實網頁重新設計過
（見 data/eval_sample/）：舊的加權平均 0.6n+0.4y>74 會漏掉 35% 的
毒品網站——純文字販售頁、訂單查詢頁沒有商品圖，YOLO 給 0 分是對的，
但加權後就被拉到門檻以下放行了。
"""
import pytest
from helpers import find_result, wait_for

pytestmark = pytest.mark.integration

# (nlp, yolo, 預期等級, 說明)
CASES = [
    (100, 100, "極高風險",             "兩個引擎都確定"),
    (95,   40, "極高風險",             "文字確定，影像也附和"),
    (90,   30, "極高風險",             "剛好踩到兩個門檻"),
    (100,   0, "高風險 (優先人工覆核)", "文字確定但沒有商品圖——舊規則會漏掉這種"),
    (90,   29, "高風險 (優先人工覆核)", "影像差一分沒附和"),
    (70,   10, "中風險 (建議人工覆核)", "文字中等"),
    # YOLO 單獨高分不該把東西推進覆核清單。實測（217 筆人工標註）符合
    # 「yolo>=90 且 nlp<60」的有 12 筆，真陽性 0 個；線上資料裡這條觸發了
    # 中風險的 78%（121/155），那批 nlp 平均 0 分。乾淨的商品照上 YOLO 很容易
    # 把保健食品、化妝品看成毒品，文字完全沒訊號時那幾乎一定是誤判。
    (10,   95, "低風險",               "只有影像高分——不該進覆核清單"),
    (59,  100, "低風險",               "文字差一分沒到中風險，影像再高也不算"),
    (30,   20, "低風險",               "兩邊都低"),
    (0,     0, "低風險",               "都沒有訊號"),
]


@pytest.mark.parametrize("nlp,yolo,level,why", CASES,
                         ids=[c[3] for c in CASES])
def test_risk_level(internal, admin, unique_url, nlp, yolo, level, why):
    internal.post("/api/nlp/report/",
              json={"url": unique_url, "risk_score": nlp, "nlp_keywords": ["t"]})
    internal.post("/api/ai_result/report/",
              json={"url": unique_url, "risk_score": yolo, "yolo_objects": ["t"]})

    row = wait_for(lambda: find_result(admin, unique_url), what="分析結果")
    assert row["risk_level"] == level, (
        f"{why}：nlp={nlp} yolo={yolo} 應為「{level}」，實際「{row['risk_level']}」")
    # 綜合分數仍以加權算出，供前端排序用
    assert row["risk_score"] == int(0.6 * nlp + 0.4 * yolo)


def test_no_high_confidence_site_is_released(internal, admin, unique_url):
    """
    NLP 完全確定卻沒有商品圖的網站，絕對不能被放行。
    實測 42 個被舊規則漏掉的毒品網站裡，39 個是這種。
    """
    internal.post("/api/nlp/report/",
              json={"url": unique_url, "risk_score": 100, "nlp_keywords": ["毒品"]})
    internal.post("/api/ai_result/report/",
              json={"url": unique_url, "risk_score": 0, "yolo_objects": []})

    row = wait_for(lambda: find_result(admin, unique_url), what="分析結果")
    assert row["risk_level"] != "低風險", "NLP 100 分的網站被判為低風險並放行"


def test_status_matches_risk_level(internal, admin, unique_url):
    """24 小時清單的 status 要跟 risk_level 講同一件事（BUG-01）。"""
    # 先讓爬蟲流程跑完。順序不能顛倒——crawler/report 會觸發背景派發，
    # stub 回報的分數會把我們設好的值蓋掉。
    internal.post("/api/crawler/report/", json={
        "task_type": "automated_24h", "url": unique_url,
        "text_content": "x", "keywords": [], "product_images_b64": []})
    wait_for(lambda: find_result(admin, unique_url), what="爬蟲建檔")

    # 再覆寫成要驗的分數：文字確定、影像沒東西
    internal.post("/api/nlp/report/",
              json={"url": unique_url, "risk_score": 100, "nlp_keywords": ["t"]})
    internal.post("/api/ai_result/report/",
              json={"url": unique_url, "risk_score": 0, "yolo_objects": []})
    wait_for(lambda: (find_result(admin, unique_url) or {}).get("nlp_score") == 100,
             what="分數覆寫完成")

    def in_list():
        r = admin.get("/api/crawler/automated_24h_list/", params={"limit": 100})
        for row in r.json().get("data", []):
            if row["domain_name"] == unique_url:
                return row
        return None

    row = wait_for(in_list, what="24h 清單裡的那筆")
    assert row["risk_level"] == "高風險 (優先人工覆核)"
    assert row["status"] != "Monitored", (
        f"risk_level 是「{row['risk_level']}」，status 卻是最低的 {row['status']}")

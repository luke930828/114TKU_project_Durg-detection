"""
最重要的一組：爬蟲 → 後端 → NLP/YOLO → 合併 → 報表。

這正是單機整合測試唯一真正該驗的東西：模組之間的介面契約。
取代並擴充原本的 scripts/smoke_test.py（它的第 4 步用舊的 X-Token=帳號名，永遠 401）。

stub 的分數是固定的：NLP 0.6 → 後端算成 60，YOLO 80。
綜合分數 = 0.6×60 + 0.4×80 = 68 → 「中風險 (建議人工覆核)」
"""
import pytest
import requests
from helpers import crawler_report, find_result, wait_both_engines, wait_for

pytestmark = pytest.mark.integration

EXPECTED_NLP = 60
EXPECTED_YOLO = 80
EXPECTED_TOTAL = 68


def test_crawler_report_accepted(internal, unique_url):
    r = crawler_report(internal, unique_url)
    assert r.status_code == 200, r.text[:300]
    assert r.json()["status"] == "success"


def test_raw_evidence_stored(internal, db, unique_url):
    """原始蒐證資料要真的落庫，不是只有 API 回 200。"""
    crawler_report(internal, unique_url)
    with db.cursor() as c:
        c.execute("SELECT * FROM suspect_websites WHERE url=%s", (unique_url,))
        row = c.fetchone()
    assert row, "suspect_websites 裡找不到這筆"
    assert row["html_content"], "html_content 沒有存進去"
    assert "測試關鍵字" in (row["keywords_found"] or "")


def test_full_pipeline_merges_both_engines(internal, admin, unique_url):
    """整條鏈路跑完，兩個引擎的分數要合併在同一筆。"""
    assert crawler_report(internal, unique_url).status_code == 200
    row = wait_both_engines(admin, unique_url)

    assert row["nlp_score"] == EXPECTED_NLP, f"NLP 分數不對：{row['nlp_score']}"
    assert row["yolo_score"] == EXPECTED_YOLO, f"YOLO 分數不對：{row['yolo_score']}"
    assert row["risk_score"] == EXPECTED_TOTAL, (
        f"綜合分數應為 0.6×{EXPECTED_NLP}+0.4×{EXPECTED_YOLO}={EXPECTED_TOTAL}，"
        f"實際 {row['risk_score']}"
    )
    assert row["risk_level"] == "中風險 (建議人工覆核)"


def test_backend_dispatched_to_both_engines(internal, unique_url):
    """後端有沒有真的把任務派出去（而不是自己算一算就好）。"""
    crawler_report(internal, unique_url)

    def got(port, path_key):
        calls = requests.get(f"http://127.0.0.1:{port}/__stub/calls", timeout=10).json()
        return [c for c in calls["calls"] if c["payload"].get("url") == unique_url]

    assert wait_for(lambda: got(18000, "/predict"), what="NLP 收到派發")
    assert wait_for(lambda: got(15000, "/api/v1/predict/trigger"), what="YOLO 收到派發")


def test_later_engine_does_not_overwrite_earlier(internal, admin, unique_url):
    """先到的引擎結果不能被後到的洗掉——這是原本 smoke test 唯一驗到的點。"""
    internal.post("/api/nlp/report/",
              json={"url": unique_url, "risk_score": 55, "nlp_keywords": ["先到"]})
    internal.post("/api/ai_result/report/",
              json={"url": unique_url, "risk_score": 90, "yolo_objects": ["後到"],
                    "class_metadata": {"後到": 1}})

    row = wait_for(lambda: find_result(admin, unique_url), what="合併結果")
    assert row["nlp_score"] == 55, "後到的 YOLO 把先到的 NLP 洗掉了"
    assert row["yolo_score"] == 90
    assert row["risk_score"] == int(0.6 * 55 + 0.4 * 90)


def test_reverse_order_also_merges(internal, admin, unique_url):
    """反過來 YOLO 先到、NLP 後到，也要正確合併。"""
    internal.post("/api/ai_result/report/",
              json={"url": unique_url, "risk_score": 70, "yolo_objects": ["先到"]})
    internal.post("/api/nlp/report/",
              json={"url": unique_url, "risk_score": 40, "nlp_keywords": ["後到"]})

    row = wait_for(lambda: find_result(admin, unique_url), what="合併結果")
    assert row["yolo_score"] == 70
    assert row["nlp_score"] == 40
    assert row["risk_score"] == int(0.6 * 40 + 0.4 * 70)


def test_nlp_result_written_exactly_once(internal, admin, db, unique_url):
    """
    BUG-03：後端 utils.py 推一次、NLP 服務自己再推一次，同一筆結果寫兩遍。
    ai_analysis_results 以 url 做 upsert，所以資料上看不出來，
    這裡驗的是同一個網址不該產生重複的分析列。
    """
    crawler_report(internal, unique_url)
    wait_both_engines(admin, unique_url)      # 等整條鏈路真的跑完再查
    with db.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM ai_analysis_results WHERE url=%s", (unique_url,))
        n = c.fetchone()["n"]
    assert n == 1, f"同一個網址產生了 {n} 筆 AI 分析結果"


def test_repeated_crawler_report_does_not_fail(internal, db, unique_url):
    """
    同一個網址重複回報不能 500。

    suspect_websites.url 上有 UNIQUE index，而 24 小時自動爬蟲本來就會
    一再遇到同一個網站。原本這裡是無條件 db.add，第二次回報就撞 1062
    Duplicate entry，被 except 接住 → 回 500、整筆回報丟掉。

    症狀難查：爬蟲只看到一次 500，看不出是「這個網址已經有了」，
    畫面上顯示的還是舊快照，看起來像爬蟲沒在跑。
    """
    for i in range(1, 4):
        r = crawler_report(internal, unique_url,
                           text_content=f"第 {i} 次抓到的內容",
                           keywords=[f"第{i}次"])
        assert r.status_code == 200, f"第 {i} 次回報失敗：{r.status_code} {r.text[:200]}"

    with db.cursor() as c:
        c.execute("SELECT COUNT(*) n FROM suspect_websites WHERE url=%s", (unique_url,))
        assert c.fetchone()["n"] == 1, "同一個網址產生了多筆蒐證資料"


def test_repeated_report_refreshes_snapshot(internal, db, unique_url):
    """重複回報要用新的快照覆蓋舊的——最新那份才是有意義的證據。

    created_at 則刻意不動，那是「第一次發現的時間」。
    """
    crawler_report(internal, unique_url, text_content="第一次的內容")
    with db.cursor() as c:
        c.execute("SELECT created_at FROM suspect_websites WHERE url=%s", (unique_url,))
        first_seen = c.fetchone()["created_at"]

    crawler_report(internal, unique_url, text_content="後來重抓的內容")
    with db.cursor() as c:
        c.execute("SELECT html_content, created_at FROM suspect_websites WHERE url=%s",
                  (unique_url,))
        row = c.fetchone()

    assert row["html_content"] == "後來重抓的內容", "快照沒有更新"
    assert row["created_at"] == first_seen, "created_at 應該保持第一次發現的時間"

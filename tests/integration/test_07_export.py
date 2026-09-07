"""Excel 匯出。"""
import io

import pytest
from helpers import crawler_report, wait_both_engines

pytestmark = pytest.mark.integration


@pytest.fixture
def one_result(internal, admin, unique_url):
    crawler_report(internal, unique_url)
    wait_both_engines(admin, unique_url)
    return unique_url


def test_export_returns_xlsx(admin, one_result):
    r = admin.get("/api/export/ai_results_excel/")
    assert r.status_code == 200
    assert "spreadsheetml" in r.headers.get("content-type", "")
    assert r.content[:2] == b"PK", "回傳的不是 xlsx（zip）格式"


def test_export_content_readable(admin, one_result):
    openpyxl = pytest.importorskip("openpyxl")
    r = admin.get("/api/export/ai_results_excel/")
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    ws = wb.active
    header = [c.value for c in next(ws.iter_rows(max_row=1))]
    assert header == ["id", "url", "risk_score", "risk_level"]

    urls = {row[1].value for row in ws.iter_rows(min_row=2)}
    assert one_result in urls, "剛產生的資料沒有出現在匯出檔裡"


def test_export_respects_date_range(admin, one_result):
    """
    BUG-02：前端 Report.tsx 會送 start_date / end_date，
    但 export.py 是 .all()，日期區間被完全忽略，每次都匯出全表。

    這裡用一個「絕對不可能有資料」的過去區間，正確行為應該是回 404 或空表。
    """
    openpyxl = pytest.importorskip("openpyxl")
    r = admin.get("/api/export/ai_results_excel/",
                  params={"start_date": "2000-01-01", "end_date": "2000-01-02"})
    if r.status_code == 404:
        return                                    # 沒資料 → 正確
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    rows = list(wb.active.iter_rows(min_row=2))
    assert len(rows) == 0, (
        f"指定 2000 年的區間卻匯出了 {len(rows)} 筆——日期參數被忽略了（BUG-02）"
    )

from dependencies import get_db, get_current_user, verify_admin, log_audit_action
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session
from typing import Optional
import pandas as pd
import io
import database
from datetime import datetime

router = APIRouter(tags=["報表匯出模組"])

@router.get("/api/export/ai_results_excel/", summary="匯出 AI 分析結果資料表")
def export_raw_results_to_excel(
    start_date: Optional[str] = Query(None, max_length=32, description="開始日期 (YYYY-MM-DD)"),
    end_date: Optional[str] = Query(None, max_length=32, description="結束日期 (YYYY-MM-DD)"),
    db: Session = Depends(get_db),
    # 匯出是把「全部蒐證資料」一次帶走，不是一般查詢——限管理員（SEC-12）。
    # 原本是 get_current_user，任何登入者都能整包下載。
    current_user = Depends(verify_admin),
): 
    query = db.query(
        database.AIAnalysisResult.id,
        database.AIAnalysisResult.url,
        database.AIAnalysisResult.risk_score,
        database.AIAnalysisResult.risk_level,
        database.AIAnalysisResult.created_at 
    )
    
    # 日期一定要先驗格式再丟進查詢。
    # 沒驗的話 start_date="' OR 1=1--" 會讓 MySQL 在比較時丟例外，
    # 整個請求 500——雖然 SQLAlchemy 有參數化、注入不會成立，
    # 但把使用者輸入直接當日期比較本來就會炸，而且 500 對使用者毫無資訊。
    def _as_date(value: str, field: str) -> str:
        try:
            return datetime.strptime(value.strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"{field} 的格式不正確，要 YYYY-MM-DD（收到：{value[:30]}）",
            )

    if start_date:
        query = query.filter(
            database.AIAnalysisResult.created_at >= _as_date(start_date, "start_date"))
    if end_date:
        query = query.filter(
            database.AIAnalysisResult.created_at
            <= f"{_as_date(end_date, 'end_date')} 23:59:59")
        
    results = query.all()
    
    if not results:
        raise HTTPException(status_code=404, detail="目前沒有符合該時間區間的分析資料可以匯出")

    data_list = [
        {
            "id": row.id,
            "url": row.url,
            "risk_score": row.risk_score,
            "risk_level": row.risk_level
        }
        for row in results
    ]

    df = pd.DataFrame(data_list)
    
    stream = io.BytesIO()
    
    with pd.ExcelWriter(stream, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='AI分析總表')
    
    stream.seek(0)

    # 「誰在什麼時候把整批蒐證資料帶走了」——這是稽核軌跡裡最該留的一筆
    log_audit_action(
        db, current_user.user_id, "匯出報表",
        f"匯出 {len(data_list)} 筆 AI 分析結果"
        + (f"（{start_date} ~ {end_date}）" if start_date or end_date else ""),
    )

    headers = {
        'Content-Disposition': 'attachment; filename="ai_analysis_database_export.xlsx"'
    }
    
    return StreamingResponse(
        stream, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
        headers=headers
    )
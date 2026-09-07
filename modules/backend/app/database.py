from sqlalchemy import create_engine, Column, Integer, String, DateTime, ForeignKey, Text, JSON, Boolean, inspect, text
from sqlalchemy.sql import func
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from datetime import datetime
from sqlalchemy.dialects.mysql import LONGTEXT
import json
import os

# 1. 設定資料庫連線網址（一律從環境變數讀，不要寫死密碼——這行已經被改回寫死兩次了，
#    再改回去之前請先確認你本機有設好 DB_PASSWORD / DB_HOST，見 .env.local）
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.environ["DB_PASSWORD"]  # 必填，沒設就直接爆掉，不要靜靜用預設值跑錯
DB_HOST = os.getenv("DB_HOST", "mysql")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_NAME = os.getenv("DB_NAME", "drug_prevention_db")
SQLALCHEMY_DATABASE_URL = (
    f"mysql+pymysql://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}"
    "?charset=utf8mb4"
)
engine = create_engine(SQLALCHEMY_DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

# 2. 定義「使用者與權限」資料表
class User(Base):
    __tablename__ = "users"

    user_id = Column(String(50), primary_key=True, index=True) 
    account = Column(String(50), unique=True, index=True, nullable=False)
    password_hash = Column(String(255), nullable=False) 
    role = Column(String(20), nullable=False) 
    department = Column(String(50))
    # 舊寫法（勿用）：default=datetime.utcnow
    # 其他四張表用的是 func.now()（MySQL NOW()），只有這兩處用 Python 的 utcnow。
    # 容器時區改成 Asia/Taipei 之後 func.now() 會跟著變、utcnow 不會，
    # 兩者會差 8 小時——同一個系統裡有兩套時間比沒有時區設定更難查。
    created_at = Column(DateTime, default=func.now())
    is_active = Column(Boolean, default=True)
    is_deleted = Column(Boolean, default=False)
    audit_logs = relationship("AuditLog", back_populates="user")



# 3. 定義「數位證據稽核日誌」資料表
class AuditLog(Base):
    __tablename__ = "audit_logs"
    
    log_id = Column(Integer, primary_key=True, index=True) 
    
    user_id = Column(String(50), ForeignKey("users.user_id"), nullable=False)
    
    action_type = Column(String(100), nullable=False)
    
    # 舊寫法（勿用）：default=datetime.utcnow
    action_timestamp = Column(DateTime, default=func.now())
    
    details = Column(String(500), nullable=True)
    
    user = relationship("User", back_populates="audit_logs")
# 4. 定義：「可疑網站黑名單」資料表
# 4. 定義：「可疑網站黑名單」資料表
class SuspectWebsite(Base):
    __tablename__ = "suspect_websites"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(768), unique=True, index=True, nullable=False)
    title = Column(String(100))
    keywords_found = Column(String(500))
    reported_by = Column(String(50))
    created_at = Column(DateTime, default=func.now())

    html_content = Column(LONGTEXT, nullable=True)  
    images_data = Column(LONGTEXT, nullable=True)
# 5. 定義：「白名單」資料表
class WhitelistWebsite(Base):
    __tablename__ = "whitelist_websites"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(768), unique=True, index=True, nullable=False)
    title = Column(String(100))
    reason = Column(String(255))
    added_by = Column(String(50)) 
    created_at = Column(DateTime, default=func.now())
    # 怎麼進白名單的：一般新增 vs 誤判回報。
    # 兩者的意義不同——「誤判回報」代表 AI 判錯過，那是模型改善的線索，
    # 混在一起看就分不出「我們主動排除的正常網站」和「模型抓錯的網站」。
    source = Column(String(20), default="一般新增")

class BlacklistWebsite(Base):
    """
    人工加入的黑名單。跟白名單對稱。

    在這之前系統的「黑名單」是從 ai_analysis_results.risk_level == 極高風險
    推導出來的，沒有辦法人工把一個網址直接標為毒品網站——承辦人員手上有
    情資但系統裡沒地方放。
    """
    __tablename__ = "blacklist_websites"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(768), unique=True, index=True, nullable=False)
    title = Column(String(100))
    reason = Column(String(255))
    added_by = Column(String(50))
    created_at = Column(DateTime, default=func.now())


# 6. 定義：專門展示給前端看的 AI 分析結果表
class AIAnalysisResult(Base):
    __tablename__ = "ai_analysis_results"

    id = Column(Integer, primary_key=True, index=True)
    url = Column(String(768), index=True, nullable=False)
    
    yolo_details = Column(String(500))  
    yolo_score = Column(Integer, default=0)
    
    nlp_details = Column(String(500))  
    nlp_score = Column(Integer, default=0)
    
    risk_score = Column(Integer)
    risk_level = Column(String(50))
    
    class_metadata = Column(JSON, nullable=True) 
    representative_image_base64 = Column(LONGTEXT, nullable=True)
    representative_image_detections = Column(JSON, nullable=True)
    # OCR 是由影像分析引擎回傳的結構化結果；保留 JSON，避免把每個辨識框拆成
    # 多張資料表後破壞既有 API 的回傳格式。
    ocr_results = Column(JSON, nullable=True)
    task_source = Column(String(100), default="未知來源")
    created_at = Column(DateTime, default=func.now())


# 既有資料庫要補的欄位。create_all 只會「建不存在的表」，絕不會 ALTER
# 已存在的表——所以每次替既有的表加欄位，都得在這裡登記一筆，
# 否則組員 pull 之後程式讀得到欄位、資料庫沒有，一查就 1054 Unknown column。
#
# (表名, 欄位名, 完整的 DDL 片段)
_PENDING_COLUMNS = [
    ("ai_analysis_results", "ocr_results", "JSON NULL"),
    # 白名單的來源分類（一般新增 / 誤判回報）。原本是請組員自己下 SQL，
    # 但那種「請大家記得手動跑」的步驟一定會有人漏掉，放進這裡自動補。
    ("whitelist_websites", "source", "VARCHAR(20) DEFAULT '一般新增'"),
]


def initialize_database():
    """建立新表並以非破壞方式補齊既有資料庫缺少的欄位。

    可以安全地重複執行：每一欄都先檢查存不存在，不會覆寫任何既有資料。
    """
    Base.metadata.create_all(bind=engine)

    inspector = inspect(engine)
    for table, column, ddl in _PENDING_COLUMNS:
        if table not in inspector.get_table_names():
            continue
        existing = {c["name"] for c in inspector.get_columns(table)}
        if column in existing:
            continue
        with engine.begin() as connection:
            connection.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
        print(f"已新增 {table}.{column} 欄位")


#  7. 執行建立資料表的指令 
if __name__ == "__main__":
    print("正在連線資料庫並建立資料表...")
    initialize_database()
    print(" 資料表建立完成！")

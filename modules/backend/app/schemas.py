from pydantic import BaseModel, Field, StringConstraints, field_validator
from typing import Annotated
from typing import List, Dict, Any, Optional
import ipaddress
import socket
from urllib.parse import urlparse


def reject_if_internal(url: str) -> str:
    """
    擋掉指向內網的網址（SSRF）。

    /api/scan_target/ 會把使用者給的網址直接交給爬蟲去抓，原本完全不檢查。
    可以拿來探測內網、打雲端 metadata（169.254.169.254）、或是叫爬蟲去打
    後端自己那三個沒有驗證的端點。前端雖然有網址格式檢查，
    但直接打 API 就繞過了——把關要在後端。

    設計上刻意「只在解析成功且指向內網時才拒絕」：
    解析不出來的網域（例如測試用的 .invalid）本來就連不到任何東西，
    沒有 SSRF 風險，讓爬蟲自己去失敗即可。太嚴會擋掉正常的新網站
    （DNS 短暫查不到）以及整合測試。
    """
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise ValueError("網址必須以 http:// 或 https:// 開頭")

    host = parsed.hostname
    if not host:
        raise ValueError("網址格式不正確，找不到主機名稱")

    if host.lower() in ("localhost", "localhost.localdomain"):
        raise ValueError("不接受指向本機的網址")

    try:
        # getaddrinfo 會一併處理 IPv6、IPv4-mapped、以及 http://2130706433/
        # 這種十進位寫法，不用自己拆解各種變形
        infos = socket.getaddrinfo(host, parsed.port or 80, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        # 查不到就查不到——爬蟲照樣抓不到，構不成 SSRF
        return url

    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
            raise ValueError(f"不接受指向內部網路的網址（{host} → {ip}）")

    return url

# 為什麼這裡沒有 XSS 黑名單了
# ─────────────────────────────
# 原本擋 <script>、javascript:、onload=、onerror= 四個字串。問題有兩個：
#
# 1. 黑名單永遠列不完。<img src=x onmouseover=1>、<script >（多一個空格）、
#    <svg onfocus=>、<details ontoggle=> 全部繞得過。擋掉四個字串只是讓人
#    以為擋住了，實際的攻擊面一點都沒縮小。
#
# 2. 它還在存進資料庫前做 html.escape()，但只做在「讀」的那一側。
#    UserLogin 會把帳號跳脫後才去查資料庫，UserCreate 卻是原文存進去，
#    結果帳號含 & < > " ' 的人永遠登不進去——防護沒做到，資料先壞了。
#
# XSS 要在「輸出」的地方擋，不是在輸入的地方：
#   前端是 React，插值預設就會跳脫（要小心的是 dangerouslySetInnerHTML）；
#   後端回應是 application/json，瀏覽器不會拿去當 HTML 解析。
#
# 所以輸入端只負責它真正該負責的事：型別與長度。
# 長度上限對齊 database.py 的欄位定義，避免寫入時才炸出 MySQL DataError。


# 前端輸入區
class UserLogin(BaseModel):
    account: str = Field(min_length=1, max_length=50)      # users.account String(50)
    password: str = Field(min_length=1, max_length=200)


class FrontendScanRequest(BaseModel):
    url: str = Field(min_length=1, max_length=768)         # ai_analysis_results.url String(768)

    @field_validator("url")
    @classmethod
    def block_ssrf(cls, v: str) -> str:
        return reject_if_internal(v)


class WhitelistCreate(BaseModel):
    url: str = Field(min_length=1, max_length=768)         # whitelist_websites 各欄位
    title: str = Field(max_length=100)
    reason: str = Field(max_length=255)
    source: str = Field(default="一般新增", max_length=20)


class BlacklistCreate(BaseModel):
    url: str = Field(min_length=1, max_length=768)         # blacklist_websites 各欄位
    title: str = Field(default="", max_length=100)
    reason: str = Field(default="", max_length=255)


#  內部通訊區 
class WebsiteReport(BaseModel):
    """
    爬蟲回報的封包。每個欄位都要有長度上限（SEC-16）。

    沒有上限的後果不是理論問題：
      * task_type 會被組成 suspect_websites.title（String(100)），
        過長時 MySQL 丟 DataError，整個請求 500，AI 派發也不會發生。
      * text_content 進 LONGTEXT，nginx 放行 50 MB，等於一個請求就能
        往資料庫塞 50 MB，連按幾次就把磁碟吃光。
    上限訂在「資料庫欄位裝得下」而不是「越大越好」。
    """
    # title 是 String(100)，格式為 "[{task_type}] 爬蟲自動通報"（多 9 個字），
    # 留 50 給 task_type 綽綽有餘。
    task_type: Optional[str] = Field("unknown", max_length=50)
    timestamp: Optional[str] = Field("unknown", max_length=64)
    # keywords_found 是 String(500)，逗號串接後會被截斷，這裡先擋住離譜的量。
    keywords: Optional[List[str]] = Field(default=[], max_length=100)
    url: str = Field(..., max_length=768)          # suspect_websites.url 是 varchar(768)
    screenshot_b64: Optional[str] = None
    full_screenshot_base64: Optional[str] = None
    product_images_b64: Optional[List[Any]] = None   
    product_images_base64: Optional[List[Any]] = None
    # 模型只讀前 256 個 token，1 MB 已經遠超過需要；再多只是佔資料庫。
    text_content: Optional[str] = Field(None, max_length=1_000_000)

class ConfirmBatch(BaseModel):
    """批次覆核要確認的分析結果 id。

    上限 200 是配合待確認清單一頁最多 200 筆——使用者不可能選到比畫面上
    更多的東西。不設上限的話，一個請求就能要求後端更新整張表（SEC-16）。
    """
    ids: List[int] = Field(..., min_length=1, max_length=200)


class OCRDetectedText(BaseModel):
    """OCR 在一張圖上找到的一段文字。欄位名對齊 modules/yolo/app/ai_model/ocr.py。"""
    text: str = Field(..., max_length=500)
    confidence: float = Field(0.0, ge=0.0, le=1.0)
    # 整批圖的文字是混在一起回報的，靠這個才知道是從第幾張圖抓到的。
    image_index: Optional[int] = Field(None, ge=0, le=10_000)
    # 座標格式跟 YOLO 的偵測框一致（xyxyn，0~1 正規化），前端才能共用畫框邏輯。
    box_format: Optional[str] = Field(None, max_length=20)
    box: Optional[List[float]] = Field(None, max_length=4)


class OCRResults(BaseModel):
    """整批圖片的 OCR 彙整。engine 留著，之後換引擎時舊資料還看得出來源。"""
    engine: str = Field("easyocr", max_length=50)
    # 一批可能有 20 張圖，每張十幾段文字；2000 是很寬鬆的上限，
    # 但擋得住「一段 1 MB 的文字」那種病態輸入。
    detected_texts: List[OCRDetectedText] = Field(default=[], max_length=2000)


class YOLOAnalysisReport(BaseModel):
    """
    YOLO 的回報。每個欄位都要有上限（SEC-16）。

    這兩個 AI 回報模型原本一個上限都沒有：1 MB 的 url 讓請求 500
    （ai_analysis_results.url 是 varchar(768)，寫入時 MySQL 丟 DataError），
    1 MB 的 representative_image_base64 則被照單全收寫進 LONGTEXT。

    ⚠️ List 的 max_length 限的是「項目數量」，不是每個字串的長度。
    兩者都要擋——只加前者的話，一個 1 MB 的物件名稱照樣穿得過去。

    端點雖然要 internal token，但那個 token 存在五個容器裡，
    任何一個被打下來就等於可以往資料庫塞任意大的資料。
    """
    url: str = Field(..., max_length=768)          # ai_analysis_results.url
    risk_score: int = Field(..., ge=0, le=100)
    yolo_objects: List[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default=[], max_length=100)
    processed_images: Optional[List[Annotated[str, StringConstraints(max_length=2000)]]] = Field(
        default=[], max_length=100)
    class_metadata: Optional[Dict[str, Any]] = None
    # 實際的代表圖約 30 KB ~ 1 MB，10 MB 已經是很寬鬆的上限。
    representative_image_base64: Optional[str] = Field(None, max_length=10_000_000)
    representative_image_detections: Optional[List[Dict[str, Any]]] = Field(
        default=None, max_length=500)
    # OCR 結果寫成明確的結構，不用 Optional[Any]。
    #
    # 原本的版本是 `ocr_results: Optional[Any] = None`，註解寫「格式可能隨模型
    # 版本改變，以 JSON 原樣保存」。但這個欄位正是 YOLO 與前端對不起來的地方：
    # YOLO 送物件、前端讀陣列，兩邊都「照自己的格式寫」，結果 OCR 永遠不顯示。
    # 把契約寫進 schema，格式一變就 422，不會再靜靜地不顯示。
    #
    # 另外 Any 沒有任何長度上限，等於在 SEC-16 修好的地方開一個新洞。
    ocr_results: Optional[OCRResults] = None
    # 「這一頁根本沒有圖」跟「有圖但沒看到東西」是兩件事，畫面上要分得出來。
    # 前者是爬蟲沒抓到任何商品圖，後者是 YOLO 跑完但沒有檢出。
    no_images: bool = False

class NLPAnalysisReport(BaseModel):
    """NLP 的回報。上限的理由同 YOLOAnalysisReport。"""
    url: str = Field(..., max_length=768)          # ai_analysis_results.url
    risk_score: int = Field(..., ge=0, le=100)
    # 清單長度與「每個項目的長度」是兩回事。max_length 用在 List 上限的是
    # 項目數量，一個 1 MB 的關鍵字照樣進得來——實測 50 個 1 MB 的關鍵字
    # 串接後寫進 varchar(500) 的 nlp_details，MySQL 丟 DataError、請求 500。
    nlp_keywords: List[Annotated[str, StringConstraints(max_length=200)]] = Field(
        default=[], max_length=100)

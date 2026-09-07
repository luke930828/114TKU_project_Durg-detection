"""爬蟲抓到的商品圖：存成檔案，資料庫只留路徑。

為什麼不放在資料庫
──────────────────
suspect_websites.images_data 原本存 base64 文字，那個欄位一路長到 10.4 GB，
佔了整個資料庫的八成。MySQL 的 buffer pool 放不下這種量，爬蟲每寫一筆就把
快取裡有用的東西擠掉，命中率長期只有 38%，所有查詢都在打磁碟。

圖片本身是蒐證資料，不能不存——「AI 判這頁 100 分」的依據就是那些圖，
而且 YOLO 掛掉時要靠它們補跑分析。所以是換地方存，不是不存。

檔名用內容的 SHA-256
────────────────────
同一個站的每一頁通常都掛著同一組 logo、橫幅、付款方式圖示，一頁一份的話
重複的部分會佔掉大半空間。用內容雜湊當檔名，一模一樣的圖天然只存一份，
而且重跑遷移不會產生重複檔案（同樣的內容算出同樣的路徑，覆寫即可）。

原樣存放，不重新編碼
────────────────────
PNG 佔了 69% 的空間卻只有 28% 的張數，轉成 JPEG 可以省很多。但這是證據，
改動位元組就失去了「這就是當時抓到的那張圖」的意義，所以一個 byte 都不動。
單是拿掉 base64 的 33% 膨脹，就已經省下約四分之一。
"""
import base64
import binascii
import hashlib
import json
import os
import re
from pathlib import Path

# 預設路徑對應 compose 掛進 backend 的 volume。
IMAGE_ROOT = Path(os.getenv("SUSPECT_IMAGE_DIR", "/data/suspect_images"))

# 副檔名只用來讓人直接開檔時方便，判斷型別一律看內容。
_MAGIC = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"GIF87a", "gif"),
    (b"GIF89a", "gif"),
)

# 遷移後存的樣子：<雜湊前兩碼>/<完整雜湊>.<副檔名>
_PATH_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{64}\.[a-z0-9]{2,4}$")


def _extension(raw: bytes) -> str:
    for magic, ext in _MAGIC:
        if raw.startswith(magic):
            return ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "webp"
    head = raw[:256].lstrip().lower()
    if head.startswith(b"<svg") or head.startswith(b"<?xml"):
        return "svg"
    # 認不出來的照樣存。爬蟲抓到什麼就是什麼，不要在這裡丟掉證據。
    return "bin"


def is_stored_path(value: str) -> bool:
    """這個字串是遷移後的路徑，還是舊的 base64 內容？

    base64 的字母表含 '/'，所以不能只看有沒有斜線。改成比對完整格式：
    路徑固定 67 個字元出頭，base64 圖片動輒好幾萬個，兩者不會混淆。
    """
    return bool(value) and len(value) < 128 and bool(_PATH_RE.match(value))


def save_base64(b64: str) -> str:
    """把一張 base64 圖片寫成檔案，回傳相對路徑。內容相同就不會重複寫。"""
    raw = base64.b64decode(b64, validate=False)
    digest = hashlib.sha256(raw).hexdigest()
    rel = f"{digest[:2]}/{digest}.{_extension(raw)}"
    target = IMAGE_ROOT / rel
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        # 先寫暫存檔再改名。中途被中斷時不會留下半個檔案，
        # 而遷移腳本是「檔案在就當作已完成」，半個檔案會被誤判成完整的。
        tmp = target.with_suffix(target.suffix + ".part")
        tmp.write_bytes(raw)
        tmp.replace(target)
    return rel


def save_many(items) -> list:
    """一整頁的圖。壞掉的項目跳過，不要讓一張圖毀掉整筆紀錄。"""
    paths = []
    for item in items or []:
        if not isinstance(item, str) or not item:
            continue
        try:
            paths.append(save_base64(item))
        except (binascii.Error, ValueError, OSError) as err:
            print(f"[image_store] 這張圖存檔失敗，跳過：{err}")
    return paths


def load_base64(ref: str):
    """讀回 base64。傳路徑就讀檔，傳舊的 base64 就原樣回傳。

    兩種格式都要吃，因為遷移期間同一張表裡兩種會並存，
    而且補跑腳本在遷移中途也可能被執行。
    """
    if not isinstance(ref, str) or not ref:
        return None
    if not is_stored_path(ref):
        return ref
    target = IMAGE_ROOT / ref
    try:
        return base64.b64encode(target.read_bytes()).decode("ascii")
    except OSError as err:
        print(f"[image_store] 讀不到 {ref}：{err}")
        return None


def load_field(images_data: str) -> list:
    """把 suspect_websites.images_data 讀成一串 base64，新舊格式都吃。"""
    try:
        items = json.loads(images_data or "[]")
    except (TypeError, ValueError):
        return []
    out = []
    for item in items:
        b64 = load_base64(item)
        if b64:
            out.append(b64)
    return out

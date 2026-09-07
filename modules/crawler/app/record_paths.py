"""
Record 目錄約定（精簡版）：

  log_24h.txt      24H 雙軌運行紀錄
  log_manual.txt   手動爬紀錄
  visited.txt      所有探訪過的網址 + 時間
  images.jsonl     圖片紀錄摘要（不含 base64；JSONL 追加）
  nlp_text.jsonl   NLP／評分摘要（不含全文；JSONL 追加）
  queue.db / monitor_state.db  佇列（SQLite，非 JSON）
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime
from typing import Any, Dict, List, Optional

# 單一 JSONL 超過此大小則輪替（預設 32 MB）
DEFAULT_JSONL_MAX_BYTES = 32 * 1024 * 1024
# 輪替後最多保留幾個 .1 .2 ... 舊檔
DEFAULT_JSONL_BACKUP_COUNT = 3

DEFAULT_RECORD_PATHS: Dict[str, Any] = {
    "dir": "Record",
    "db_queue": "Record/queue.db",
    "db_monitor": "Record/monitor_state.db",
    "log_24h": "Record/log_24h.txt",
    "log_manual": "Record/log_manual.txt",
    "log_visited": "Record/visited.txt",
    "json_images": "Record/images.jsonl",
    "json_nlp_text": "Record/nlp_text.jsonl",
    "jsonl_max_bytes": DEFAULT_JSONL_MAX_BYTES,
    "jsonl_backup_count": DEFAULT_JSONL_BACKUP_COUNT,
    # 相容舊鍵（導向新檔，避免舊程式炸）
    "json_reports": "Record/nlp_text.jsonl",
    "json_reports_jsonl": "Record/nlp_text.jsonl",
    "json_dedup_urls": "Record/dedup_urls.json",
    "json_dedup_images": "Record/dedup_images.json",
    "json_webhook_failed": "Record/log_24h.txt",
    "log_engine": "Record/log_24h.txt",
    "log_unified_text": "Record/log_24h.txt",
    "log_unified_jsonl": "Record/log_24h.txt",
    "log_archive_dir": "Record/_archive",
    "log_dirs": {},
}

# 啟動時可刪／歸檔的雜訊檔（含舊版整包 JSON，避免再被讀爆記憶體）
OBSOLETE_NAMES = (
    "operation_v2.log",
    "operation_v2.log.1",
    "log_engine.log",
    "log_engine.log.1",
    "log_all.log",
    "log_all.log.1",
    "log_all.jsonl",
    "log_all.jsonl.1",
    "Potential_Shops.txt",
    "intel_report.json",
    "reports.json",
    "reports.jsonl",
    "search_health.json",
    "webhook_failed.jsonl",
    "dedup_urls.json",
    "dedup_images.json",
    "seen_urls.json",
    "seen_images.json",
    "visited_all.txt",
    "README.txt",
    # INFRA-02：舊的整檔 JSON（可能數百 MB～GB）
    "images.json",
    "nlp_text.json",
)

OBSOLETE_DIRS = (
    "log_manual",
    "log_visit",
    "log_24h_page",
    "log_24h_engine",
    "_logs_old",
    "_archive",
)


def get_record_paths(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    cfg = config or {}
    merged = {**DEFAULT_RECORD_PATHS, **(cfg.get("record_paths") or {})}
    return merged


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def append_text_line(path: str, line: str) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line if line.endswith("\n") else line + "\n")


def append_visited(
    paths: Dict[str, Any],
    url: str,
    *,
    source: str = "24H",
    tier: str = "",
    score: Any = "",
    status: str = "",
) -> None:
    path = paths.get("log_visited", "Record/visited.txt")
    parts = [f"[{_now()}]", f"source={source}"]
    if tier:
        parts.append(f"tier={tier}")
    if score != "" and score is not None:
        parts.append(f"score={score}")
    if status:
        parts.append(f"status={status}")
    parts.append(f"| {url}")
    append_text_line(path, " ".join(parts))


def _rotate_file_if_needed(
    path: str,
    *,
    max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
    backup_count: int = DEFAULT_JSONL_BACKUP_COUNT,
) -> None:
    """超過上限則 images.jsonl -> images.jsonl.1 -> ...，刪最舊。"""
    try:
        if max_bytes <= 0 or not os.path.isfile(path):
            return
        if os.path.getsize(path) < max_bytes:
            return
        # 刪最舊
        oldest = f"{path}.{backup_count}"
        if os.path.isfile(oldest):
            os.remove(oldest)
        for i in range(backup_count - 1, 0, -1):
            src = f"{path}.{i}"
            dst = f"{path}.{i + 1}"
            if os.path.isfile(src):
                os.replace(src, dst)
        os.replace(path, f"{path}.1")
        logging.info(f"[Record] 已輪替過大檔案: {path} (>{max_bytes} bytes)")
    except OSError as e:
        logging.warning(f"[Record] 輪替失敗 {path}: {e}")


def append_jsonl_record(
    path: str,
    record: Dict[str, Any],
    *,
    max_bytes: int = DEFAULT_JSONL_MAX_BYTES,
    backup_count: int = DEFAULT_JSONL_BACKUP_COUNT,
) -> None:
    """一行一筆 JSON，只追加、不整檔重寫。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    _rotate_file_if_needed(path, max_bytes=max_bytes, backup_count=backup_count)
    line = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _jsonl_limits(paths: Dict[str, Any]) -> tuple[int, int]:
    max_bytes = int(paths.get("jsonl_max_bytes") or DEFAULT_JSONL_MAX_BYTES)
    backup_count = int(paths.get("jsonl_backup_count") or DEFAULT_JSONL_BACKUP_COUNT)
    return max_bytes, max(1, backup_count)


def slim_images_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """去掉 base64，只留摘要（Webhook／MySQL 已有完整圖）。"""
    products = record.get("product_images") or record.get("product_images_b64") or []
    filenames: List[str] = []
    if isinstance(products, list):
        for i, item in enumerate(products, start=1):
            if isinstance(item, dict):
                name = str(item.get("filename") or "").strip()
                filenames.append(name or f"image_{i:02d}")
            elif isinstance(item, str) and item.strip():
                filenames.append(f"image_{i:02d}")
    has_shot = bool(
        (record.get("screenshot_b64") or "").strip()
        or (record.get("full_screenshot_base64") or "").strip()
        or record.get("has_screenshot")
    )
    return {
        "timestamp": record.get("timestamp") or _now(),
        "url": record.get("url") or "",
        "tier": record.get("tier", ""),
        "score": record.get("score", 0),
        "has_screenshot": has_shot,
        "product_image_count": len(filenames),
        "product_filenames": filenames[:30],
        "source": record.get("source") or "24H",
    }


def slim_nlp_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """去掉大段全文，只留評分／關鍵字摘要。"""
    text = record.get("text_content")
    text_len = len(text) if isinstance(text, str) else int(record.get("text_len") or 0)
    matched = record.get("matched") or record.get("keywords") or []
    if not isinstance(matched, list):
        matched = []
    fps = record.get("fingerprints") or []
    if not isinstance(fps, list):
        fps = []
    return {
        "timestamp": record.get("timestamp") or _now(),
        "url": record.get("url") or "",
        "tier": record.get("tier", ""),
        "score": record.get("score", 0),
        "matched": [str(k) for k in matched if str(k).strip()][:50],
        "fingerprints": [str(f) for f in fps if str(f).strip()][:20],
        "text_len": text_len,
        "source": record.get("source") or "24H",
    }


def append_images_record(paths: Dict[str, Any], record: Dict[str, Any]) -> None:
    path = paths.get("json_images", "Record/images.jsonl")
    max_bytes, backup_count = _jsonl_limits(paths)
    append_jsonl_record(
        path,
        slim_images_record(record),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def append_nlp_record(paths: Dict[str, Any], record: Dict[str, Any]) -> None:
    path = paths.get("json_nlp_text", "Record/nlp_text.jsonl")
    max_bytes, backup_count = _jsonl_limits(paths)
    append_jsonl_record(
        path,
        slim_nlp_record(record),
        max_bytes=max_bytes,
        backup_count=backup_count,
    )


def _looks_like_json_array(path: str) -> bool:
    """檔案是不是舊的「整個 JSON 陣列」格式。只讀開頭幾個位元組。"""
    try:
        with open(path, encoding="utf-8") as f:
            while True:
                ch = f.read(1)
                if ch == "":
                    return False
                if not ch.isspace():
                    return ch == "["
    except OSError:
        return False


def iter_json_array(path: str, chunk: int = 4 << 20):
    """
    串流讀出舊格式的陣列，一次只在記憶體裡放一筆。

    不用 json.load()：這個函式存在的理由就是要處理數百 MB 的舊檔，
    整份讀進來正是 INFRA-02 本身。
    """
    dec = json.JSONDecoder()
    with open(path, encoding="utf-8", errors="replace") as f:
        buf = f.read(chunk)
        i = buf.find("[")
        if i < 0:
            return
        buf = buf[i + 1:]
        while True:
            buf = buf.lstrip().lstrip(",").lstrip()
            if buf.startswith("]") or not buf:
                return
            while True:
                try:
                    obj, end = dec.raw_decode(buf)
                    break
                except ValueError:
                    more = f.read(chunk)
                    if not more:
                        return          # 檔案被截斷，能救多少算多少
                    buf += more
            yield obj
            buf = buf[end:]


def _load_json_list(path: str) -> List[Dict[str, Any]]:
    """
    讀出整份紀錄。舊的 JSON array 與新的 JSONL 都吃。

    只認 json.load 的話會出問題：json_images / json_nlp_text 現在指到
    .jsonl，migrate_record_legacy.py 有 6 處在讀它們，json.load 會拋例外、
    被 except 接住後回傳 []——不是報錯，是**靜靜地讀到空的**。

    ⚠️ 會把整個檔案讀進記憶體，只適合手動跑的遷移工具，不要放在爬頁的路徑上。
    """
    if not os.path.isfile(path):
        return []
    try:
        if _looks_like_json_array(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        items: List[Dict[str, Any]] = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue        # 壞掉的那一行跳過，不要讓整份讀不到
        return items
    except Exception:
        return []


def _save_json_list(path: str, items: List[Dict[str, Any]]) -> None:
    """依副檔名決定格式：.jsonl 寫逐行，其餘維持舊的 JSON array。"""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    if path.endswith(".jsonl"):
        with open(path, "w", encoding="utf-8") as f:
            for it in items:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
        return
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=2, ensure_ascii=False)


def migrate_legacy_json_to_jsonl(paths: Dict[str, Any]) -> Dict[str, int]:
    """
    把舊的 images.json / nlp_text.json 轉成新的 .jsonl 摘要。

    為什麼一定要有這一步：cleanup_obsolete_record_files 的 OBSOLETE_NAMES
    現在含 images.json 與 nlp_text.json，而 ensure_record_layout 每次啟動都會
    呼叫它——換句話說，換上新版之後爬蟲一啟動就會把舊檔刪掉。那兩個檔在
    2026-08-31 是 713 MB 與 88 MB，裡面的整頁截圖是唯一一份（後端只收
    product_images_b64，screenshot_b64 從來沒進過 MySQL）。

    這裡先把每筆記錄過一次 slim_*（去掉 base64）再追加到 .jsonl，
    讓紀錄的時間序不會斷在換格式那一天；原檔改名成 .migrated 保留，
    不直接刪——要刪請人工確認，而且 data/backups/ 應該先有一份完整副本。

    串流讀，不用 json.load：舊檔就是幾百 MB，整份讀進來正是 INFRA-02 本身。
    """
    moved = {"images": 0, "nlp_text": 0}
    jobs = (
        ("images", os.path.join(paths.get("dir", "Record"), "images.json"),
         paths.get("json_images", "Record/images.jsonl"), slim_images_record),
        ("nlp_text", os.path.join(paths.get("dir", "Record"), "nlp_text.json"),
         paths.get("json_nlp_text", "Record/nlp_text.jsonl"), slim_nlp_record),
    )
    for key, old_path, new_path, slim in jobs:
        if not os.path.isfile(old_path) or os.path.getsize(old_path) == 0:
            continue
        # 來源與目的地是同一個檔就什麼都別做。
        # config.json 的 record_paths 可以覆寫 json_images——如果那裡還指著
        # 舊的 Record/images.json，下面就會變成「一邊讀同一個檔、一邊往它
        # append」的無窮迴圈，檔案會一直長到磁碟滿。
        # 2026-08-31 實際踩過：713 MB 的檔在三分鐘內長到 10.8 GB。
        if os.path.abspath(old_path) == os.path.abspath(new_path):
            logging.warning(
                f"[RECORD] {old_path} 與目的地同一個檔，略過遷移。"
                f"請確認 config.json 的 record_paths.{('json_images' if key == 'images' else 'json_nlp_text')}"
                f" 已改成 .jsonl"
            )
            continue
        # 兩種格式都要搬。舊檔可能是原本的 JSON array，也可能已經被先前那版
        # INFRA-02 修正就地轉成 JSONL 了——後者副檔名還是 .json，一樣會被
        # cleanup 依檔名刪掉，所以不能只認 array。
        if _looks_like_json_array(old_path):
            source = iter_json_array(old_path)
        else:
            def source():                                        # noqa: E306
                with open(old_path, encoding="utf-8", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            yield json.loads(line)
                        except json.JSONDecodeError:
                            continue
            source = source()
        try:
            os.makedirs(os.path.dirname(new_path) or ".", exist_ok=True)
            with open(new_path, "a", encoding="utf-8") as out:
                for rec in source:
                    if isinstance(rec, dict):
                        out.write(json.dumps(slim(rec), ensure_ascii=False) + "\n")
                        moved[key] += 1
            os.replace(old_path, old_path + ".migrated")
            logging.info(f"[RECORD] {old_path} 已轉入 {new_path}（{moved[key]} 筆），"
                         f"原檔改名為 {old_path}.migrated")
        except Exception as e:                                   # noqa: BLE001
            logging.error(f"[RECORD] 轉檔失敗 {old_path}: {e!r}（原檔保留未動）")
    return moved


def cleanup_obsolete_record_files(paths: Dict[str, Any], *, force: bool = False) -> List[str]:
    """靜默清掉明顯多餘的舊檔；不做 migrate（遷移請手動跑 migrate_record_legacy.py）。"""
    record_dir = paths.get("dir", "Record")
    keep = {
        os.path.normpath(paths.get("log_24h", "")),
        os.path.normpath(paths.get("log_manual", "")),
        os.path.normpath(paths.get("log_visited", "")),
        os.path.normpath(paths.get("json_images", "")),
        os.path.normpath(paths.get("json_nlp_text", "")),
        os.path.normpath(paths.get("db_queue", "")),
        os.path.normpath(paths.get("db_monitor", "")),
        os.path.normpath(os.path.join(record_dir, "monitor_state.db")),
        os.path.normpath(os.path.join(record_dir, "queue.db")),
        os.path.normpath(os.path.join(record_dir, "README.txt")),
        os.path.normpath(os.path.join(record_dir, "operation_v2.log.bak")),
    }
    removed: List[str] = []

    for name in OBSOLETE_NAMES:
        if name == "operation_v2.log" and not force:
            continue
        p = os.path.join(record_dir, name)
        if os.path.normpath(p) in keep:
            continue
        if os.path.isfile(p):
            try:
                size_mb = os.path.getsize(p) / (1024 * 1024)
                os.remove(p)
                removed.append(name)
                if size_mb >= 10:
                    logging.warning(
                        f"[Record] 已刪除過大舊檔 {name} ({size_mb:.1f} MB)，"
                        f"改用 JSONL 追加寫入"
                    )
            except OSError:
                pass

    for name in OBSOLETE_DIRS:
        p = os.path.join(record_dir, name)
        if os.path.isdir(p):
            try:
                shutil.rmtree(p)
                removed.append(name + "/")
            except OSError:
                pass

    return removed


def _write_readme(paths: Dict[str, Any]) -> None:
    readme = os.path.join(paths["dir"], "README.txt")
    with open(readme, "w", encoding="utf-8") as f:
        f.write(
            "Record 資料夾（精簡）\n"
            "==================\n"
            "\n"
            "  log_24h.txt       24H 雙軌運行紀錄\n"
            "  log_manual.txt    手動爬紀錄\n"
            "  visited.txt       所有探訪網址 + 時間\n"
            "  images.jsonl      圖片摘要（無 base64，JSONL 追加）\n"
            "  nlp_text.jsonl    評分／關鍵字摘要（無全文，JSONL 追加）\n"
            "  monitor_state.db  24H 佇列（SQLite）\n"
            "  queue.db          手動／共用佇列（若有）\n"
            "\n"
            "完整截圖／商品圖請以後端 MySQL／Webhook 為準，勿再整包存本地 JSON。\n"
            "舊資料遷移（僅手動）：python migrate_record_legacy.py\n"
        )


def ensure_record_layout(config: Dict[str, Any] | None = None) -> Dict[str, Any]:
    """確保 Record 目錄與約定檔存在；啟動時安靜、不跑遷移、不刷 log。"""
    paths = get_record_paths(config)
    os.makedirs(paths["dir"], exist_ok=True)
    for key in ("log_24h", "log_manual", "log_visited"):
        p = paths[key]
        if not os.path.isfile(p):
            append_text_line(p, f"# {_now()} created\n")
    # JSONL：不預先寫空陣列；檔案不存在時第一次 append 會建立
    for key in ("json_images", "json_nlp_text"):
        p = paths[key]
        parent = os.path.dirname(p) or "."
        os.makedirs(parent, exist_ok=True)
    # 先把舊的整檔 JSON 轉成 .jsonl 摘要，再交給 cleanup。
    # 順序不能顛倒——cleanup 的 OBSOLETE_NAMES 含 images.json / nlp_text.json，
    # 先跑 cleanup 的話那些紀錄就直接沒了。
    migrate_legacy_json_to_jsonl(paths)
    cleanup_obsolete_record_files(paths)
    if not os.path.isfile(os.path.join(paths["dir"], "README.txt")):
        _write_readme(paths)
    return paths


def load_config_record_paths(config_path: str = "config.json") -> Dict[str, Any]:
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            return get_record_paths(json.load(f))
    except OSError:
        return get_record_paths()

"""
Webhook 工具模組

後端規格（手動 / 24H 共用）：
{
  "url": "...",
  "task_type": "automated_24h" | "manual",
  "timestamp": "2026-08-14T22:17:40Z",
  "keywords": ["..."],
  "text_content": "...",
  "screenshot_b64": "...",
  "full_screenshot_base64": "...",
  "product_images_b64": [
    "iVBORw0KGgo...",
    {"base64_data": "iVBORw0KGgo..."}
  ]
}
"""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urlparse

import httpx

from record_paths import get_record_paths

REQUIRED_WEBHOOK_FIELDS = (
    "url",
    "task_type",
    "timestamp",
    "keywords",
    "text_content",
    "screenshot_b64",
    "full_screenshot_base64",
    "product_images_b64",
)

NEGATIVE_ACCESS_TEXT = "非毒品網站或無法登入"

ProductImageItem = Union[str, Dict[str, str]]

_helper_instance: Optional["WebhookHelper"] = None


def utc_timestamp_iso() -> str:
    """後端規格：2026-08-14T22:17:40Z"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _is_valid_pure_base64(s: str) -> bool:
    if not isinstance(s, str):
        return False
    raw = s.strip()
    if not raw or raw.startswith("data:image"):
        return False
    try:
        base64.b64decode(raw, validate=True)
        return True
    except (binascii.Error, ValueError):
        return False


def _extract_b64(item: Any) -> str:
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        return str(
            item.get("base64_data")
            or item.get("base64")
            or item.get("b64")
            or item.get("data")
            or ""
        ).strip()
    return ""


def normalize_product_images(raw: Any) -> List[ProductImageItem]:
    """
    正規化為後端可接受格式：
    - 純 base64 字串
    - 或 {"base64_data": "..."}
    來源若是 {filename, base64_data}，輸出改為純字串（去掉 filename）。
    來源若已是僅含 base64_data 的物件，保留物件形態。
    """
    if not isinstance(raw, list):
        return []
    out: List[ProductImageItem] = []
    for item in raw:
        b64 = _extract_b64(item)
        if not b64:
            continue
        if isinstance(item, dict):
            keys = {k for k in item.keys() if str(item.get(k) or "").strip()}
            # 僅 base64_data（可附帶空 filename）→ 保留物件
            if keys <= {"base64_data", "filename"} and "base64_data" in item:
                if keys == {"base64_data"} or (
                    keys == {"base64_data", "filename"}
                    and not str(item.get("filename") or "").strip()
                ):
                    out.append({"base64_data": b64})
                    continue
                # 有 filename 的舊格式 → 改純字串
                out.append(b64)
                continue
            out.append(b64)
        else:
            out.append(b64)
    return out


def _normalize_task_type(task_type: str) -> str:
    t = (task_type or "").strip()
    if t in ("manual", "automated_24h"):
        return t
    return "manual"


def _truncate_url(url: str, limit: int = 500) -> str:
    u = (url or "").strip()
    if len(u) > limit:
        logging.warning(f"[WEBHOOK] 已截斷過長 URL ({len(u)} -> {limit} 字元)")
        return u[:limit]
    return u


def build_webhook_payload_from_result(
    res: Dict[str, Any],
    task_type: str = "manual",
    *,
    default_keywords: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """組出符合後端規格的單筆 payload（僅含規定欄位）。"""
    raw_images = (
        res.get("product_images_b64")
        or res.get("product_images_base64")
        or res.get("product_images")
        or []
    )
    images = normalize_product_images(raw_images)
    screenshot = str(res.get("screenshot_b64") or "")
    full_screenshot = str(res.get("full_screenshot_base64") or screenshot)

    keywords = res.get("keywords")
    if not isinstance(keywords, list) or not keywords:
        matched = res.get("matched")
        if isinstance(matched, list) and matched:
            keywords = matched
        else:
            keywords = list(default_keywords or [])

    ts = str(res.get("timestamp") or "").strip()
    if not (len(ts) >= 20 and "T" in ts and ts.endswith("Z")):
        ts = utc_timestamp_iso()

    return {
        "url": _truncate_url(str(res.get("url") or "")),
        "task_type": _normalize_task_type(task_type),
        "timestamp": ts,
        "keywords": [str(k) for k in keywords if str(k).strip()],
        "text_content": str(res.get("text_content") or ""),
        "screenshot_b64": screenshot,
        "full_screenshot_base64": full_screenshot,
        "product_images_b64": images,
    }


def is_negative_access_payload(payload: Dict[str, Any]) -> bool:
    return (payload.get("text_content") or "").strip() == NEGATIVE_ACCESS_TEXT


def build_negative_access_report(url: str, task_type: str = "manual") -> Dict[str, Any]:
    """
    登入/註冊牆無法繼續時的精簡 Webhook 封包：
    { url, task_type, text_content, product_images_b64 }
    """
    return {
        "url": _truncate_url(url),
        "task_type": _normalize_task_type(task_type),
        "text_content": NEGATIVE_ACCESS_TEXT,
        "product_images_b64": [],
        # 本地用標記（finalize 送出前會剔除）
        "login_wall": True,
        "tier": "SKIP",
        "score": 0,
        "matched": [],
        "keywords": [],
    }


def finalize_webhook_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """補齊缺欄、正規化圖片；negative 封包只保留 4 欄。"""
    base = dict(payload or {})
    task_type = _normalize_task_type(str(base.get("task_type") or "manual"))

    if is_negative_access_payload(base):
        return {
            "url": _truncate_url(str(base.get("url") or "")),
            "task_type": task_type,
            "text_content": NEGATIVE_ACCESS_TEXT,
            "product_images_b64": [],
        }

    built = build_webhook_payload_from_result(base, task_type=task_type)

    # 若無 viewport 截圖，用第一張商品圖補（相容舊行為）
    if not built["screenshot_b64"] and built["product_images_b64"]:
        first = built["product_images_b64"][0]
        built["screenshot_b64"] = _extract_b64(first)
    if not built["full_screenshot_base64"]:
        built["full_screenshot_base64"] = built["screenshot_b64"]

    return built


def validate_webhook_payload(payload: Dict[str, Any]) -> Tuple[bool, List[str]]:
    """手動 / 24H 共用；negative 封包只驗 4 欄。"""
    errors: List[str] = []
    if not isinstance(payload, dict):
        return False, ["payload 必須是 object"]

    if is_negative_access_payload(payload):
        for field in ("url", "task_type", "text_content", "product_images_b64"):
            if field not in payload:
                errors.append(f"缺少欄位: {field}")
        url = str(payload.get("url") or "").strip()
        if not url:
            errors.append("url 不可空")
        elif not url.lower().startswith(("http://", "https://")):
            errors.append("url 格式無效")
        elif len(url) > 500:
            errors.append("url 長度不可超過 500")
        if payload.get("task_type") not in ("automated_24h", "manual"):
            errors.append("task_type 必須是 automated_24h 或 manual")
        if (payload.get("text_content") or "").strip() != NEGATIVE_ACCESS_TEXT:
            errors.append("text_content 必須是「非毒品網站或無法登入」")
        if not isinstance(payload.get("product_images_b64"), list):
            errors.append("product_images_b64 必須是 array")
        return len(errors) == 0, errors

    for field in REQUIRED_WEBHOOK_FIELDS:
        if field not in payload:
            errors.append(f"缺少欄位: {field}")
            continue

        value = payload[field]

        if field == "task_type":
            if value not in ("automated_24h", "manual"):
                errors.append("task_type 必須是 automated_24h 或 manual")
        elif field == "timestamp":
            if not isinstance(value, str) or not value.strip():
                errors.append("timestamp 不可空")
            elif not (len(value) >= 20 and "T" in value and value.endswith("Z")):
                errors.append("timestamp 必須是 ISO 格式，例如 2026-08-14T22:17:40Z")
        elif field == "url":
            if not isinstance(value, str) or not value.strip():
                errors.append("url 不可空")
            elif not value.strip().lower().startswith(("http://", "https://")):
                errors.append("url 格式無效")
            elif len(value) > 500:
                errors.append("url 長度不可超過 500")
        elif field == "keywords":
            if not isinstance(value, list) or not any(
                isinstance(k, str) and k.strip() for k in value
            ):
                errors.append("keywords 不可空")
        elif field in ("screenshot_b64", "full_screenshot_base64"):
            if not isinstance(value, str):
                errors.append(f"{field} 必須是字串")
            elif not value.strip():
                errors.append(f"{field} 不可空")
            elif not _is_valid_pure_base64(value):
                errors.append(f"{field} base64 無效")
        elif field == "text_content":
            if not isinstance(value, str) or not value.strip():
                errors.append("text_content 不可空")
        elif field == "product_images_b64":
            if not isinstance(value, list):
                errors.append("product_images_b64 必須是 array")
            else:
                for idx, img in enumerate(value):
                    if isinstance(img, str):
                        if not img.strip():
                            errors.append(f"product_images_b64[{idx}] 字串不可空")
                        elif not _is_valid_pure_base64(img):
                            errors.append(f"product_images_b64[{idx}] base64 無效")
                    elif isinstance(img, dict):
                        b64 = str(img.get("base64_data") or "").strip()
                        if not b64:
                            errors.append(f"product_images_b64[{idx}].base64_data 不可空")
                        elif not _is_valid_pure_base64(b64):
                            errors.append(f"product_images_b64[{idx}].base64_data base64 無效")
                    else:
                        errors.append(
                            f"product_images_b64[{idx}] 必須是 base64 字串或 {{base64_data}} 物件"
                        )

    return len(errors) == 0, errors


class WebhookHelper:
    def __init__(
        self,
        webhook_url: str,
        api_key: str = "",
        config: Optional[Dict[str, Any]] = None,
    ):
        self.webhook_url = webhook_url
        self.api_key = api_key
        self.config = config or {}
        self._failed_path = get_record_paths(self.config).get(
            "json_webhook_failed", "Record/webhook_failed.jsonl"
        )
        self._sent_urls_cache: set = set()
        if not self.webhook_url:
            logging.warning("[WEBHOOK] 未設定後端 URL，發送功能將進入乾跑模式")

    def _write_dead_letter(self, data: Dict[str, Any], reason: str) -> None:
        """失敗只記 log（精簡 Record 後不再獨立 webhook_failed.jsonl）。"""
        logging.warning(
            f"[WEBHOOK] 發送失敗已記錄: {data.get('url')} | {reason}"
        )
        try:
            # 若路徑仍指向獨立 jsonl 則寫入；否則略過（避免污染 log_24h.txt）
            path = self._failed_path or ""
            if path.endswith(".jsonl"):
                os.makedirs(os.path.dirname(path) or "Record", exist_ok=True)
                entry = {
                    "ts": utc_timestamp_iso(),
                    "reason": reason,
                    "url": data.get("url"),
                    "payload_keys": list(data.keys()),
                }
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception as e:
            logging.error(f"[WEBHOOK] 死信記錄失敗: {e}")

    async def send_result(self, data: Dict[str, Any], max_retries: int = 3) -> bool:
        payload = finalize_webhook_payload(data)
        url = str(payload.get("url") or "")

        if url and url in self._sent_urls_cache:
            logging.info(f"[WEBHOOK] 偵測到重複發送，已略過: {url}")
            return True

        ok, errors = validate_webhook_payload(payload)
        if not ok:
            logging.warning(
                f"[WEBHOOK] 封包不完整，已略過發送 ({'; '.join(errors)}) | url={url}"
            )
            self._write_dead_letter(payload, f"validation: {'; '.join(errors)}")
            return False

        if not self.webhook_url:
            logging.info("[WEBHOOK] (乾跑模式) 封包有效，但未設定 URL，不發送")
            return False

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        for attempt in range(max_retries):
            try:
                async with httpx.AsyncClient(timeout=120.0) as client:
                    response = await client.post(
                        self.webhook_url, json=payload, headers=headers
                    )
                    if response.status_code == 200:
                        logging.info(
                            f"[WEBHOOK] 資料傳送成功: {url or 'Unknown URL'}"
                        )
                        if url:
                            self._sent_urls_cache.add(url)
                        return True
                    logging.warning(
                        f"[WEBHOOK] 傳送失敗 ({response.status_code}), "
                        f"準備重試: {response.text[:300]}"
                    )
            except Exception as e:
                logging.error(f"[WEBHOOK] 連線錯誤: {e}, 準備重試")

            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)

        logging.error(
            f"[WEBHOOK] 達到最大重試次數，資料傳送放棄: {url or 'Unknown URL'}"
        )
        self._write_dead_letter(payload, "max_retries_exceeded")
        return False


def get_webhook_helper(config: Dict[str, Any]) -> WebhookHelper:
    global _helper_instance
    if _helper_instance is None:
        webhook_settings = config.get("webhook_settings", {}) or {}
        url = webhook_settings.get(
            "backend_url", config.get("backend_webhook_url", "")
        )
        # 服務間驗證用的 token。環境變數優先，config.json 的 api_key 只當本機
        # 備援——docker-compose 已經把 INTERNAL_API_TOKEN 送進這個容器了，
        # 不該再要求使用者去改一份進版控的 json。
        api_key = os.getenv("INTERNAL_API_TOKEN") or webhook_settings.get("api_key", "")

        # 後端的三個 report 端點現在會驗這個 token。有設 URL 卻沒有 token 的話，
        # 每一筆蒐證資料都會被擋成 401，然後在這裡重試三次、寫進死信檔——
        # 爬蟲看起來一切正常，資料庫卻一筆都不會進。寧可現在就起不來。
        if url and not api_key:
            raise RuntimeError(
                "設定了 webhook backend_url 卻沒有 INTERNAL_API_TOKEN。"
                "後端會把所有回報擋成 401。請在 .env 設定同一組 token。"
            )

        _helper_instance = WebhookHelper(url, api_key, config=config)
    return _helper_instance

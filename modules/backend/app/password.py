"""
密碼雜湊與強度檢查。

以前這裡是四份一模一樣的 hashlib.sha256——dependencies.py、routers/auth.py、
routers/users.py、main.py 各寫一次。無鹽、單輪，彩虹表直接反查，
而 requirements.txt 裡的 passlib 與 bcrypt 從來沒被 import 過。

現在集中在這個檔案，其他地方一律從這裡拿。

舊帳號怎麼辦
────────────
資料庫裡還是 SHA-256 的十六進位字串。直接換成 bcrypt 會把所有人鎖在外面，
所以 verify_password 兩種格式都認：先試 bcrypt，不是的話再比對 SHA-256。
只要是舊格式驗證成功，就回傳 needs_rehash=True，呼叫端負責用 bcrypt 重存一次
（見 routers/auth.py 的登入流程）。使用者無感，登入一次就完成遷移。
"""
import base64
import hashlib
import hmac
import re

import bcrypt

# 直接用 bcrypt，不經過 passlib。
# passlib 從 2020 年就沒有再發版，跟 bcrypt 4.x 不相容
# （它內部的 detect_wrap_bug 會丟一個超過 72 bytes 的密碼進去測試，
#  新版 bcrypt 改成拋 ValueError 而不是靜默截斷，整個登入就 500）。
_ROUNDS = 12   # 再高會讓登入明顯變慢，對這個規模的系統沒有必要


def _prehash(password: str) -> bytes:
    """
    bcrypt 只吃前 72 個「位元組」，超過的部分會被靜默丟掉。
    中文一個字 3 bytes，24 個中文字就滿了——使用者不會知道自己的密碼
    後面那一段其實沒有作用。

    先用 SHA-256 壓成固定長度再交給 bcrypt，就沒有截斷問題，
    也不會有「前 72 bytes 相同的兩組密碼會互通」這種狀況。
    """
    return base64.b64encode(hashlib.sha256(password.encode("utf-8")).digest())

# 跟前端 inputSecurity.ts 的 getPasswordValidationMessage 一致。
# 前端只是即時提示，真正把關在這裡——以前只有前端檢查，直接打 API 就繞過了。
MIN_LENGTH = 8
_RULES = [
    (lambda p: len(p) >= MIN_LENGTH, f"至少 {MIN_LENGTH} 碼"),
    (lambda p: re.search(r"[A-Z]", p), "英文大寫"),
    (lambda p: re.search(r"[a-z]", p), "英文小寫"),
    (lambda p: re.search(r"\d", p), "數字"),
    (lambda p: re.search(r"[^A-Za-z0-9\s]", p), "特殊符號"),
]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prehash(password), bcrypt.gensalt(rounds=_ROUNDS)).decode()


def _is_legacy_sha256(stored: str) -> bool:
    """舊格式是 64 個十六進位字元；bcrypt 是 $2b$ 開頭。"""
    return len(stored) == 64 and all(c in "0123456789abcdef" for c in stored.lower())


def verify_password(plain: str, stored: str) -> tuple[bool, bool]:
    """
    回傳 (密碼正確嗎, 需不需要重新雜湊)。

    needs_rehash 為 True 代表這個帳號還是舊的 SHA-256，
    呼叫端應該趁這次登入把它換成 bcrypt。
    """
    if not stored:
        return False, False

    if _is_legacy_sha256(stored):
        # compare_digest 避免時間差比對
        ok = hmac.compare_digest(
            hashlib.sha256(plain.encode("utf-8")).hexdigest(), stored.lower())
        return ok, ok

    try:
        return bcrypt.checkpw(_prehash(plain), stored.encode()), False
    except (ValueError, TypeError):
        # 格式壞掉或無法辨識，一律當成驗證失敗
        return False, False


def validate_strength(password: str) -> str | None:
    """不合格就回傳給使用者看的訊息，合格回 None。"""
    missing = [label for check, label in _RULES if not check(password)]
    if missing:
        return "密碼格式不符，請加入：" + "、".join(missing) + "。"
    return None

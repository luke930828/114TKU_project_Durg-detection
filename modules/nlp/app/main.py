import os
import logging
import httpx
import torch
import torch.nn.functional as F
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from typing import List, Optional

# ── 模型設定 ──────────────────────────────────────────────────────────────────
# 微調過的模型放在 Hugging Face Hub，容器啟動時直接下載，不用把 1GB+ 權重包進 image
MODEL_ID = os.getenv("MODEL_ID", "matt0513/drug-detection-xlm-roberta")

# 後端主系統位址（docker-compose 網路裡用 service name，本機測試可覆寫）
BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "http://backend:8000")
BACKEND_NLP_URL = f"{BACKEND_BASE_URL}/api/nlp/report/"

# 服務間驗證。沒設就直接爆掉——如果讓它用空字串跑下去，模型會照常推論、
# 回推卻每次都被後端擋成 401，而 push_to_backend 只記 warning，
# 表面上一切正常，實際上分析結果一筆都沒進資料庫。
INTERNAL_API_TOKEN = os.environ["INTERNAL_API_TOKEN"]

# ── 啟動時只載入一次模型 ─────────────────────────────────────────────────────
device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
tokenizer = AutoTokenizer.from_pretrained(MODEL_ID)
model = AutoModelForSequenceClassification.from_pretrained(
    MODEL_ID,
    attn_implementation="eager",  # sdpa 不支援 output_attentions，改用 eager
)
model.config.output_attentions = True
model.to(device)
model.eval()

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nlp_api")

# ── FastAPI 應用程式 ───────────────────────────────────────────────────────────
app = FastAPI(title="毒品黑話偵測 API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ── 資料格式定義 ───────────────────────────────────────────────────────────────
class PredictRequest(BaseModel):
    url: str            # 被掃描的網址（送給後端用）
    text: str           # 要偵測的文字內容
    # 這支端點預設會自己把結果回寫到後端的 /api/nlp/report/。
    # 但「同一個網址要跑第二次」的時候不能這樣——後端拿 OCR 文字再判一次時，
    # 如果 NLP 自己就把結果寫回去，圖片文字的分數會直接蓋掉網頁文字的分數，
    # 而圖片文字通常比較零碎、分數偏低，等於愈補愈糟。
    # 這種情況由呼叫端設 report=False，自己決定要怎麼合併。
    report: bool = True
    # 截斷長度。預設 256 維持原本的行為不變。
    # 後端把「圖片文字 + 網頁文字」合併後再送一次時會指定 512（XLM-R 的上限），
    # 不然 OCR 佔掉的額度會把網頁文字擠出視窗，等於用更少的網頁內容重判一次。
    max_length: int = 256


class PredictResponse(BaseModel):
    label: str          # "DRUG" 或 "SAFE"
    score: float        # 毒品機率，0.0 ~ 1.0
    keywords: List[str] # 觸發判斷的關鍵字


# ── 關鍵字提取（透過 Attention 權重）────────────────────────────────────────────
# 這個模型是 XLM-RoBERTa，用 SentencePiece 切詞，一個「字」常常被切成好幾片：
#   dispensary → ▁di + spen + sa + ry
# 舊版直接把單一 token decode 出來當關鍵字，所以畫面上會出現 ana、pensa、ed、BU
# 這種看不懂的碎片——實測 58% 的關鍵字長度 ≤3 個字元。
# ▁ 是 SentencePiece 的「字首」標記，要靠它把碎片組回完整的字。
WORD_START = "\u2581"

# CLS 的 attention 天生會集中在功能詞上（attention sink），沒有這張表的話
# 前五名經常被 the / and / of 佔滿——實測停用詞佔 29%。
# 刻意寫死在這裡而不是用 nltk/spacy：那兩個都要在啟動時下載語料，
# 容器沒網路就起不來，為了一張停用詞表不值得。
STOPWORDS = frozenset("""
a an the this that these those there here
and or but nor so yet if then else than as
is am are was were be been being do does did done
have has had having will would shall should can could may might must
i you he she it we they me him her us them my your his its our their
of in on at to for from by with about into over under between through during
no not only own same too very just also more most other some such
what which who whom whose when where why how all any both each few
s t don now use used using get got make made take taken
com www http https html org net
的 了 是 在 我 有 和 就 不 人 都 一 上 也 很 到 說 要 去 你 會 著 沒有 看 好 自己 這
""".split())


# 電商樣板用語。這些字在每一頁都出現、attention 也不低，但對「這是不是毒品
# 網站」毫無資訊量——實測它們佔關鍵字欄位的 11%。
#
# 註：試過改用「出現次數加總」來壓過它們，結果更糟——重複最多次的正是橫幅與
# 頁尾（SITEWIDE、FREE SHIPPING、MONDAY–FRIDAY），六個網頁裡三個變差。
# 所以是列表過濾，不是改計分方式。
UI_NOISE = frozenset("""
cart carts checkout basket shop shops store stores home menu login logout signin signup
account search view browse click press skip content site sitewide page pages next prev
product products item items collection collections category categories tag tags
add remove select sort filter default popularity latest price prices free shipping ship
delivery deliver order orders buy sale sales off discount coupon deal deals gift gifts
review reviews rating ratings star stars contact about faq blog news info help support
policy privacy terms cookie cookies copyright rights reserved subscribe newsletter email
result results showing all more less read learn share follow us we you your our my
alt screen reader enter open close toggle button link image images
monday tuesday wednesday thursday friday saturday sunday
""".split())

# 夾雜這些符號的多半是介面殘留（Alt+0、Alt+1 這種無障礙提示），不是內容詞。
# 連字號刻意不列入——4-MMC、2C-B 這類毒品名稱要留著。
SYMBOL_CHARS = frozenset("+=<>|@#$%^&*/\\~`")


def _merge_subwords(tokens, scores):
    """把 SentencePiece 的碎片組回完整的字，分數取該字所有碎片的平均。"""
    words = []
    for tok, score in zip(tokens, scores):
        if tok in tokenizer.all_special_tokens:
            continue
        if tok.startswith(WORD_START):
            words.append([tok[len(WORD_START):], score, 1])
        elif words:
            words[-1][0] += tok
            words[-1][1] += score
            words[-1][2] += 1
        else:
            # 句首沒有 ▁ 的情況，當成新的字起頭
            words.append([tok, score, 1])
    return [(text, total / n) for text, total, n in words if text]


def _is_meaningful(word: str) -> bool:
    low = word.lower()
    if len(word) < 2 or low in STOPWORDS or low in UI_NOISE:
        return False
    if any(ch in SYMBOL_CHARS for ch in word):
        return False
    # 純標點或純數字不是關鍵字（實測佔 9%）
    return any(ch.isalpha() for ch in word)


def extract_keywords(text: str, top_k: int = 5, max_length: int = 256) -> List[str]:
    """用 CLS token 的 Attention 找出模型最依賴的詞。

    max_length 要跟算分那一次一樣。不一樣的話，標出來的關鍵字會來自
    「模型根本沒看到的那段文字」——分數是 A 段算的，理由卻是 B 段給的。
    """
    encoding = tokenizer(
        text, return_tensors="pt", truncation=True, padding=True,
        max_length=max_length,
    )
    input_ids = encoding["input_ids"][0].tolist()
    inputs = {k: v.to(device) for k, v in encoding.items()}

    with torch.no_grad():
        outputs = model(**inputs, output_attentions=True)

    # (layers, 1, heads, seq, seq) → 取 CLS 那一行，對 head 取平均 → (seq_len,)
    #
    # 只取最後四層。前面幾層的 attention 幾乎是均勻分布的（還在做位置與語法），
    # 全部層一起平均等於拿一堆雜訊去稀釋真正有鑑別力的後段。
    attentions = torch.stack(outputs.attentions)
    n_layers = attentions.shape[0]
    cls_attn = attentions[-min(4, n_layers):, 0, :, 0, :].mean(dim=(0, 1)).cpu().tolist()

    tokens = tokenizer.convert_ids_to_tokens(input_ids)
    words = _merge_subwords(tokens, cls_attn)

    words.sort(key=lambda kv: kv[1], reverse=True)
    seen: set = set()
    keywords: List[str] = []
    for word, _score in words:
        word = word.strip(".,;:!?()[]{}\"'\u3001\u3002\uff0c")
        if not _is_meaningful(word) or word.lower() in seen:
            continue
        seen.add(word.lower())
        keywords.append(word)
        if len(keywords) >= top_k:
            break

    return keywords


# ── 推送結果給後端主系統 ────────────────────────────────────────────────────────
async def push_to_backend(url: str, risk_score: float, nlp_keywords: List[str]) -> None:
    """
    把 NLP 分析結果 POST 給後端主系統的 /api/nlp/report/。
    失敗只記 log，不影響回傳給呼叫方的結果。
    """
    payload = {
        "url": url,
        "risk_score": round(risk_score * 100),  # 後端要整數 0~100
        "nlp_keywords": nlp_keywords,
    }
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                BACKEND_NLP_URL,
                json=payload,
                headers={"X-Internal-Token": INTERNAL_API_TOKEN},
            )
            resp.raise_for_status()
            logger.info(f"推送成功 {url} → {resp.status_code}")
    except Exception as e:
        # 推送失敗不中斷主流程，只記 log
        logger.warning(f"推送後端失敗 ({url}): {e}")


# ── API 端點 ──────────────────────────────────────────────────────────────────
@app.post("/predict", response_model=PredictResponse)
async def predict(req: PredictRequest):
    """
    輸入網址 + 文字，回傳 label/score/keywords，
    並自動把結果推送到後端主系統的 /api/nlp/report/。
    """
    text = req.text.strip()
    if not text:
        return PredictResponse(label="SAFE", score=0.0, keywords=[])

    # 1. 預測
    # 上下限夾住：低於 64 幾乎判不出東西，高於 512 超過 XLM-R 的位置編碼上限。
    max_length = max(64, min(req.max_length, 512))
    encoding = tokenizer(
        text, return_tensors="pt", truncation=True, padding=True,
        max_length=max_length,
    )
    inputs = {k: v.to(device) for k, v in encoding.items()}

    with torch.no_grad():
        outputs = model(**inputs)

    probs = F.softmax(outputs.logits, dim=-1).squeeze().tolist()
    pred_idx = torch.argmax(outputs.logits, dim=-1).item()

    label = "DRUG" if pred_idx == 1 else "SAFE"
    drug_score = round(float(probs[1]), 4) if pred_idx == 1 else 0.0

    # 2. 提取關鍵字（機率 > 0.3 才值得標）
    keywords = extract_keywords(text, max_length=max_length) if drug_score > 0.3 else []

    # 3. 非同步推送給後端主系統（不阻塞回應）
    #    report=False 時不推——呼叫端要自己合併，見 PredictRequest 的說明。
    if req.report:
        await push_to_backend(req.url, drug_score, keywords)

    return PredictResponse(label=label, score=drug_score, keywords=keywords)


@app.get("/health")
def health():
    return {"status": "ok", "device": str(device)}

"""
視覺風險計分模組 (Presence-Based Scoring)

與 api_server.py 解耦：只負責「YOLO 偵測結果 -> 風險分數」的純運算，
不碰任何網路 / 資料庫 / FastAPI 邏輯，方便 NLP 模組或未來的批次分析工具直接複用。
"""

from typing import Dict, Iterable, Optional, Tuple, TypedDict

MAX_VISUAL_SCORE = 100

# 16 個 YOLO 類別基礎權重
CLASS_WEIGHTS: Dict[str, int] = {
    # 高風險群 (70~90)
    "cannabis_logo": 80,
    "illicit_packaging": 80,
    "magic_mushroom": 80,
    "botanical_joints": 85,
    "cannabis_like_herbal": 80,
    # 中高風險群 (40~60)
    "vape_device": 50,
    "vape_cartridge": 50,
    "substance_crystal": 50,
    "substance_powder": 50,
    "syringe": 60,
    # 通用載具/低風險群 (10~30)
    "drug_edible": 20,
    "digital_scale": 20,
    "ziplock_bag": 15,
    "medical_bottle": 15,
    "pills_capsules": 15,
    "pills_tablets": 15,
}

# 組合加成：同圖出現特定類別組合時觸發乘數，多組命中取最大的單一乘數（不疊加）
COMBO_MULTIPLIERS: Tuple[Tuple[frozenset, float], ...] = (
    (frozenset({"drug_edible", "cannabis_logo"}), 2.5),
    (frozenset({"vape_cartridge", "cannabis_logo"}), 2.5),
    (frozenset({"vape_device", "cannabis_logo"}), 2.5),
    (frozenset({"ziplock_bag", "substance_powder"}), 2.0),
    (frozenset({"ziplock_bag", "substance_crystal"}), 2.0),
    (frozenset({"digital_scale", "ziplock_bag"}), 2.0),
)


class ClassDetection(TypedDict):
    count: int
    max_confidence: float


def new_class_metadata() -> Dict[str, ClassDetection]:
    return {}


def record_detection(class_metadata: Dict[str, ClassDetection], class_name: str, confidence: float) -> None:
    """把單一偵測框（class_name, confidence）記入 metadata，供應累加 count / 更新 max_confidence。"""
    if class_name not in CLASS_WEIGHTS:
        return
    entry = class_metadata.setdefault(class_name, {"count": 0, "max_confidence": 0.0})
    entry["count"] += 1
    entry["max_confidence"] = max(entry["max_confidence"], confidence)


def merge_class_metadata(target: Dict[str, ClassDetection], source: Dict[str, ClassDetection]) -> None:
    """把 source 的 count / max_confidence 累加/取最大值 合併進 target（用於跨圖片的批次彙總）。"""
    for name, meta in source.items():
        entry = target.setdefault(name, {"count": 0, "max_confidence": 0.0})
        entry["count"] += meta["count"]
        entry["max_confidence"] = max(entry["max_confidence"], meta["max_confidence"])


def compute_combo_multiplier(present_classes: Iterable[str]) -> Tuple[float, Optional[frozenset]]:
    present = set(present_classes)
    best_multiplier = 1.0
    best_combo = None
    for combo, multiplier in COMBO_MULTIPLIERS:
        if multiplier > best_multiplier and combo.issubset(present):
            best_multiplier = multiplier
            best_combo = combo
    return best_multiplier, best_combo


def compute_visual_risk(class_metadata: Dict[str, ClassDetection]) -> Dict:
    """
    存在即採計：Score_i = Weight_i * Max_Confidence_i，取所有出現類別中最高者為 S_visual_base。
    命中組合加成則以最大單一乘數套用，最終分數封頂 100。
    """
    per_class_scores = {
        name: CLASS_WEIGHTS[name] * meta["max_confidence"]
        for name, meta in class_metadata.items()
        if name in CLASS_WEIGHTS
    }

    if not per_class_scores:
        return {
            "visual_score": 0,
            "base_score": 0.0,
            "top_class": None,
            "combo_multiplier": 1.0,
            "triggered_combo": None,
            "class_metadata": class_metadata,
        }

    top_class = max(per_class_scores, key=per_class_scores.get)
    base_score = per_class_scores[top_class]

    multiplier, combo = compute_combo_multiplier(class_metadata.keys())
    visual_score = min(MAX_VISUAL_SCORE, base_score * multiplier)

    return {
        "visual_score": int(round(visual_score)),
        "base_score": round(base_score, 2),
        "top_class": top_class,
        "combo_multiplier": multiplier,
        "triggered_combo": sorted(combo) if combo else None,
        "class_metadata": class_metadata,
    }

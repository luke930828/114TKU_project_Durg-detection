"""把模型的原始機率校準成「真的代表機率」的數字。

為什麼需要
──────────
分類模型的 softmax 輸出不是機率，是一個 0~1 的信心值。這個模型對多數頁面
給 99% 以上，但那批實際只有約 82% 是毒品站——數字比實際樂觀得多。

結果是門檻很難解釋：校準前「5 分」就已經是明顯偏高的位置，
因為正常網頁大多落在 0.1~0.8 分。「5 分卻是中風險」看起來很怪，
但那不是門檻設錯，是尺度本身沒有意義。

校準之後，分數與實際比例對得上（獨立評估集實測）：
    90-100 分 → 實際 94.7% 是毒品站
    70-89  分 → 68.8%
    50-69  分 → 50.0%
    0-9    分 → 11.0%

用哪批資料校
────────────
訓練時切出的 20% 驗證集（454 筆），模型沒有用它更新過權重。
沒有用 217 筆人工標註評估集，那樣校準與評估會用到同一批資料，
評估結果就不再獨立。

選 Platt 不選 Isotonic
──────────────────────
Isotonic 的校準誤差略低（ECE 0.069 vs 0.096），但它是階梯函數，
會把相近的分數壓成同一階，排序能力因此下降（ROC-AUC 0.900 → 0.890）。
Platt 是單調的連續轉換，ROC-AUC 完全不變——校準只改變數字的意義，
不改變誰排在誰前面。這個系統要靠分數排優先順序，不能犧牲排序。

參數要跟模型一起換
──────────────────
A/B 是對「這一版模型」擬合出來的。換模型就要重新校準，
否則校準會把新模型的分數扭到錯的地方。
"""
import math
import os

# 對 matt0513/drug-detection-xlm-roberta-v3 擬合（2026-09-09，454 筆校準集）
PLATT_A = float(os.getenv("CALIBRATION_A", "0.513222"))
PLATT_B = float(os.getenv("CALIBRATION_B", "0.659438"))
ENABLED = os.getenv("CALIBRATION_ENABLED", "1") not in ("0", "false", "False", "")

_EPS = 1e-7


def calibrate(prob: float) -> float:
    """原始機率 → 校準後機率。關閉校準時原樣回傳。"""
    if not ENABLED:
        return prob
    p = min(max(float(prob), _EPS), 1 - _EPS)
    logit = math.log(p / (1 - p))
    return 1 / (1 + math.exp(-(PLATT_A * logit + PLATT_B)))

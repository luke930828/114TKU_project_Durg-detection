"""把模型的原始機率校準成「真的代表機率」的數字。

softmax 的輸出不是機率，是信心值。這個模型對多數頁面給 99% 以上，
但那批實際只有約 82% 是毒品站，數字比實際樂觀得多。
校準之後 90 分那批實際 94.7%、50 分那批 50.0%，門檻才能照字面理解。

用訓練時切出的 454 筆驗證集擬合，不用那 217 筆評估集——
拿評估集校準的話，評估結果就不再獨立。

選 Platt 不選 Isotonic：Isotonic 誤差略低但是階梯函數，會把相近分數壓成同一階，
ROC-AUC 從 0.900 掉到 0.890。這個系統要靠分數排優先順序，不能犧牲排序。

A/B 是對這一版模型擬合的，換模型要重新校準。
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

"""
Roboflow 匯出資料匯入工具

（類別數與類別名稱一律以 Roboflow 匯出的 data.yaml 為準，這支腳本本身不寫死類別數，
  所以之後增減類別、換版本都不需要改這支腳本，目前對應的是 v3 / 15 類別資料集）

使用方式：
1. 到 Roboflow 專案匯出資料集，格式選 "YOLOv8"，下載並解壓縮
2. 把解壓縮出來的 train/ valid/ test/ 資料夾與 data.yaml 整包放進 data/raw/
   （Roboflow 預設就是 train/valid/test 各自帶 images/ + labels/ 的結構，跟這裡的慣例一致，直接整包丟進去即可）
3. 執行：python -m src.ai_model.import_roboflow

這支腳本會：
- 讀 data/raw/data.yaml 裡 Roboflow 實際匯出的 names 順序，逐一比對 src/ai_model/scoring.py 的
  CLASS_WEIGHTS（16 類權重表的唯一標準來源）。api_server.py 是用「類別名稱」而非數字 ID 對齊分數，
  所以 Roboflow 匯出的 ID 順序本身不重要，但名稱拼字必須完全吻合，否則該類別會在推論時被當成未知類別忽略。
- 驗證通過後，把 images/labels 複製進 data/processed/，並「原封不動」沿用 Roboflow 匯出當下的
  names 順序寫出 data/processed/data.yaml —— 絕對不能自己重排順序，因為每個 label .txt 裡的數字 ID
  是照 Roboflow 匯出當下的順序寫死的，重排會讓訓練資料的類別全部對不上。
"""

import shutil
import sys
from pathlib import Path
from typing import List, Tuple

import yaml

from modules.yolo.app.ai_model.scoring import CLASS_WEIGHTS

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

RAW_DATA_PATH = Path("data/raw")
PROCESSED_DATA_PATH = Path("data/processed")
SUBSETS = ("train", "valid", "test")


def load_roboflow_names(raw_path: Path) -> List[str]:
    yaml_path = raw_path / "data.yaml"
    if not yaml_path.exists():
        raise FileNotFoundError(
            f"找不到 {yaml_path}，請確認 Roboflow 匯出的資料夾（含 data.yaml）已完整解壓縮到 {raw_path}/"
        )
    with open(yaml_path, "r", encoding="utf-8") as f:
        config = yaml.safe_load(f)
    names = config.get("names")
    if not names:
        raise ValueError(f"{yaml_path} 裡讀不到 names 清單，請確認是用 Roboflow 的 YOLOv8 格式匯出。")
    return list(names)


def validate_names(names: List[str]) -> None:
    known = set(CLASS_WEIGHTS)
    unknown = [n for n in names if n not in known]
    missing = sorted(known - set(names))

    if unknown:
        raise ValueError(
            "Roboflow 匯出的類別名稱與 src/ai_model/scoring.py 的 16 類定義對不上，"
            "請回 Roboflow 專案修正類別命名（snake_case，大小寫與底線需完全一致）後重新匯出。\n"
            f"無法辨識的類別: {unknown}\n"
            f"scoring.py 目前定義的 16 類: {sorted(known)}"
        )

    if missing:
        print(f"⚠️ 提醒：這批資料沒有包含以下類別，不影響匯入，但代表目前沒有這些類別的訓練樣本：{missing}")


def copy_subset(subset: str) -> Tuple[int, int]:
    img_src = RAW_DATA_PATH / subset / "images"
    lbl_src = RAW_DATA_PATH / subset / "labels"
    img_dest = PROCESSED_DATA_PATH / subset / "images"
    lbl_dest = PROCESSED_DATA_PATH / subset / "labels"

    if not img_src.exists() or not lbl_src.exists():
        print(f"⚠️ 找不到 {subset} 的 images/labels，跳過。")
        return 0, 0

    img_dest.mkdir(parents=True, exist_ok=True)
    lbl_dest.mkdir(parents=True, exist_ok=True)

    img_count = 0
    for img_file in img_src.iterdir():
        if img_file.is_file():
            shutil.copy2(img_file, img_dest / img_file.name)
            img_count += 1

    lbl_count = 0
    for lbl_file in lbl_src.iterdir():
        if lbl_file.is_file():
            shutil.copy2(lbl_file, lbl_dest / lbl_file.name)
            lbl_count += 1

    return img_count, lbl_count


def write_data_yaml(names: List[str]) -> None:
    config = {
        "path": "data/processed",
        "train": "train/images",
        "val": "valid/images",
        "test": "test/images",
        "nc": len(names),
        "names": names,
    }
    with open(PROCESSED_DATA_PATH / "data.yaml", "w", encoding="utf-8") as f:
        yaml.safe_dump(config, f, allow_unicode=True, sort_keys=False)


def main() -> None:
    names = load_roboflow_names(RAW_DATA_PATH)
    validate_names(names)
    print(f"✅ 類別名稱驗證通過，共 {len(names)} 類（依 Roboflow 匯出順序）：{names}")

    total_img, total_lbl = 0, 0
    for subset in SUBSETS:
        img_count, lbl_count = copy_subset(subset)
        print(f"📦 {subset}: 複製 {img_count} 張圖片、{lbl_count} 個標籤")
        total_img += img_count
        total_lbl += lbl_count

    if total_img == 0:
        print("🚨 沒有複製到任何圖片，請確認 data/raw/ 底下有 train/valid/test/images 與 labels。")
        sys.exit(1)

    write_data_yaml(names)
    print(f"\n🚀 匯入完成！共 {total_img} 張圖片、{total_lbl} 個標籤已複製到 {PROCESSED_DATA_PATH}/")
    print(f"   已依 Roboflow 實際類別順序寫入 {PROCESSED_DATA_PATH / 'data.yaml'}，可以直接執行 src/ai_model/train.py 開始訓練。")


if __name__ == "__main__":
    main()

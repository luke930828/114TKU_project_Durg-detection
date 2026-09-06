"""
YOLO 訓練腳本 (16 類別 / v2)

用法：
    python -m src.ai_model.train              # 從 base_model 開始全新訓練
    python -m src.ai_model.train --resume      # 接續 runs/detect/<name>/weights/last.pt 繼續跑

訓練資料預設讀 data/processed/data.yaml（由 src/ai_model/import_roboflow.py 匯入 Roboflow 標註產生）。
訓練完成後，會自動把這次跑出來的 best.pt 複製到 models/best.pt —— 也就是 api_server.py 實際載入推論用的那份權重。
"""

import argparse
import os

# 4GB 顯存的卡很容易因為記憶體碎片化在第一個 batch 就 ptxas/CUDA allocation 失敗，
# 這個環境變數要在 torch 被 import、建立 CUDA context 之前設定才會生效。
os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Union

import torch
import yaml
from ultralytics import YOLO

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


@dataclass
class TrainConfig:
    base_model: str = "models/yolo11n.pt"        # 訓練起點的基礎模型
    data_yaml: str = "data/processed/data.yaml"  # 16 類別訓練資料清單
    epochs: int = 150                            # 資料量不大 + 類別嚴重不平衡，拉高上限交給 patience 決定何時停
    patience: int = 30                           # mAP50-95 連續 30 epoch 沒進步就自動早停，避免對多數類過擬合
    imgsz: int = 640                             # Roboflow 匯出的圖片本身就是 640x640，不需要再放大
    batch: Union[int, float] = 0.3                # AutoBatch，只用 30% 顯存當目標（4GB 卡太小，預設 60% 實測仍會爆）
    workers: int = 2                              # DataLoader 平行 worker 數，預設 8 個 OpenCV 子行程在這台機器上會爆記憶體
    device: Union[int, str] = field(default_factory=lambda: 0 if torch.cuda.is_available() else "cpu")
    project: str = ""                             # 留空即可：Ultralytics 對相對路徑的 project 會自動加上 runs/<task>/ 前綴，
                                                   # 這裡若填 "runs/detect" 會被重複套用變成 runs/detect/runs/detect/<name>
    name: str = "drug_prevention_v2"             # 這次實驗的名稱
    exist_ok: bool = True                        # 名稱重複時直接覆蓋
    deploy_dir: Path = Path("models")            # 最終權重要部署到哪個資料夾
    deploy_filename: str = "best.pt"             # api_server.py 實際載入的檔名
    low_sample_threshold: int = 100              # 訓練集裡樣本數低於此值的類別，開訓前會被列為警示
    resume: bool = False                         # True 時接續 runs/detect/<name>/weights/last.pt 繼續跑，
                                                  # 而不是從 base_model 重新開始（optimizer 狀態、LR 排程、epoch 計數都會照舊接續）


def resolve_resume_checkpoint(config: TrainConfig) -> Path:
    return Path("runs/detect") / config.name / "weights" / "last.pt"


def describe_device(device: Union[int, str]) -> str:
    return f"NVIDIA GPU (device={device})" if device != "cpu" else "CPU（訓練時間會長很多，建議先確認 CUDA 是否安裝正確）"


def backup_existing_weights(dest: Path) -> None:
    if not dest.exists():
        return
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    backup_path = dest.with_name(f"{dest.stem}_backup_{timestamp}{dest.suffix}")
    shutil.move(str(dest), str(backup_path))
    print(f"🗂️ 偵測到既有的 {dest.name}，已備份至: {backup_path}")


def deploy_weights(source: Path, config: TrainConfig) -> Path:
    if not source.exists():
        raise FileNotFoundError(f"找不到訓練產出的權重檔: {source}")

    config.deploy_dir.mkdir(parents=True, exist_ok=True)
    dest = config.deploy_dir / config.deploy_filename

    backup_existing_weights(dest)
    shutil.copy2(source, dest)
    return dest


def summarize_class_distribution(data_yaml: str) -> Dict[str, int]:
    yaml_path = Path(data_yaml)
    config = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    names = config["names"]
    train_labels_dir = yaml_path.parent / "train" / "labels"

    counts = {name: 0 for name in names}
    for label_file in train_labels_dir.glob("*.txt"):
        for line in label_file.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if not parts:
                continue
            class_id = int(parts[0])
            if 0 <= class_id < len(names):
                counts[names[class_id]] += 1
    return counts


def print_dataset_health_check(config: TrainConfig) -> None:
    counts = summarize_class_distribution(config.data_yaml)
    total = sum(counts.values())

    print("\n📊 [資料健檢]", end=" ")
    if total == 0:
        print("讀不到任何標籤，請確認 data/processed 是否已匯入資料。")
        return

    print(f"訓練集共 {total} 個標註框，橫跨 {len(counts)} 類別")
    low_sample = {name: c for name, c in counts.items() if c < config.low_sample_threshold}
    if low_sample:
        print(f"   ⚠️ 以下 {len(low_sample)} 個類別樣本數 < {config.low_sample_threshold}，訓練時這些類別的 AP 可能偏低或不穩定，建議優先補標：")
        for name, c in sorted(low_sample.items(), key=lambda item: item[1]):
            print(f"      - {name}: {c} 個標註框")


def describe_batch(batch: Union[int, float]) -> str:
    if batch == -1:
        return "AutoBatch（目標使用 60% 顯存）"
    if isinstance(batch, float) and 0.0 < batch < 1.0:
        return f"AutoBatch（目標使用 {batch * 100:.0f}% 顯存）"
    return str(batch)


def print_training_banner(config: TrainConfig) -> None:
    print("=" * 60)
    if config.resume:
        print("⏯️ [接續訓練] 防毒影像辨識模型 - 從上次中斷的地方繼續")
        print(f"   接續檢查點 : {resolve_resume_checkpoint(config)}")
    else:
        print("🚀 [訓練啟動] 防毒影像辨識模型 - 16 類別訓練流程")
        print(f"   基礎模型   : {config.base_model}")
    print(f"   訓練資料   : {config.data_yaml}")
    print(f"   Epochs     : {config.epochs} (patience={config.patience})")
    print(f"   Batch Size : {describe_batch(config.batch)}")
    print(f"   Image Size : {config.imgsz}")
    print(f"   Workers    : {config.workers}")
    print(f"   運算裝置   : {describe_device(config.device)}")
    print(f"   實驗紀錄   : runs/detect/{config.name}（實際路徑以訓練結束時印出的為準）")
    print("=" * 60)


def print_training_summary(final_weights_path: Path, run_dir: Path, metrics, data_yaml: str) -> None:
    print("\n" + "=" * 60)
    print("✅ [訓練完成] 模型已成功訓練並部署！")
    print(f"   最終權重已儲存至 : {final_weights_path.resolve()}")
    print(f"   訓練紀錄與圖表   : {run_dir.resolve()}")

    results_dict = getattr(metrics, "results_dict", None) if metrics is not None else None
    if results_dict:
        map50 = results_dict.get("metrics/mAP50(B)")
        map50_95 = results_dict.get("metrics/mAP50-95(B)")
        if map50 is not None:
            print(f"   評估結果         : mAP50 = {map50:.4f}, mAP50-95 = {map50_95:.4f}")
    else:
        print("   評估結果         : 未取得驗證指標，可自行執行下方指令補跑驗證")

    print(f"   👉 建議驗證指令   : yolo val model={final_weights_path} data={data_yaml}")
    print("=" * 60)


def finalize_training(model: YOLO, config: TrainConfig, metrics) -> Path:
    trainer = model.trainer
    weights_source = trainer.best if trainer.best.exists() else trainer.last
    final_weights_path = deploy_weights(weights_source, config)
    print_training_summary(final_weights_path, trainer.save_dir, metrics, config.data_yaml)
    return final_weights_path


def train_model(config: TrainConfig = None) -> Path:
    config = config or TrainConfig()

    print_training_banner(config)
    print_dataset_health_check(config)

    # 接續訓練的重點：從「上次的 last.pt」載入模型，並用 resume=True 呼叫 train()。
    # Ultralytics 會自動從該 checkpoint 存的 args.yaml 還原 data/epochs 等設定、
    # optimizer 狀態、LR 排程進度、epoch 計數，只有下面這幾個參數可以在 resume 時覆寫
    # （imgsz/batch/workers/device/patience 都在允許覆寫的白名單內，其餘傳了也會被忽略）。
    if config.resume:
        resume_checkpoint = resolve_resume_checkpoint(config)
        if not resume_checkpoint.exists():
            raise FileNotFoundError(
                f"找不到可接續的訓練紀錄: {resume_checkpoint}\n"
                f"請確認 TrainConfig.name（目前是 '{config.name}'）是不是跟上次中斷的實驗名稱一致，"
                f"或把 resume 改回 False 從頭開始。"
            )
        model = YOLO(str(resume_checkpoint))
    else:
        model = YOLO(config.base_model)

    metrics = None
    interrupted = False
    try:
        if config.resume:
            metrics = model.train(
                resume=True,
                imgsz=config.imgsz,
                batch=config.batch,
                workers=config.workers,
                device=config.device,
                patience=config.patience,
            )
        else:
            metrics = model.train(
                data=config.data_yaml,
                epochs=config.epochs,
                patience=config.patience,
                imgsz=config.imgsz,
                batch=config.batch,
                workers=config.workers,
                device=config.device,
                project=config.project,
                name=config.name,
                exist_ok=config.exist_ok,
            )
    except KeyboardInterrupt:
        # except 區塊只做最少的事（設旗標、印一行訊息）就馬上跳出去，
        # 部署動作統一放在 try/except 外面、不管有沒有被中斷都會執行——
        # 避免部署過程中如果使用者又按了第二次 Ctrl+C，把整段部署邏輯連本帶利吃掉。
        interrupted = True
        print("\n⏹️ [手動中斷] 偵測到 Ctrl+C，訓練提前結束，嘗試部署最後一個完整跑完的 epoch...")

    # Ultralytics 每個 epoch 結束才會把 weights/last.pt（與可能的 best.pt）寫到磁碟，
    # 不管是正常訓練完，還是中途被 Ctrl+C 打斷，只要硬碟上已經有 checkpoint 就一定嘗試部署。
    trainer = getattr(model, "trainer", None)
    has_checkpoint = trainer is not None and (trainer.best.exists() or trainer.last.exists())

    if not has_checkpoint:
        if interrupted:
            print("🚨 [手動中斷] 連第一個 epoch 都還沒跑完存檔，沒有可用的權重可以部署。")
            raise KeyboardInterrupt
        raise RuntimeError("訓練流程結束但找不到任何權重檔，請檢查上面的錯誤訊息。")

    final_weights_path = finalize_training(model, config, metrics)
    if interrupted:
        print("⚠️ 這是手動中斷後的部署，上面的評估結果是「最後一次驗證」的數字，不代表完整訓練跑完後的最終結果。")
    return final_weights_path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="YOLO 訓練腳本 (16 類別 / v2)")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="接續 runs/detect/<name>/weights/last.pt 繼續跑，而不是從 base_model 重新開始訓練",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    train_model(TrainConfig(resume=args.resume))

"""算兩條 ROC 曲線：同源驗證集 vs 真實評估集。

同源驗證集：完全照 train_bert.py 的切法重現（balance → train_test_split，
random_state 都是 42），再用現行線上模型跑一次推論。
真實評估集：直接用 eval_sample_ALL.csv 裡已經算好的 nlp_score。
"""
import glob, json, os
import numpy as np, pandas as pd, torch
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_curve, roc_auc_score
from transformers import AutoTokenizer, AutoModelForSequenceClassification

ROOT = "/work"
MODEL_ID = os.getenv("MODEL_ID", "matt0513/drug-detection-xlm-roberta")

# ---------- 1. 重現 train_bert.py 的同源驗證集 ----------
frames = []
for f in sorted(glob.glob(f"{ROOT}/data/processed/*.csv")):
    t = pd.read_csv(f)
    if "text" in t.columns and "label" in t.columns:
        frames.append(t[["text", "label"]])
df = pd.concat(frames, ignore_index=True).dropna(subset=["text", "label"])
counts = df.label.value_counts().to_dict()
min_count = int(min(counts.values()))
df = df.groupby("label", group_keys=False).sample(n=min_count, random_state=42).reset_index(drop=True)
_, val_texts, _, val_labels = train_test_split(
    df.text.astype(str).tolist(), df.label.astype(int).tolist(),
    test_size=0.2, random_state=42, stratify=df.label.astype(int).tolist())
print(f"原始各類別筆數 {counts}　平衡後每類 {min_count}　驗證集 {len(val_texts)} 筆", flush=True)

# ---------- 2. 用現行線上模型跑推論 ----------
tok = AutoTokenizer.from_pretrained(MODEL_ID)
model = AutoModelForSequenceClassification.from_pretrained(MODEL_ID)
model.eval()
probs = []
B = 16
for i in range(0, len(val_texts), B):
    enc = tok(val_texts[i:i+B], return_tensors="pt", truncation=True,
              padding=True, max_length=256)
    with torch.no_grad():
        logits = model(**enc).logits
    probs.extend(torch.softmax(logits, dim=-1)[:, 1].tolist())
    print(f"  推論 {min(i+B, len(val_texts))}/{len(val_texts)}", flush=True)

y_same = np.array(val_labels); s_same = np.array(probs)

# ---------- 3. 真實評估集 ----------
ev = pd.read_csv(f"{ROOT}/data/eval_sample/eval_sample_ALL.csv").dropna(subset=["label", "nlp_score"])
y_real = ev.label.astype(int).values
s_real = ev.nlp_score.astype(float).values / 100.0

out = {}
for name, y, s in (("same_source", y_same, s_same), ("real_eval", y_real, s_real)):
    fpr, tpr, thr = roc_curve(y, s)
    out[name] = {
        "auc": float(roc_auc_score(y, s)),
        "n": int(len(y)), "pos": int(y.sum()), "neg": int((y == 0).sum()),
        "fpr": [round(float(x), 5) for x in fpr],
        "tpr": [round(float(x), 5) for x in tpr],
    }
    print(f"{name}: n={len(y)} pos={int(y.sum())} neg={int((y==0).sum())} AUC={out[name]['auc']:.4f}", flush=True)

json.dump(out, open("/work/roc_out.json", "w"))
print("寫出 /work/roc_out.json")

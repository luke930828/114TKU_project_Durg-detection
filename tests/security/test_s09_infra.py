"""部署與執行環境。"""
import json
import subprocess
from pathlib import Path

import pytest
from conftest import known_vuln

pytestmark = pytest.mark.security

REPO = Path(__file__).resolve().parents[2]


def _health(service):
    """問 docker 這個服務的健康狀態。拿不到就跳過（可能不是用 compose 起的）。"""
    try:
        cid = subprocess.run(
            ["docker", "compose",
             "-f", "deploy/docker-compose.yml",
             "-f", "tests/docker-compose.test.yml",
             "--env-file", ".env.local", "ps", "-q", service],
            capture_output=True, text=True, timeout=30, cwd="..",
        ).stdout.strip()
        if not cid:
            pytest.skip(f"找不到 {service} 容器")
        out = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State.Health}}", cid],
            capture_output=True, text=True, timeout=30,
        ).stdout.strip()
    except OSError:
        # FileNotFoundError 也是 OSError；docker 不在或叫不動時都走這裡
        pytest.skip("這個環境叫不動 docker 指令")
    if out in ("", "null"):
        return None
    return json.loads(out)


@pytest.mark.parametrize("service", ["backend", "mysql"])
def test_core_services_healthy(service):
    h = _health(service)
    if h is None:
        pytest.skip(f"{service} 沒有定義 healthcheck")
    assert h["Status"] == "healthy", f"{service} 的健康狀態是 {h['Status']}"


@known_vuln("INFRA-01")
def test_frontend_healthcheck_passes():
    """
    nginx 只監聽 IPv4，但 healthcheck 用 localhost（會先解析成 ::1），
    所以 frontend 永遠 unhealthy——即使它其實好好的。
    """
    h = _health("frontend")
    if h is None:
        pytest.skip("frontend 沒有定義 healthcheck")
    assert h["Status"] == "healthy", (
        f"frontend 健康狀態是 {h['Status']}，但服務本身其實是通的："
        f"{h['Log'][-1]['Output'].strip()[:120] if h.get('Log') else ''}"
    )


@known_vuln("INFRA-02")
def test_record_append_does_not_rewrite_whole_file():
    """
    append_images_record / append_nlp_record 不該把整個檔案讀進來再整份寫回。

    2026-08-29 這個寫法讓 images.json 長到 934 MB，爬蟲每頁產生約 1.9 GB 的
    磁碟 I/O、佔用 4.5 GB 記憶體，最後記憶體耗盡 OOM，Chromium 崩潰後
    WSL 寫出 191.7 GB 的核心傾印。

    正確作法是 open(path, "a") 逐行追加（JSONL）。
    """
    import ast
    src_path = REPO / "modules/crawler/app/record_paths.py"
    if not src_path.exists():
        pytest.skip("找不到 record_paths.py")

    tree = ast.parse(src_path.read_text(encoding="utf-8"))
    offenders = []
    for fn in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
               and n.name.startswith("append_")]:
        calls = {n.func.id for n in ast.walk(fn)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        if "_load_json_list" in calls or "_save_json_list" in calls:
            offenders.append(f"{fn.name}()（第 {fn.lineno} 行）")

    assert not offenders, (
        "這些函式每次追加都會重寫整個檔案，檔案越大越慢：\n  "
        + "\n  ".join(offenders)
        + "\n改用 open(path, 'a') 逐行追加即可。"
    )


@known_vuln("INFRA-03")
def test_services_have_memory_limits():
    """
    每個服務都要有記憶體上限，否則失控的模組會拖垮整台機器——
    而且 OOM killer 殺的往往不是肇事者（當天被殺的是 nlp 和 yolo，
    真正吃掉 4.5 GB 的是 crawler）。
    """
    import re
    compose = REPO / "deploy/docker-compose.yml"
    text = compose.read_text(encoding="utf-8")

    # 服務區塊拆開來看，每個都要有 mem_limit 或 deploy.resources.limits.memory
    blocks = re.split(r"\n  (?=\w[\w-]*:\n)", text)
    missing = []
    for b in blocks:
        m = re.match(r"\n?  ([\w-]+):", b)
        if not m or m.group(1) in ("volumes", "networks", "services"):
            continue
        if "mem_limit" not in b and "memory:" not in b:
            missing.append(m.group(1))

    assert not missing, "這些服務沒有設記憶體上限：" + "、".join(missing)


@known_vuln("ML-01")
def test_model_evaluated_against_real_labels():
    """
    模型必須用人工標註的真實網頁驗收，不能只看跟訓練資料同源的驗證集。

    同源驗證集會給出 0.998，真實表現是 0.879——差 0.12。
    只看前者的話，會把更差的模型當成改善推上線。
    """
    import csv
    ev = REPO / "data/eval_sample/eval_sample_ALL.csv"
    assert ev.exists(), "找不到人工標註的評估集 data/eval_sample/"

    with ev.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    labelled = [r for r in rows if (r.get("label") or "").strip() not in ("", "nan")]
    assert len(labelled) >= 200, f"標註樣本只有 {len(labelled)} 筆，不足以當驗收標準"

    domains = {r["domain"] for r in labelled}
    assert len(domains) >= 100, f"只涵蓋 {len(domains)} 個網域，代表性不足"

    pos = sum(1 for r in labelled if r["label"].strip().startswith("1"))
    ratio = pos / len(labelled)
    assert 0.3 <= ratio <= 0.7, f"正負樣本失衡（正樣本佔 {ratio:.0%}），評估會失真"


@known_vuln("ML-01")
def test_training_uses_metrics_not_just_loss():
    """train_bert.py 沒有 compute_metrics 的話，評估只印 loss，看不出模型好壞。"""
    src = REPO / "src/bert_train/train_bert.py"
    if not src.exists():
        pytest.skip("找不到 train_bert.py（/src/ 不在版控裡）")
    text = src.read_text(encoding="utf-8")
    assert "compute_metrics" in text, "訓練腳本沒有計算 accuracy / f1 / auc"
    assert "roc_auc" in text, "沒有計算 ROC-AUC，無法比較不同版本的排序能力"


@known_vuln("ML-02")
def test_nlp_keywords_are_whole_words_not_subwords():
    """
    extract_keywords 必須把 SentencePiece 的碎片組回完整的字，並過濾停用詞。

    這裡驗原始碼而不是實際輸出：stub 測試環境沒有真的 NLP 服務
    （真模型要下載 1 GB 權重又要 GPU）。要驗實際關鍵字品質請跑 make test-full。

    判斷依據是三個具體作法：
      1. convert_ids_to_tokens——拿得到 ▁ 字首標記才有辦法把碎片組回完整的字。
         舊版用 tokenizer.decode([token_id])，單一 token 解出來必然是碎片。
      2. 有停用詞表——CLS attention 天生集中在功能詞上，沒表就會被 the/and/of 佔滿。
      3. attention 不是對全部層取平均——前面幾層分布幾乎均勻，會稀釋後段的訊號。
    """
    src_path = REPO / "modules/nlp/app/main.py"
    if not src_path.exists():
        pytest.skip("找不到 nlp/app/main.py")
    src = src_path.read_text(encoding="utf-8")

    missing = []
    if "convert_ids_to_tokens" not in src:
        missing.append("沒有用 convert_ids_to_tokens 取回帶 ▁ 標記的 token，組不回完整的字")
    if "STOPWORDS" not in src:
        missing.append("沒有停用詞表，關鍵字會被 the / and / of 佔滿")
    if "attentions[:, 0, :, 0, :]" in src:
        missing.append("attention 仍對全部層取平均，前段的均勻分布會稀釋後段訊號")

    assert not missing, "NLP 關鍵字抽取仍有問題：\n  " + "\n  ".join(missing)


@known_vuln("INFRA-04")
def test_record_file_does_not_store_base64():
    """
    寫進記錄檔的圖片紀錄不可以含 base64。

    商品圖後端已經存進 suspect_websites.images_data（routers/crawler.py:78），
    記錄檔再存一份是重複的，而且佔了 images.json 的 92.5%
    （實測 219 筆：426.8 MB / 461.5 MB，磁碟成長 45 MB/分）。

    直接呼叫 slim_images_record 驗行為，不去檢查呼叫端怎麼寫——瘦身放在
    record_paths 內部或呼叫端都可以，重點是 base64 不能落到檔案裡。
    record_paths.py 只 import 標準函式庫，測試環境不需要爬蟲的相依套件。
    """
    import json as _json
    import sys as _sys

    app_dir = REPO / "modules/crawler/app"
    if not (app_dir / "record_paths.py").exists():
        pytest.skip("找不到 record_paths.py")
    _sys.path.insert(0, str(app_dir))
    try:
        import record_paths
    finally:
        _sys.path.remove(str(app_dir))

    assert hasattr(record_paths, "slim_images_record"), (
        "record_paths 沒有 slim_images_record——圖片紀錄沒有經過瘦身"
    )

    shot, full, prod, raw = "A" * 500, "B" * 500, "C" * 500, "D" * 500
    slim = record_paths.slim_images_record({
        "timestamp": "2026-08-31 00:00:00",
        "url": "https://itest.invalid/",
        "tier": "HIGH",
        "score": 100,
        "screenshot_b64": shot,
        "full_screenshot_base64": full,
        "product_images": [{"filename": "a.jpg", "base64_data": prod}, raw],
    })
    blob = _json.dumps(slim, ensure_ascii=False)

    leaked = [name for name, val in
              (("screenshot_b64", shot), ("full_screenshot_base64", full),
               ("product_images[].base64_data", prod), ("product_images[] 純字串", raw))
              if val[:100] in blob]
    assert not leaked, (
        "這些 base64 仍然會被寫進記錄檔：" + "、".join(leaked)
        + f"\n瘦身後的內容：{blob[:200]}"
    )

    for field in ("url", "timestamp"):
        assert slim.get(field), f"瘦身後遺失 {field}，紀錄就失去追溯價值了"


@known_vuln("INFRA-05")
def test_services_disable_core_dumps():
    """
    每個服務都要設 ulimits.core: 0。

    WSL 的 core_pattern 是 |/wsl-capture-crash，導向管線時核心不套用
    RLIMIT_CORE 的大小上限，崩潰的行程會把整個位址空間寫出來。
    2026-08-31 Chromium 崩一次寫了 172 GB（370 MB/s），8/29 那次 191.7 GB。
    """
    compose = REPO / "deploy/docker-compose.yml"
    if not compose.exists():
        pytest.skip("找不到 docker-compose.yml")
    import yaml
    conf = yaml.safe_load(compose.read_text(encoding="utf-8"))

    missing = []
    for name, svc in (conf.get("services") or {}).items():
        core = ((svc.get("ulimits") or {}).get("core"))
        if isinstance(core, dict):
            core = core.get("hard", core.get("soft"))
        if core != 0:
            missing.append(name)

    assert not missing, (
        "這些服務沒有關閉 core dump：" + "、".join(missing)
        + "\n崩潰時會寫出跟記憶體一樣大的傾印檔，WSL 上沒有大小上限。"
    )


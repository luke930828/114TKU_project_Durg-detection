FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

# ---- 第一層：torch + CUDA（4.5 GB，幾乎不會變）----
# 分開一層的理由見 requirements-torch.txt。cache mount 讓已下載的 wheel 留在
# BuildKit 快取裡，換版本時不用再從 pypi.nvidia.com 重拉（跟 yolo 共用同一份快取）。
# 用了 cache mount 就不要再加 --no-cache-dir，兩者互相抵消。
COPY requirements-torch.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements-torch.txt \
        --extra-index-url https://download.pytorch.org/whl/cu128

# ---- 第二層：其餘套件（常改，但都很小）----
COPY requirements.txt .
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install -r requirements.txt

COPY app/ /app/

# 模型從 Hugging Face Hub 下載（MODEL_ID 環境變數），不打包進 image。
# 下載後的快取請掛 volume 到 /root/.cache/huggingface，避免每次重啟都重新下載。

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]

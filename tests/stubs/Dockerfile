FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir fastapi==0.115.6 uvicorn==0.34.0 requests==2.32.3
COPY stub_app.py .
# PORT 由 compose 指定（nlp:8000 / yolo:5000 / crawler:8001）
ENV PORT=8000
CMD ["sh", "-c", "uvicorn stub_app:app --host 0.0.0.0 --port ${PORT}"]

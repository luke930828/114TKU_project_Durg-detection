"""
本機開發用啟動腳本，不會進 Docker image（不在 modules/yolo/ 底下，不會被 COPY 進去）。

main.py 現在對齊 docker_branch 的作法：BACKEND_BASE_URL / INTERNAL_API_TOKEN 沒設就直接
fail-fast 炸掉（避免忘記設定時把資料送到錯的機器、或漏掉服務間驗證卻沒人發現）。
這裡用純 Python 設定環境變數再啟動 uvicorn，比 .bat/cmd.exe 包一層可靠，
不會被殼層（shell）對路徑/副檔名的處理方式影響。

⚠️ INTERNAL_API_TOKEN 這裡填的是本機測試用的佔位值，不是真正的密鑰。
   要接真正的後端、或多人共用測試，要跟後端組要實際的 token 值。
"""

import os

os.environ.setdefault("BACKEND_BASE_URL", "http://100.122.59.16:8000")
os.environ.setdefault("INTERNAL_API_TOKEN", "local-dev-placeholder-token")

import uvicorn

if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="100.101.167.105",
        port=5000,
        reload=True,
        reload_dirs=["modules/yolo/app"],
        app_dir="modules/yolo/app",
    )

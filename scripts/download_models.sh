#!/usr/bin/env bash
# ============================================================
# 下載模型權重
#
# 權重不進 git，改用 GitHub Release assets 發布。
# 這支腳本照 models/MODELS.txt 的清單去抓，並用 SHA256 驗證檔案完整。
#
# 用法：./scripts/download_models.sh
# ============================================================
set -euo pipefail
cd "$(dirname "$0")/.."

MANIFEST="models/MODELS.txt"
DEST="models"

[ -f "$MANIFEST" ] || { echo "[錯誤] 找不到 $MANIFEST"; exit 1; }
mkdir -p "$DEST"

sha_of() {
  if command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | cut -d' ' -f1
  else
    shasum -a 256 "$1" | cut -d' ' -f1   # macOS
  fi
}

downloaded=0
skipped=0

while IFS='|' read -r name url expected desc; do
  # 跳過註解和空行
  case "$name" in ''|\#*) continue ;; esac
  name="$(echo "$name" | xargs)"
  url="$(echo "$url" | xargs)"
  expected="$(echo "$expected" | xargs)"
  desc="$(echo "${desc:-}" | xargs)"

  target="$DEST/$name"

  # 已經有而且雜湊對得上 → 不重抓
  if [ -f "$target" ] && [ "$expected" != "請填入SHA256" ]; then
    if [ "$(sha_of "$target")" = "$expected" ]; then
      echo "⏭  $name 已存在且校驗通過，跳過"
      skipped=$((skipped + 1))
      continue
    fi
    echo "[警告] $name 已存在但雜湊不符，重新下載"
  fi

  echo "下載 $name  ($desc)"
  if ! curl -fL --progress-bar -o "$target.tmp" "$url"; then
    echo "   [錯誤] 下載失敗：$url"
    echo "      確認 Release 已發布，且如果 repo 是 private 需要先 gh auth login"
    rm -f "$target.tmp"
    exit 1
  fi

  if [ "$expected" = "請填入SHA256" ]; then
    actual="$(sha_of "$target.tmp")"
    echo "   [警告] 清單裡還沒填 SHA256。實際值是："
    echo "      $actual"
    echo "      請把它填回 $MANIFEST"
  else
    actual="$(sha_of "$target.tmp")"
    if [ "$actual" != "$expected" ]; then
      echo "   [錯誤] 校驗失敗！檔案可能損毀或被換過。"
      echo "      預期：$expected"
      echo "      實際：$actual"
      rm -f "$target.tmp"
      exit 1
    fi
    echo "   [OK] 校驗通過"
  fi

  mv "$target.tmp" "$target"
  downloaded=$((downloaded + 1))
done < "$MANIFEST"

echo ""
echo "完成：下載 $downloaded 個，跳過 $skipped 個"
echo "權重位置：$(pwd)/$DEST"

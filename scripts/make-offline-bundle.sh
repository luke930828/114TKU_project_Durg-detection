#!/usr/bin/env bash
# ============================================================
# 產生「離線包」—— 給展示場地沒網路 / GHCR 連不上時用
#
# 一般使用者用不到這個，他們下載的輕量包會自己去 GHCR 拉 image。
# 這份是給你們自己在 demo 前打包用的。
#
# 用法：./scripts/make-offline-bundle.sh v1.0.0
# ============================================================
set -euo pipefail

VERSION="${1:?請指定版本，例如 ./make-offline-bundle.sh v1.0.0}"
PREFIX="ghcr.io/luke930828/tku-drug-detection"
OUT="offline-bundle-${VERSION}"

mkdir -p "$OUT/images"

echo "拉取 image..."
IMAGES=(
  "mysql:8.0"
  "${PREFIX}/backend:${VERSION}"
  "${PREFIX}/frontend:${VERSION}"
  "${PREFIX}/crawler:${VERSION}"
  "${PREFIX}/nlp:${VERSION}"
  "${PREFIX}/yolo:${VERSION}"
)
for img in "${IMAGES[@]}"; do
  docker pull "$img"
done

echo "匯出成 tar（會很大，YOLO 那包 CUDA base 大概就 6-8GB）..."
docker save "${IMAGES[@]}" | gzip -1 > "$OUT/images/all-images.tar.gz"

cp dist/docker-compose.yml "$OUT/"
cp -r dist/initdb "$OUT/" 2>/dev/null || true
mkdir -p "$OUT/models"

# 離線包的載入腳本
cat > "$OUT/start-offline.sh" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
echo "載入本機 image（約 3-10 分鐘）..."
# 如果 image 被 split 成多份，先合併
if ls images/all-images.tar.gz.part-* >/dev/null 2>&1; then
  cat images/all-images.tar.gz.part-* > images/all-images.tar.gz
fi
gunzip -c images/all-images.tar.gz | docker load
[ -f .env ] || cp .env.example .env
docker compose up -d
echo "[OK] 完成 → http://localhost:8080"
EOF
chmod +x "$OUT/start-offline.sh"

SIZE=$(du -sh "$OUT" | cut -f1)
echo "打包中（目前 $SIZE）..."
tar czf "${OUT}.tar.gz" "$OUT"

# GitHub Release 單一檔案上限 2 GiB。
#    含 CUDA 的 YOLO image 一定會超過，所以要切開。
BYTES=$(stat -c%s "${OUT}.tar.gz" 2>/dev/null || stat -f%z "${OUT}.tar.gz")
if [ "$BYTES" -gt 2000000000 ]; then
  echo " 超過 GitHub Release 的 2GB 單檔上限，切成多份..."
  split -b 1900M "${OUT}.tar.gz" "${OUT}.tar.gz.part-"
  rm "${OUT}.tar.gz"
  echo "   使用者要先合併再解壓："
  echo "   cat ${OUT}.tar.gz.part-* | tar xzf -"
  ls -lh "${OUT}".tar.gz.part-*
else
  ls -lh "${OUT}.tar.gz"
fi

echo ""
echo "完成。提醒：離線包很大，如果只是要給人試用，"
echo "還是建議用輕量包（10KB，讓 Docker 自己去拉 image）。"

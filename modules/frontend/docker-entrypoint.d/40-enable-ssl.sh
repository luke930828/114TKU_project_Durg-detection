#!/bin/sh
# 憑證存在就啟用 HTTPS，不存在就維持純 HTTP。
#
# 為什麼要用腳本判斷而不是直接寫死 listen 443：
# nginx 找不到 ssl_certificate 指定的檔案會拒絕啟動。設定檔寫死 443 的話，
# 憑證還沒申請下來之前整個前端都起不來——而第一次申請憑證正好需要前端在
# port 80 上回應 ACME 的驗證請求。先有雞還是先有蛋。
#
#   沒有憑證 → 純 HTTP（可以跑、可以通過 ACME 驗證）
#   有憑證   → port 80 只留 ACME 與轉址，其餘全部走 443
#
# ⚠️ 轉址一定要放在 location 裡，不能放在 server 層。
#    server 層的 return 在 rewrite 階段就執行，會優先於所有 location——
#    包括 ACME 那個 location ^~。第一版就是這樣寫的，結果
#    /.well-known/acme-challenge/ 回 301，Let's Encrypt 永遠驗證不過。
#
# nginx 官方 image 會依序執行 /docker-entrypoint.d/*.sh，放這裡就好。
set -e

CERT_DIR="${SSL_CERT_DIR:-/etc/nginx/certs}"
CONF=/etc/nginx/conf.d/default.conf
# 原始設定檔的備份。放在 conf.d 外面——nginx 是 include conf.d/*.conf，
# 副檔名不是 .conf 雖然不會被載入，但擺在那個目錄裡遲早有人以為它有效。
PRISTINE=/etc/nginx/default.conf.pristine
FULLCHAIN="$CERT_DIR/fullchain.pem"
PRIVKEY="$CERT_DIR/privkey.pem"

# ⚠️ 這支腳本必須是冪等的。
#
# nginx 官方 image 的 entrypoint 每次「啟動」都會跑一遍 docker-entrypoint.d/，
# 不是只有「建立容器」時跑。容器重新建立時 default.conf 是 image 裡那份乾淨的，
# 但只是 restart（Docker Desktop 重開、WSL 重開、crash 後 restart: unless-stopped
# 自動拉起）的話，寫入層還在——腳本會對著「已經改過的檔案」再改一次。
#
# 第一版就是這樣：restart 之後多出第二個 443 server 區塊，nginx 直接
#     [emerg] "ssl_ciphers" directive is duplicate
# 拒絕啟動 → 又被 restart → 又疊一層，越修越壞，站台整個掛掉。
# 2026-09-03 實際發生，RestartCount 累到 11。
#
# 解法：第一次執行時把原始檔備份起來，之後每次都從那份重新產生，
# 不管跑幾次結果都一樣。
if [ ! -f "$PRISTINE" ]; then
    cp "$CONF" "$PRISTINE"
else
    cp "$PRISTINE" "$CONF"
fi

if [ ! -f "$FULLCHAIN" ] || [ ! -f "$PRIVKEY" ]; then
    echo "[ssl] 找不到 $FULLCHAIN，維持純 HTTP。"
    echo "[ssl] 申請憑證：bash scripts/issue-cert.sh <網域或IP>"
    exit 0
fi

echo "[ssl] 找到憑證，啟用 HTTPS。"

# 原本那個 server 區塊原封不動搬到 443，只換掉 listen 並補上 TLS 設定。
# 用複製而不是維護兩份設定檔：兩份最後一定會不同步，而安全標頭漏在其中
# 一份上是看不出來的。
sed \
    -e 's|^    listen 80;|    listen 443 ssl;\n    listen [::]:443 ssl;\n    http2 on;|' \
    -e '/#__HTTPS_REDIRECT__/d' \
    -e "s|^    server_name _;|    server_name _;\n\n    ssl_certificate     $FULLCHAIN;\n    ssl_certificate_key $PRIVKEY;\n    ssl_protocols       TLSv1.2 TLSv1.3;\n    ssl_ciphers         HIGH:!aNULL:!MD5;\n    ssl_prefer_server_ciphers off;\n    ssl_session_cache   shared:SSL:10m;\n    ssl_session_timeout 10m;\n\n    # HSTS 只加在 HTTPS 這一份。瀏覽器會忽略非加密連線送出的 HSTS，\n    # 加在 HTTP 那份沒有作用。|" \
    "$CONF" > /tmp/ssl-server.conf

# port 80 只留兩件事：ACME 驗證（不能轉址）與轉址。
# 因為是獨立的 server 區塊，location 比對正常運作，^~ 會贏過 location /。
cat > "$CONF" <<'HTTPCONF'
# ---- HTTP：只負責 ACME 驗證與轉址（由 40-enable-ssl.sh 產生）----
server {
    listen 80;
    listen [::]:80;
    server_name _;

    # 這個一定要能回 200。ACME 的 HTTP-01 驗證會來讀，
    # 轉址到 HTTPS 的話第一次簽發永遠不會成功（那時還沒有憑證）。
    location ^~ /.well-known/acme-challenge/ {
        root /var/www/certbot;
        default_type "text/plain";
        try_files $uri =404;
    }

    # 健康檢查不轉址，理由同 nginx.conf 裡那一份。
    location = /healthz {
        access_log off;
        add_header Content-Type text/plain always;
        return 200 "ok";
    }

    location / {
        return 301 https://$host$request_uri;
    }
}
HTTPCONF

cat /tmp/ssl-server.conf >> "$CONF"
rm -f /tmp/ssl-server.conf

# HSTS 要加在「每一個」有自己 add_header 的 location，不能只加在 server 層。
#
# nginx 的 add_header 不繼承：只要 location 裡有任何一個 add_header，
# 上層那一整組就全部失效。這個專案的 SEC-15 就是這樣——設定檔裡看得到
# 安全標頭，實際回應卻沒有，而且從設定檔完全看不出來。
#
# 跟著 X-Content-Type-Options 加——那是唯一每個 location（含 /assets/）
# 都有的標頭。第一版跟著 Referrer-Policy 加，結果 /assets/ 沒有，
# 因為那個 location 只有 Cache-Control 與 X-Content-Type-Options。
sed -i 's|^\(\s*\)add_header X-Content-Type-Options \(.*\)$|\1add_header X-Content-Type-Options \2\n\1add_header Strict-Transport-Security "max-age=31536000" always;|' "$CONF"

echo "[ssl] 完成：80 只留 ACME 與轉址，443 提供服務。"

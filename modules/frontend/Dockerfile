# ---------- build 階段 ----------
FROM node:22-alpine AS build

WORKDIR /app

COPY app/package.json app/package-lock.json* ./
RUN npm install

COPY app/ .

# Vite 的環境變數是「build 時」注入、烤進靜態檔的，不是執行時讀的。
ARG VITE_API_BASE_URL
ENV VITE_API_BASE_URL=$VITE_API_BASE_URL

RUN npm run build

# ---------- 執行階段 ----------
FROM nginx:alpine

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

# 憑證存在就啟用 HTTPS，不存在維持純 HTTP。寫死 listen 443 的話，
# 憑證還沒申請下來之前 nginx 會拒絕啟動——而申請憑證正好需要 port 80
# 能回應 ACME 驗證，先有雞還是先有蛋。
COPY docker-entrypoint.d/40-enable-ssl.sh /docker-entrypoint.d/40-enable-ssl.sh
RUN chmod +x /docker-entrypoint.d/40-enable-ssl.sh

EXPOSE 80 443

# 打 /healthz 而不是 /：啟用 HTTPS 之後 / 會回 301，wget --spider 跟著轉到
# 尚未被容器信任的憑證就會失敗，容器變 unhealthy 而服務其實正常。
# /healthz 刻意留在 HTTP 且不轉址。
#
# 用 127.0.0.1 而不是 localhost：容器的 /etc/hosts 讓 localhost 同時對應
# 127.0.0.1 與 ::1，wget 會先試 ::1，但 nginx.conf 只寫 listen 80（IPv4），
# 於是連線被拒、健康檢查永遠失敗——服務其實是好的，狀態卻一直是 unhealthy，
# 真的出問題時反而看不出來。
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=3 \
    CMD wget -q --spider http://127.0.0.1/healthz || exit 1

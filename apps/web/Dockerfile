# 构建上下文为仓库根目录：docker build -f apps/web/Dockerfile .
FROM node:20-alpine AS build

WORKDIR /app
COPY apps/web/package.json apps/web/package-lock.json ./
RUN npm config set registry https://registry.npmmirror.com && npm ci
COPY apps/web/ ./
# 前端走同源相对路径，留空即可由 nginx 反代到 api
ENV VITE_API_BASE_URL=""
RUN npm run build

FROM nginx:1.27-alpine
COPY deploy/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html
EXPOSE 80
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD wget -q -O /dev/null http://localhost/index.html || exit 1

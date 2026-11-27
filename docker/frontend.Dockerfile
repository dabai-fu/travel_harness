# ==============================================================================
# PyTravel-Harness React 19 前端多阶段构建镜像（Node 22 Build -> Nginx Serve）
# ==============================================================================
FROM node:22-alpine AS builder

ARG NPM_REGISTRY=https://registry.npmmirror.com

WORKDIR /web
COPY frontend/package.json ./
RUN npm install --legacy-peer-deps --registry=${NPM_REGISTRY}
COPY frontend/ ./
RUN npm run build

FROM nginx:1.27-alpine
COPY docker/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /web/dist /usr/share/nginx/html
EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]

# 依赖阶段
FROM node:20-alpine AS deps

WORKDIR /app

# 配置 npm 镜像源
RUN npm config set registry https://registry.npmmirror.com

# 复制 package.json
COPY frontend/package*.json ./
RUN npm install --legacy-peer-deps

# 质量检查阶段（在镜像构建期间执行）
#FROM deps AS qa
#
#COPY frontend/ .
#RUN npm run lint
#RUN npm run typecheck

# 构建阶段
FROM deps AS builder

COPY frontend/ ./
RUN npm run build

# 生产阶段
FROM nginx:alpine

# 复制构建产物
COPY --from=builder /app/dist /usr/share/nginx/html

# 复制 nginx 配置
COPY frontend/nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

CMD ["nginx", "-g", "daemon off;"]

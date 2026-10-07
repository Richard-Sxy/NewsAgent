# syntax=docker/dockerfile:1

# ---------- 构建阶段：只产出静态资源 ----------
FROM node:22.12-alpine AS build

WORKDIR /app

ENV CI=true \
    npm_config_audit=false \
    npm_config_fund=false

# 依赖层单独缓存：package.json 不变时不会重复安装。
COPY package.json package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY tsconfig.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src
COPY nginx ./nginx

# typecheck 与 build 在镜像内完成，CI 与本地使用同一套判定标准。
RUN npm run build


# ---------- 运行阶段：非 root nginx，仅含静态资源 ----------
FROM nginxinc/nginx-unprivileged:1.27-alpine AS runtime

# 该镜像默认以 uid 101 运行并监听 8080，不覆盖 USER。
COPY nginx/default.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 8080

CMD ["nginx", "-g", "daemon off;"]

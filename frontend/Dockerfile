# 前端两阶段构建：node 编译出静态文件，nginx 只负责发文件 + 反代 /api。
# 不把 node_modules 带进最终镜像，产物只有几十 KB 的 dist。
FROM node:24-alpine AS build

WORKDIR /build
# 单独 COPY package.json 再装：源码改动不会让依赖层缓存失效
COPY package.json ./
# 没有 lockfile 时不能写 npm ci（它要求 lockfile 存在），用 install 更稳
RUN npm install --no-fund --no-audit

COPY . .
RUN npm run build


FROM nginx:1.27-alpine

COPY --from=build /build/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

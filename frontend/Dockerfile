# syntax=docker/dockerfile:1.7
# Frontend ANPR Parking: сборка Vite в node, раздача nginx-unprivileged (uid 101, порт 8080, read-only root-fs).
ARG NODE_IMAGE=node:24-bookworm-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.29-alpine-slim

FROM ${NODE_IMAGE} AS build
WORKDIR /src
COPY package.json package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci --no-audit --no-fund
COPY tsconfig.json vite.config.ts index.html ./
COPY public ./public
COPY src ./src
ARG VITE_SENTRY_DSN=""
ARG APP_VERSION=0.0.0
ENV VITE_SENTRY_DSN=${VITE_SENTRY_DSN} VITE_APP_VERSION=${APP_VERSION}
# схема API (src/api/schema.d.ts) зафиксирована в репозитории и проверяется в CI (check:api) — gen:api в контейнере не нужен
RUN npm run build:image && find dist -name "*.map" -delete

FROM ${NGINX_IMAGE} AS runtime
ARG APP_VERSION=0.0.0
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="parking-frontend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}"
# фиксы apk (libcrypto/libssl: HIGH с доступными версиями) — образ nginx-unprivileged отстаёт от репозитория alpine
USER 0
RUN apk upgrade --no-cache
USER 101
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /src/dist /usr/share/nginx/html
# nginx-unprivileged уже запускается от uid 101 и слушает 8080; pid и кеши — в /tmp (emptyDir при read-only root-fs)
USER 101
EXPOSE 8080

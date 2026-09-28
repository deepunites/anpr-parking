# syntax=docker/dockerfile:1.7
# Backend ANPR Parking — многостадийная сборка (фаза 9 §1).
# builder: uv ставит зависимости из uv.lock в /app/.venv (без dev-группы: pgserver, mutmut, bandit и т. п. в образ не попадают).
# runtime: python:3.12-alpine без компиляторов и uv, непривилегированный пользователь, root-fs read-only
# (пишущие каталоги — только /tmp, монтируется emptyDir в Helm). Выбор базы по Trivy — docs/INFRA.md §1:
# slim-bookworm 5 CRITICAL (unfixed), slim-trixie 0/44 HIGH unfixed, distroless-debian13 0/21 HIGH unfixed (и Python 3.13),
# alpine3.22 после apk upgrade — 0 CRITICAL / 0 HIGH.
ARG PYTHON_IMAGE=python:3.12-alpine3.22
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.17

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS builder
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY --from=uv /uv /usr/local/bin/uv
# компиляторы — только в builder (на случай пакета без musllinux-колеса); в runtime их нет
RUN apk add --no-cache gcc musl-dev libffi-dev
# сначала только манифесты: слой зависимостей кешируется независимо от кода
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable
COPY src ./src
COPY alembic ./alembic
COPY alembic.ini ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable \
    && find /app/.venv -name "*.pyc" -path "*__pycache__*" -prune -o -type d -name tests -exec rm -rf {} + 2>/dev/null || true

FROM ${PYTHON_IMAGE} AS runtime
ARG APP_VERSION=0.0.0
ARG GIT_SHA=unknown
LABEL org.opencontainers.image.title="parking-backend" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.revision="${GIT_SHA}" \
      org.opencontainers.image.source="anpr-parking/backend" \
      org.opencontainers.image.licenses="Proprietary"
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONFAULTHANDLER=1 \
    PATH="/app/.venv/bin:${PATH}" \
    PARKING_ENV=prod \
    APP_VERSION="${APP_VERSION}" \
    GIT_SHA="${GIT_SHA}"
# обновление базовых пакетов (Trivy: HIGH с доступными фиксами в apk), curl не ставим — health-пробы делает kubelet по HTTP
RUN apk upgrade --no-cache \
    && addgroup -S -g 10001 app \
    && adduser -S -u 10001 -G app -h /app -s /sbin/nologin app
# pip/ensurepip базового образа в runtime не нужны (единственные CVE образа по Trivy были в pip)
RUN rm -rf /usr/local/lib/python3.12/site-packages/pip* /usr/local/lib/python3.12/ensurepip /usr/local/bin/pip* \
    && find /usr/local/lib/python3.12 -depth -type d -name __pycache__ -exec rm -rf {} +
WORKDIR /app
COPY --from=builder --chown=root:root /app/.venv /app/.venv
COPY --from=builder --chown=root:root /app/alembic /app/alembic
COPY --from=builder --chown=root:root /app/alembic.ini /app/alembic.ini
USER 10001:10001
EXPOSE 8000
# по умолчанию — API; воркеры переопределяют command/args в Helm (см. deploy/helm/parking/values.yaml processes.*)
CMD ["uvicorn", "parking.api.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-keep-alive", "75", "--no-server-header"]

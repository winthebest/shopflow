# syntax=docker/dockerfile:1
# Build context: repository root (uv workspace).
ARG PYTHON_IMAGE=python:3.12-slim-trixie@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv

FROM ${PYTHON_IMAGE} AS build
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /src
# Third-party dependencies first: this layer is reused until uv.lock or a pyproject changes.
COPY pyproject.toml uv.lock ./
COPY services/common/pyproject.toml services/common/
COPY services/fulfillment-worker/pyproject.toml services/fulfillment-worker/
COPY services/gateway/pyproject.toml services/gateway/
COPY services/orders/pyproject.toml services/orders/
COPY services/payments/pyproject.toml services/payments/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-workspace --package shopflow-fulfillment-worker
COPY services/common services/common
COPY services/fulfillment-worker services/fulfillment-worker
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable --package shopflow-fulfillment-worker

FROM ${PYTHON_IMAGE}
LABEL org.opencontainers.image.source="https://github.com/winthebest/shopflow" \
      org.opencontainers.image.title="shopflow-fulfillment-worker" \
      org.opencontainers.image.description="shopflow fulfillment-worker: one shipment per paid order (CDC consumer)"
RUN groupadd --system --gid 10001 app \
    && useradd --system --uid 10001 --gid 10001 --no-create-home --shell /usr/sbin/nologin app
COPY --from=build /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    OTEL_SDK_DISABLED=true \
    OTEL_SEMCONV_STABILITY_OPT_IN=http
WORKDIR /app
USER 10001:10001
EXPOSE 8003
CMD ["fulfillment-worker"]

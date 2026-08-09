# syntax=docker/dockerfile:1.7
FROM python:3.11-slim-bookworm AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

COPY requirements.txt ./
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip setuptools wheel \
    && /opt/venv/bin/pip install --no-compile -r requirements.txt

FROM python:3.11-slim-bookworm AS runtime

ARG APP_VERSION=development
LABEL org.opencontainers.image.title="SmartReco" \
      org.opencontainers.image.description="Behavioral next-best-action course recommendation platform" \
      org.opencontainers.image.version="${APP_VERSION}"

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    PORT=8000 \
    WEB_CONCURRENCY=1 \
    FORWARDED_ALLOW_IPS=127.0.0.1 \
    SESSION_COOKIE_SECURE=true \
    SCHEDULER_ENABLED=false \
    MESH_CALLS_ENABLED=false \
    PINECONE_NAMESPACE=production

WORKDIR /app
RUN groupadd --system smartreco \
    && useradd --system --gid smartreco --home-dir /app --shell /usr/sbin/nologin smartreco

COPY --from=builder /opt/venv /opt/venv
COPY --chown=smartreco:smartreco src ./src
COPY --chown=smartreco:smartreco frontend ./frontend
COPY --chown=smartreco:smartreco assets ./assets
COPY --chown=smartreco:smartreco scripts ./scripts
COPY --chown=smartreco:smartreco migrations ./migrations

USER smartreco
EXPOSE 8000
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3)"

CMD ["sh", "-c", "exec uvicorn src.main:app --host 0.0.0.0 --port ${PORT} --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips ${FORWARDED_ALLOW_IPS} --no-server-header"]

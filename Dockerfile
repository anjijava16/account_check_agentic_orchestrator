# syntax=docker/dockerfile:1.7
# Multi-stage build. The runtime image carries no compiler and runs unprivileged.

FROM python:3.12-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential curl git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build
COPY pyproject.toml README.md ./
COPY app ./app

RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
# setuptools >=81 drops pkg_resources, which litellm still imports at runtime.
RUN pip install --upgrade pip "setuptools<81" wheel && pip install .

# ---------------------------------------------------------------- runtime
FROM python:3.12-slim-bookworm AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd -r banking && useradd -r -g banking -u 10001 -m -d /home/banking banking

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
COPY --chown=banking:banking app ./app
COPY --chown=banking:banking alembic ./alembic
COPY --chown=banking:banking alembic.ini ./
COPY --chown=banking:banking scripts ./scripts

USER banking
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://localhost:8000/live || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["gunicorn", "app.main:app", \
     "--worker-class", "uvicorn.workers.UvicornWorker", \
     "--workers", "4", \
     "--bind", "0.0.0.0:8000", \
     "--timeout", "120", \
     "--graceful-timeout", "30", \
     "--keep-alive", "5", \
     "--access-logfile", "-", \
     "--error-logfile", "-"]

# Production image (#2). Multi-stage: wheels are built with build tooling, the
# runtime layer carries only what is needed to serve the API.
FROM python:3.11-slim AS build

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
WORKDIR /src
RUN apt-get update && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml README.md ./
COPY app ./app
# setuptools declares `scripts` as a package (scripts/seed.py), so it must be
# present for `pip install .` to build the wheel — see docs/DEPLOYING.md.
COPY scripts ./scripts
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

FROM python:3.11-slim AS runtime

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    APP_ENV=production \
    HOST=0.0.0.0 \
    PORT=8000 \
    WORKERS=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends postgresql-client curl \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 agentcms
COPY --from=build /opt/venv /opt/venv
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY app ./app
COPY scripts ./scripts
COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh && chown -R agentcms:agentcms /app
USER agentcms
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=15s --retries=5 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1
ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--workers", "1"]

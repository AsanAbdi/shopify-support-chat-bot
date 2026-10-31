# syntax=docker/dockerfile:1.7
ARG PYTHON_VERSION=3.12

# ---------- ML models (own stage: cached across code and dependency changes, ~2.3 GB download once) ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS models
ARG FASTEMBED_VERSION=0.8.1
ARG EMBEDDINGS_MODEL=intfloat/multilingual-e5-large
ARG RERANKER_MODEL=jinaai/jina-reranker-v2-base-multilingual
ARG BAKE_RERANKER=0
# the xet transfer protocol breaks on flaky links; plain HTTP resumes
ENV HF_HUB_DISABLE_XET=1
# Models are baked into the image: no downloads at runtime and no external embedding API sees customer data.
# Retries: a 2 GB download over a flaky connection should resume (partial files stay in /models), not restart the build.
RUN pip install --no-cache-dir "fastembed==${FASTEMBED_VERSION}" \
    && for i in 1 2 3 4 5 6 7 8 9 10; do \
         python -c "from fastembed import TextEmbedding; TextEmbedding('${EMBEDDINGS_MODEL}', cache_dir='/models')" && exit 0; \
         echo "model download failed (attempt $i of 10), retrying in 20 s"; sleep 20; \
       done; exit 1
# Optional (~1.1 GB, slow on CPU: 1.5-3.5 s per query): docker compose build --build-arg BAKE_RERANKER=1, then RERANKER=fastembed
RUN if [ "$BAKE_RERANKER" = "1" ]; then \
      python -c "from fastembed.rerank.cross_encoder import TextCrossEncoder; TextCrossEncoder('${RERANKER_MODEL}', cache_dir='/models')"; \
    fi

# ---------- dependencies (cached layers) ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.8 /uv /usr/local/bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-dev --no-install-project

FROM builder AS builder-dev
RUN --mount=type=cache,target=/root/.cache/uv uv sync --frozen --no-install-project

# ---------- runtime base ----------
FROM python:${PYTHON_VERSION}-slim-bookworm AS runtime-base
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH" \
    FASTEMBED_CACHE_PATH=/opt/models \
    PYTHONPATH=/app
RUN groupadd -r -g 10001 app && useradd -r -u 10001 -g app -d /app -s /usr/sbin/nologin app \
    && mkdir -p /app /data/uploads && chown -R app:app /app /data
COPY --from=builder /opt/venv /opt/venv
COPY --from=models --chown=app:app /models /opt/models
WORKDIR /app
COPY --chown=app:app . /app
USER app
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=40s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"

# ---------- dev / test image ----------
FROM runtime-base AS dev
USER root
COPY --from=builder-dev /opt/venv /opt/venv
USER app

# ---------- production image (default: last stage) ----------
FROM runtime-base AS prod
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips=*"]

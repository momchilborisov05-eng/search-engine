# syntax=docker/dockerfile:1.7

# ---------- build stage: install dependencies into a virtualenv ----------
FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
COPY requirements.txt .
RUN pip install -r requirements.txt

# ---------- runtime stage: slim image, non-root user, only what runs ----------
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="sift-search" \
      org.opencontainers.image.description="Search engine from scratch: crawler, inverted index, BM25, autocomplete, analytics"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    SIFT_DB=/data/sift.db

RUN groupadd --system app && useradd --system --gid app --home-dir /app --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data

WORKDIR /app
COPY --from=build /opt/venv /opt/venv
COPY --chown=app:app sift ./sift

USER app
EXPOSE 8000
VOLUME ["/data"]

HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# One worker process: the inverted index, LRU cache and trie live in memory, so a single process keeps them
# consistent; threads give concurrency (searches are short and mostly release the GIL in SQLite).
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--access-logfile", "-", \
     "sift.web.app:create_app()"]

# syntax=docker/dockerfile:1

# Two stages: the builder resolves the locked dependencies into a virtualenv,
# the runtime stage ships that virtualenv plus the `aoeo_market` package.  The
# dependencies are installed from `uv.lock` only, so the image contains exactly
# what `uv sync` produces locally and `--frozen` makes a stale lockfile a build
# failure instead of a silent re-resolve.

# --- builder ------------------------------------------------------------------
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.15 /uv /uvx /bin/

# Byte-compile on install (faster cold start), copy instead of hardlink (the
# cache and the venv live on different filesystems), and never download a
# Python: the base image's interpreter is the one the lockfile is resolved for.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Only the files `uv sync` reads are visible here, so editing the source below
# never invalidates the cached dependency layer.  Dev dependencies (pytest,
# ruff, the OpenAPI validators) stay out of the image.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-dev --no-install-project

# --- runtime ------------------------------------------------------------------
FROM python:3.14-slim AS runtime

# The project is a plain module tree (`[tool.uv] package = false`), not an
# installed distribution: it runs from the workdir via `python -m aoeo_market.web`
# and `python -m aoeo_market.cli`.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

# Unprivileged owner of the process and of the database volume.  /data holds
# `market.db`; the web process is its single writer, so the directory must be
# writable by this user.
RUN groupadd --system --gid 10001 aoeo \
 && useradd --system --uid 10001 --gid aoeo --create-home --home-dir /home/aoeo aoeo \
 && install -d --owner aoeo --group aoeo /data

WORKDIR /app

COPY --from=builder --chown=aoeo:aoeo /app/.venv /app/.venv
COPY --chown=aoeo:aoeo aoeo_market ./aoeo_market
COPY --chown=aoeo:aoeo LICENSE README.md THIRD_PARTY_NOTICES.md ./

USER 10001:10001

VOLUME ["/data"]
# 8000 is the public dashboard/read API; 8001 is the snapshot write API, which
# only the fetcher needs to reach (publish 8000 only).
EXPOSE 8000 8001

# Liveness only (`/healthz` answers 200 while the process is up); readiness is
# `/readyz`, which stays 503 until the database exists and opens.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4)"]

# Serve the dashboard (read API on 8000, snapshot write API on 8001).
CMD ["python", "-m", "aoeo_market.web", "--db", "/data/market.db", "--host", "0.0.0.0", "--port", "8000", "--write-port", "8001"]

# syntax=docker/dockerfile:1

# ---- builder: resolve dependencies into a venv with uv --------------------
FROM python:3.12-slim AS builder

# Pin the uv major.minor that produced uv.lock (see `uv --version` locally).
COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

# `cryptography` / `cffi` ship prebuilt wheels for x86-64 and arm64 (incl.
# Raspberry Pi OS 64-bit), so no compiler is needed. On 32-bit ARM only,
# uncomment the next line to build them from source:
# RUN apt-get update && apt-get install -y --no-install-recommends gcc libffi-dev && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first (cached layer) using only the lockfiles.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

# Then install the project itself.
COPY app ./app
COPY README.md LICENSE ./
RUN uv sync --frozen --no-dev


# ---- runtime: minimal image, non-root user ------------------------------
FROM python:3.12-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --home /app app

WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --chown=app:app app ./app

# Writable dirs: /app/data (SQLite file / local state), /app/config
# (auto-generated secret.key when CONFIG_ENCRYPTION_KEY is not set). Both are
# mounted as named volumes by docker-compose; creating them here with the right
# owner means the volumes inherit that ownership on first use.
RUN mkdir -p /app/data /app/config && chown app:app /app/data /app/config
USER app

# The bot is an outbound Discord gateway client; this port only serves
# GET /health, for platforms (e.g. Cloudflare Containers) that need something
# to probe to know the process is alive.
EXPOSE 8080
CMD ["python", "-m", "app.main"]

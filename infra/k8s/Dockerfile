# syntax=docker/dockerfile:1.6
#
# deile-stack — single image carrying both the deile agent and the
# deilebot daemon. Two pods derive from this image, each with a
# different command and a different (minimal) Secret.
#
# Security posture:
#   - Multi-stage: build deps live only in the builder; final image is
#     python:3.11-slim with no compilers, no curl, no apt cache.
#   - Non-root user (uid 10001).
#   - No secrets, no .env, no SQLite database, no logs in the image.
#     The .dockerignore in this directory enforces that.
#   - The image carries source for both packages but installs them
#     via `pip install -e` so the file tree under /app stays inspectable
#     for audit. Combined with readOnlyRootFilesystem at the Pod level,
#     nothing in /app can be modified at runtime.
# ----------------------------------------------------------------------

ARG PYTHON_VERSION=3.11.10

# ---- builder ---------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        build-essential \
        git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the deilebot subtree first (so deile can resolve it as a
# *local* dep when [bot] is requested). Order matters for the layer
# cache: deilebot rarely changes versus deile during dev.
COPY deilebot/pyproject.toml deilebot/README.md ./deilebot/
COPY deilebot/deilebot ./deilebot/deilebot
COPY deilebot/deilebot_client ./deilebot/deilebot_client

# Then deile.
COPY pyproject.toml requirements.txt README.md ./
COPY deile ./deile

# Install both packages into a venv we'll copy into the final stage.
# - deilebot[discord,client] gives us the Discord adapter + the thin HTTP
#   client that deile imports (deilebot_client).
# - deile is installed *without* the [bot] extra to skip its git URL —
#   deilebot is already on disk from the previous step.
RUN python -m venv /venv \
    && /venv/bin/pip install --upgrade pip wheel \
    && /venv/bin/pip install -r requirements.txt \
    && /venv/bin/pip install ./deilebot[discord,client] \
    && /venv/bin/pip install . \
    && find /venv -type d -name __pycache__ -exec rm -rf {} +

# Bug: neither pyproject declares `package-data`, so non-Python files
# (.sql migrations, .md persona prompts, .yaml configs) get dropped on
# wheel build. Without these the bot cannot init its sqlite schema and
# deile cannot load any persona. Copy them in by hand.
# SITE_PACKAGES is derived from the venv's own Python so it stays
# correct when PYTHON_VERSION is overridden at build time.
RUN set -eux \
    && SITE_PACKAGES=$(/venv/bin/python -c "import sysconfig; print(sysconfig.get_path('purelib'))") \
    && mkdir -p "${SITE_PACKAGES}/deilebot/foundation/sql" \
    && cp /build/deilebot/deilebot/foundation/sql/*.sql "${SITE_PACKAGES}/deilebot/foundation/sql/" \
    && mkdir -p "${SITE_PACKAGES}/deile/personas/instructions/core" \
    && cp /build/deile/personas/instructions/*.md         "${SITE_PACKAGES}/deile/personas/instructions/" \
    && cp /build/deile/personas/instructions/core/*.md    "${SITE_PACKAGES}/deile/personas/instructions/core/" \
    && mkdir -p "${SITE_PACKAGES}/deile/personas/library" \
    && cp /build/deile/personas/library/*.yaml            "${SITE_PACKAGES}/deile/personas/library/" \
    && mkdir -p "${SITE_PACKAGES}/deile/config/profiles" \
    && cp /build/deile/config/*.yaml                      "${SITE_PACKAGES}/deile/config/" \
    && cp /build/deile/config/profiles/*.yaml             "${SITE_PACKAGES}/deile/config/profiles/" \
    && ls "${SITE_PACKAGES}/deilebot/foundation/sql/" "${SITE_PACKAGES}/deile/personas/instructions/" \
    && echo "package-data injection complete"

# ---- final -----------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS final

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PATH="/venv/bin:${PATH}" \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    HOME=/home/deile

# Drop apt caches; we install nothing in the final stage.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tini \
    && rm -rf /var/lib/apt/lists/*

# Non-root user. UID 10001 is well above the system uid range
# and matches the runAsUser in the Pod manifests.
RUN groupadd --system --gid 10001 deile \
    && useradd  --system --uid 10001 --gid 10001 \
        --home-dir /home/deile --shell /usr/sbin/nologin deile \
    && mkdir -p /home/deile /app \
    && chown -R deile:deile /home/deile /app

# Bring in the venv from the builder.
COPY --from=builder --chown=deile:deile /venv /venv

# Bring source for inspection/import (read-only at runtime per Pod spec).
COPY --from=builder --chown=deile:deile /build/deile /app/deile
COPY --from=builder --chown=deile:deile /build/deilebot /app/deilebot

# Wrapper script — strips secrets before deile runs.
COPY --chown=deile:deile infra/k8s/wrapper.py /app/wrapper.py
RUN chmod 0555 /app/wrapper.py

WORKDIR /app
USER deile:deile

# tini reaps zombies (matters because deile may shell-out via bash_tool).
ENTRYPOINT ["/usr/bin/tini", "--"]

# Healthcheck for the bot pod is wired at the Pod level (httpGet /v1/health).
# No CMD here — each Pod sets its own command.

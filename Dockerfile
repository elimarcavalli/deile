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

# Test toolchain — baked in so the deile-worker can run a cloned repo's
# pytest suite at runtime WITHOUT `pip install` (the Pod rootfs is
# read-only). Versions pinned to match the repo's dev-requirements.txt so
# the worker's runs mirror CI. Kept to the pytest runner stack (no
# black/isort/radon/safety/bandit) to limit image growth.
RUN /venv/bin/pip install \
        pytest==8.4.2 \
        pytest-asyncio==1.2.0 \
        pytest-mock==3.15.1 \
        pytest-cov==6.3.0 \
        pytest-xdist==3.8.0 \
        pytest-timeout==2.4.0 \
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

# Install tini (zombie reaper), git, iputils-ping (diagnostics), curl
# (REST calls + gh repo key) and the GitHub CLI `gh`. The worker opens
# issues/PRs and clones via gh, so it must be present (the previous
# "lean, use git directly" stance broke `gh issue create`). gh ships in
# GitHub's own apt repo, added here with a signed keyring.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        tini git iputils-ping curl ca-certificates gnupg jq \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
        -o /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
        > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# GitLab CLI `glab` (issue #297). Installed in a separate layer so updating
# the version does NOT invalidate the gh layer above. Version PINNED — bumps
# are deliberate PRs, never silent. The .deb is fetched from the official
# GitLab releases (no third-party PPA). Layer growth ~20 MB. Runtime
# behaviour for GitHub-only operators is unchanged: glab is dormant unless
# DEILE_FORGE_KIND=gitlab or a GitLab URL is processed.
#
# SHA256 verificados em 2026-05-25 baixando do release oficial v1.45.0:
#   https://gitlab.com/gitlab-org/cli/-/releases/v1.45.0/downloads/checksums.txt
# Fonte canônica: arquivo checksums.txt publicado junto ao release.
ARG GLAB_VERSION=1.45.0
ARG GLAB_SHA256_AMD64=3efe5be6d5fd6c3346d2cabd2ca35d7f85a5ae5d97da8c90dff81557124dc519
ARG GLAB_SHA256_ARM64=2bd45d6d0f7c6af15604720dc8d177a3a15661230bcee45334879c5928de57bc
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
        amd64) GLAB_ARCH=x86_64; EXPECTED_SHA="${GLAB_SHA256_AMD64}" ;; \
        arm64) GLAB_ARCH=arm64;  EXPECTED_SHA="${GLAB_SHA256_ARM64}" ;; \
        *) echo "unsupported arch for glab: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /tmp/glab.deb \
        "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_Linux_${GLAB_ARCH}.deb" \
    && echo "${EXPECTED_SHA}  /tmp/glab.deb" | sha256sum -c - \
    && dpkg -i /tmp/glab.deb \
    && rm /tmp/glab.deb \
    && gh --version \
    && glab --version

# kubectl (Decisão #46). O pipeline pod usa kubectl para refrescar OAuth do
# claude-worker in-pod (chama ``kubectl exec`` em ``_claude_creds_refresh``).
# Antes este pod era construído sem kubectl, então o refresh automático
# falhava silenciosamente com FileNotFoundError e o pipeline tinha que ser
# resgatado pelo humano via ``deploy.py k8s claude-renew``.
#
# Versão alinhada com k3s/Rancher Desktop server-side (~v1.31). Layer
# separada para não invalidar gh/glab quando bumpamos kubectl.
ARG KUBECTL_VERSION=v1.31.4
RUN ARCH="$(dpkg --print-architecture)" \
    && case "$ARCH" in \
        amd64) KUBECTL_ARCH=amd64 ;; \
        arm64) KUBECTL_ARCH=arm64 ;; \
        *) echo "unsupported arch for kubectl: $ARCH" >&2; exit 1 ;; \
       esac \
    && curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${KUBECTL_ARCH}/kubectl" \
    && chmod 0555 /usr/local/bin/kubectl \
    && kubectl version --client --output=yaml | head -5

# -----------------------------------------------------------------------------
# claude CLI (issue #309 fase 2)
#
# Bake do claude CLI no image. Permite que o pod claude-worker rode
# `claude -p` sem instalar em runtime (que exigiria egress npm e seria
# bloqueado pela NetworkPolicy default-deny).
#
# Camada separada das anteriores (gh/glab) para layer cache em rebuilds que
# não tocam claude.
#
# nodejs ~20 (LTS) via NodeSource (debian-based). Tamanho: ~80MB nodejs +
# ~30MB claude CLI = ~110MB nesta camada.
#
# Versão pinada (não "latest"): reprodutibilidade + garante o keyword trigger
# de "workflow"/"workflows" que o preset ultracode usa para opt-in no Workflow
# tool (presente a partir de 2.1.158). Sem pin, o layer cache do Docker servia
# 2.1.156 indefinidamente. Bump via --build-arg CLAUDE_CODE_VERSION=<x>.
# -----------------------------------------------------------------------------
ARG CLAUDE_CODE_VERSION=2.1.158
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
 && apt-get update \
 && apt-get install -y --no-install-recommends nodejs \
 && rm -rf /var/lib/apt/lists/* \
 && npm install -g --omit=dev "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && claude --version

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

# claude-worker HTTP server — entry point for the claude-worker pod (issue #309).
COPY --chown=deile:deile infra/k8s/claude_worker_server.py /app/claude_worker_server.py
RUN chmod 0555 /app/claude_worker_server.py

# Worker HTTP server — entry point for the deile-worker pod.
COPY --chown=deile:deile infra/k8s/worker_server.py /app/worker_server.py
RUN chmod 0555 /app/worker_server.py

# Pure-logic helper imported by worker_server.py (``import _worker_resume``):
# resume fingerprint / journal / end-detection (issue #254). It MUST sit in
# /app next to worker_server.py or the worker crashes on import at startup.
COPY --chown=deile:deile infra/k8s/_worker_resume.py /app/_worker_resume.py
RUN chmod 0555 /app/_worker_resume.py

# Pipeline status server — HTTP introspection endpoints (issue #347 fix). Roda
# in-process no wrapper.py mode pipeline (asyncio task no mesmo event loop do
# monitor). Sem este arquivo no /app o `from pipeline_status_server import ...`
# do wrapper falha na boot do pipeline pod.
COPY --chown=deile:deile infra/k8s/pipeline_status_server.py /app/pipeline_status_server.py
RUN chmod 0555 /app/pipeline_status_server.py

WORKDIR /app
USER deile:deile

# tini reaps zombies (matters because deile may shell-out via bash_tool).
ENTRYPOINT ["/usr/bin/tini", "--"]

# Healthcheck for the bot pod is wired at the Pod level (httpGet /v1/health).
# No CMD here — each Pod sets its own command.

# syntax=docker/dockerfile:1.7
# ── Stage 1: builder — has compilers, builds Python wheels ──────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# apt cache mount: keeps .debs across builds so apt-get is near-instant on rebuild.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev

WORKDIR /build

COPY requirements-linux.txt .

# pip cache mount: persists pip's wheel cache between builds, so unchanged packages
# (which is most of them, most of the time) are reused instead of re-downloaded.
# The builder stage's site-packages is what we COPY to runtime — the cache stays here.
RUN --mount=type=cache,target=/root/.cache/pip,sharing=locked \
    pip install --upgrade pip \
    && pip install --retries 10 --timeout 120 --resume-retries 10 -r requirements-linux.txt


# ── Stage 2: runtime — no compilers, only runtime shared libs ───────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DJANGO_SETTINGS_MODULE=workflow_backend.settings.deployment

# nodejs/npm are required at runtime: stdio MCP servers are launched as
# `npx -y @modelcontextprotocol/...` subprocesses by mcp_integration.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libmagic1 curl nodejs npm

# Copy installed Python packages and console scripts from the builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Pre-warm the curated stdio connectors into the npx cache.
#
# Without this, `npx -y <pkg>` reaches the npm registry the first time each
# connector is used, and resolve+install is far slower than any budget a
# request can hold: measured 25 s+ cold against 2.4–3.7 s once npx has the
# package cached. The first click on every connector would time out, and the
# negative cache would then replay that timeout for a minute.
#
# Running each server once here is what populates `$NPM_CONFIG_CACHE/_npx`.
# A global `npm install` would *not* do it: `npx -y <spec>` resolves by package
# spec, not by binary name, so it would still consult the registry. The DB rows
# keep saying `npx -y <pkg>` so local development — which has no image and no
# pre-warm — works exactly the same way, just slower on first use.
#
# `</dev/null` gives each server EOF on stdin so it exits instead of waiting for
# an MCP handshake; `timeout` bounds the ones that ignore it, and `|| true`
# keeps a registry hiccup at build time from failing the whole image.
# Keep this list in step with the curated catalogue — currently
# `mcp_integration/migrations/0011_working_connector_catalogue.py` as amended
# by `0012_enable_gmail_connector.py`. An enabled connector missing from this
# list is one that times out on its first use in production: cold `npx -y` is
# ~21 s and `client.CONNECT_TIMEOUT` is 25 s, so it is a coin flip, not a
# margin.
ENV NPM_CONFIG_CACHE=/opt/npm-cache
RUN mkdir -p /opt/npm-cache \
    && for pkg in \
        @modelcontextprotocol/server-filesystem \
        @modelcontextprotocol/server-memory \
        @modelcontextprotocol/server-sequential-thinking \
        @modelcontextprotocol/server-slack \
        @notionhq/notion-mcp-server \
        @tokenizin/mcp-npx-fetch \
        @shinzolabs/gmail-mcp \
        @isaacphi/mcp-gdrive \
        @cocal/google-calendar-mcp \
    ; do \
        echo "pre-warming $pkg" \
        && timeout 300 npx -y "$pkg" </dev/null >/dev/null 2>&1 || true; \
    done \
    && chmod -R a+rX /opt/npm-cache

WORKDIR /app

# Application source. `.dockerignore` excludes static/, media/, data/, db, etc.
COPY . .

RUN mkdir -p /app/data /app/media /app/staticfiles

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD curl --fail http://localhost:8000/api/health/ || exit 1

# collectstatic at boot: regenerates the small Django admin + DRF CSS/JS.
# If AWS_STORAGE_BUCKET_NAME is set, settings.py routes it to S3.
# Otherwise it lands in /app/staticfiles and is served locally.
# Serve with daphne, not runserver: runserver enables StatReloader (a file
# watcher that can restart the process mid-stream, aborting SSE/WS) and is a
# single-threaded dev server. DJANGO_SETTINGS_MODULE comes from the ENV above
# (asgi.py only setdefaults it), so this boots deployment settings.
CMD ["sh", "-c", "python manage.py collectstatic --noinput --clear && python manage.py migrate --noinput && daphne -b 0.0.0.0 -p 8000 workflow_backend.asgi:application"]

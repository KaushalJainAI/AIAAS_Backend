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

# Install the curated stdio connectors, and launch them directly.
#
# `npx -y <pkg>` is two Node processes: the launcher, which then sits there
# holding a pipe for the life of the session, and the server itself. On a
# 384 MB container with a 150 MB connector budget that was roughly half the
# ceiling spent on launchers, which is why `mcp_integration/launch.py` rewrites
# an `npx` row to `node <pkg>/<bin>` whenever the package is here.
#
# This replaces the old `npx -y <pkg>` pre-warm loop (2026-09-17). That loop
# existed so a cold `npx` (~21 s, against a 25 s CONNECT_TIMEOUT) would not be
# paid on first use; installing the packages removes the resolve step
# altogether, and the 1.1 GB npx cache it left in the image was redundant for
# every enabled row — all nine resolve to a direct `node` launch, which
# `tests/test_launch.py` covers and the deploy smoke-test re-checks in the
# built image.
#
# Keep this list in step with the curated catalogue — currently
# `mcp_integration/migrations/0011_working_connector_catalogue.py` as amended
# by `0012_enable_gmail_connector.py` and `0014_enable_google_file_connectors.py`
# (Drive and Sheets share `@isaacphi/mcp-gdrive`, so one entry covers both).
# A connector missing from here still works: it falls back to `npx -y`, now
# without a warm cache, so it is slow on first use rather than broken.
#
# `|| true` keeps a registry hiccup at build time from failing the whole image;
# the smoke-test is what catches an install that silently produced nothing.
ENV NPM_CONFIG_CACHE=/opt/npm-cache
ENV MCP_PACKAGE_ROOT=/opt/mcp/node_modules
RUN mkdir -p /opt/mcp     && cd /opt/mcp     && npm init -y >/dev/null 2>&1     && npm install --omit=dev --no-audit --no-fund         @modelcontextprotocol/server-filesystem         @modelcontextprotocol/server-memory         @modelcontextprotocol/server-sequential-thinking         @modelcontextprotocol/server-slack         @notionhq/notion-mcp-server         @tokenizin/mcp-npx-fetch         @shinzolabs/gmail-mcp         @isaacphi/mcp-gdrive         @cocal/google-calendar-mcp         >/dev/null 2>&1 || true     && chmod -R a+rX /opt/mcp     && npm cache clean --force >/dev/null 2>&1 || true     && rm -rf /opt/npm-cache/_cacache

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
# migrate at boot, then re-sync the model catalogue (`populate_models` only
# upserts: existing rows get the seed's prices and effort rungs, hand-added
# rows survive). Without this the catalogue freezes at whatever the database
# carried when it was created — which is how production served an empty
# `effort_levels` on every reasoning model months after the effort feature
# shipped, and every picker explained that the model "has no setting".
# Serve with daphne, not runserver: runserver enables StatReloader (a file
# watcher that can restart the process mid-stream, aborting SSE/WS) and is a
# single-threaded dev server. DJANGO_SETTINGS_MODULE comes from the ENV above
# (asgi.py only setdefaults it), so this boots deployment settings.
CMD ["sh", "-c", "python manage.py collectstatic --noinput --clear && python manage.py migrate --noinput && python manage.py shell -c 'import populate_models; populate_models.populate()' && daphne -b 0.0.0.0 -p 8000 workflow_backend.asgi:application"]

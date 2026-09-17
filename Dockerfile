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

# No Node in this image (2026-09-17). Every curated stdio connector is gone:
# Gmail, Drive, Sheets and Calendar are native tools calling Google's REST APIs
# from this process (`chat/tools/google/`), the four utility connectors were
# retired as duplicates of built-ins, and `MCP_ALLOW_STDIO=False` refuses any
# user-added local-process server. A Node process per connector (70-150 MB) on
# a 384 MB container is what killed daphne on 2026-09-16.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends libpq5 libmagic1 curl

# Copy installed Python packages and console scripts from the builder.
COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

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

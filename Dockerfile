# syntax=docker/dockerfile:1.7
# ── Stage 1: builder — has compilers, builds Python wheels ──────────────
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libpq-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY requirements-linux.txt .

# CPU-only torch from the dedicated index, then the rest from PyPI.
# --no-cache-dir keeps the layer lean; site-packages will be copied to runtime.
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir --index-url https://download.pytorch.org/whl/cpu torch==2.11.0 \
    && pip install --no-cache-dir -r requirements-linux.txt


# ── Stage 2: runtime — no compilers, only runtime shared libs ───────────
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DJANGO_SETTINGS_MODULE=workflow_backend.settings.deployment

RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        libmagic1 \
        curl \
    && rm -rf /var/lib/apt/lists/* /tmp/* /var/tmp/*

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
CMD ["sh", "-c", "python manage.py collectstatic --noinput --clear && python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]

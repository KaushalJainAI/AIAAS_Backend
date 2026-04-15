FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    libpq-dev \
    libmagic1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --upgrade pip \
    && pip install -r requirements.txt

COPY . .

RUN mkdir -p /app/data /app/media /app/static

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=5 \
    CMD curl --fail http://localhost:8000/api/health/ || exit 1

CMD ["sh", "-c", "python manage.py migrate --noinput && python manage.py runserver 0.0.0.0:8000"]

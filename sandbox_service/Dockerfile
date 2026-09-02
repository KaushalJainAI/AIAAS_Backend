# The sandbox sidecar image. It runs untrusted, LLM-authored Python, so it is
# built to hold nothing worth stealing and to have nowhere to send it: no
# application code, no credentials, no network egress (enforced by compose).
#
# The container is the security boundary. runner.py + executor.py add per-run
# resource limits and a seccomp filter on top; see their docstrings.
FROM python:3.12-slim

# libgomp1 is numpy/BLAS's OpenMP runtime; without it `import numpy` fails.
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 libseccomp2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY runner.py executor.py server.py ./

# A non-root user with no home and no shell. Combined with compose's
# `read_only: true` + a small tmpfs on /tmp, the process can write only to
# scratch space that vanishes with the container.
RUN useradd --no-create-home --shell /usr/sbin/nologin sandbox
USER sandbox

ENV SANDBOX_HOST=0.0.0.0 \
    SANDBOX_PORT=8100 \
    PYTHONUNBUFFERED=1

EXPOSE 8100

# A liveness probe that does not need curl in the image.
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8100/health', timeout=3).status==200 else 1)"

CMD ["python", "server.py"]

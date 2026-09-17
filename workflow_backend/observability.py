"""
Error reporting. Off unless `SENTRY_DSN` is set.

Before this, an unhandled exception in production existed only in a container
log on a single box, read by nobody until a user complained. Called from each
process entry point (`asgi.py`, `celery.py`) rather than from settings, so
importing settings in a test or a management command never opens a network
client.

Three choices worth knowing:

- **No PII by default.** `send_default_pii=False` keeps request bodies,
  cookies and user emails out of error reports. A chat request body is the
  user's own message, and a credential form holds an API key.
- **Tracing is opt-in** (`SENTRY_TRACES_SAMPLE_RATE`, default 0). Error
  reporting is nearly free; sampling every request on a 1.9 GB box is not.
- **A missing package is not a crash.** The SDK is in the requirements, but a
  process that cannot import it logs once and runs without reporting: losing
  the error reporter must not take the product down with it.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)


def init_error_reporting(component: str) -> bool:
    """Start Sentry for this process. Returns whether it was started."""
    dsn = os.environ.get("SENTRY_DSN", "").strip()
    if not dsn:
        return False
    try:
        import sentry_sdk
    except ImportError:
        logger.warning("SENTRY_DSN is set but sentry-sdk is not installed; errors will not be reported")
        return False

    try:
        traces = float(os.environ.get("SENTRY_TRACES_SAMPLE_RATE", "0") or 0)
    except ValueError:
        traces = 0.0

    sentry_sdk.init(
        dsn=dsn,
        environment=os.environ.get("SENTRY_ENVIRONMENT", "production"),
        release=os.environ.get("SENTRY_RELEASE") or None,
        send_default_pii=False,
        traces_sample_rate=traces,
    )
    sentry_sdk.set_tag("component", component)
    return True

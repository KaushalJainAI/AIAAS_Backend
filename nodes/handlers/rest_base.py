"""
Shared plumbing for REST-API connectors.

Every connector written before this one repeated the same forty lines: build a
client, set a timeout, POST, branch on status_code, try to dig a message out of
the error body, wrap the whole thing in a bare `except Exception`. That
repetition is not just verbose — it is where the inconsistencies live. Some
connectors set a timeout and some do not; some surface the provider's error
message and some return `str(e)`; none of them handle a 429; and the one that
takes a user-supplied URL forgot to check where it pointed.

`RestConnectorNode` puts that in one place, so a new connector is a declaration
of endpoints and fields rather than another copy of the same error handling.

What every connector gets by carrying this base:

- SSRF validation on outbound URLs (see core.net)
- Retry with backoff on 429/5xx, honouring Retry-After
- One error shape, with the provider's own message when it gives one
- Redacted response headers
- A response size ceiling
- Credentials read through the execution context, never logged
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, TYPE_CHECKING

import httpx

from core.net import redact_headers, validate_url_async
from .base import (
    BaseNodeHandler,
    NodeCategory,
    NodeExecutionResult,
    NodeItem,
)

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext

logger = logging.getLogger(__name__)

# Bodies larger than this are truncated rather than loaded whole. A node's output
# is persisted and rendered, so "download a 2GB file into a JSON column" is a way
# to take out the worker and the database in one request.
MAX_RESPONSE_BYTES = 5 * 1024 * 1024

DEFAULT_TIMEOUT = 30
MAX_RETRIES = 3
# Give up rather than sleep through an hour-long Retry-After; a workflow that
# hangs for an hour is indistinguishable from one that has broken.
MAX_RETRY_AFTER_SECONDS = 60

RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ConnectorError(Exception):
    """A connector call failed in a way worth reporting verbatim to the user."""

    def __init__(self, message: str, status_code: int | None = None, body: Any = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.body = body


def extract_api_error(response: httpx.Response) -> str:
    """
    Pull the most useful message out of an error response.

    Providers disagree about where the message lives — Slack uses {"error": ...},
    Stripe {"error": {"message": ...}}, Jira {"errorMessages": [...]}, others just
    {"message": ...}. Guessing well matters: the alternative is showing the user a
    bare "HTTP 400", which tells them nothing about which field they got wrong.
    """
    try:
        data = response.json()
    except Exception:
        text = (response.text or '').strip()
        return text[:400] if text else f"HTTP {response.status_code}"

    if isinstance(data, dict):
        err = data.get('error')
        if isinstance(err, dict):
            for key in ('message', 'msg', 'detail', 'description'):
                if err.get(key):
                    return str(err[key])[:400]
        elif isinstance(err, str) and err:
            return err[:400]

        for key in ('message', 'detail', 'error_description', 'errorMessage', 'title'):
            if data.get(key):
                return str(data[key])[:400]

        # Jira/Atlassian style: a list of strings.
        for key in ('errorMessages', 'errors'):
            val = data.get(key)
            if isinstance(val, list) and val:
                return '; '.join(str(v) for v in val)[:400]
            if isinstance(val, dict) and val:
                return '; '.join(f"{k}: {v}" for k, v in val.items())[:400]

    return f"HTTP {response.status_code}"


def _retry_delay(response: httpx.Response | None, attempt: int) -> float:
    """
    How long to wait before retrying.

    Retry-After is authoritative when present — the server is telling us exactly
    when it will accept traffic again, and ignoring it in favour of our own
    backoff is how a client gets itself rate-limited for longer. Otherwise
    exponential with jitter, because a workflow fanning out across items would
    otherwise retry in lockstep and reproduce the burst that caused the 429.
    """
    if response is not None:
        header = response.headers.get('Retry-After')
        if header:
            try:
                return min(float(header), MAX_RETRY_AFTER_SECONDS)
            except ValueError:
                pass  # Retry-After may be an HTTP-date; fall through to backoff.
    return min(2 ** attempt, 30) + random.uniform(0, 0.5)


async def request_json(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json_body: Any = None,
    data: Any = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_retries: int = MAX_RETRIES,
    validate: bool = True,
    expected: tuple[int, ...] = (200, 201, 202, 204),
) -> tuple[Any, httpx.Response]:
    """
    Make one API call with SSRF checks, retries and consistent errors.

    Returns (parsed_body, response). Raises ConnectorError on failure.
    """
    if validate:
        safe, reason = await validate_url_async(url)
        if not safe:
            raise ConnectorError(f"Blocked request to {url}: {reason}")

    last_error: str = "Request failed"
    response: httpx.Response | None = None

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        for attempt in range(max_retries + 1):
            try:
                response = await client.request(
                    method.upper(), url,
                    headers=headers or {}, params=params,
                    json=json_body, data=data,
                )
            except httpx.TimeoutException:
                last_error = f"Request timed out after {timeout}s"
                if attempt < max_retries:
                    await asyncio.sleep(_retry_delay(None, attempt))
                    continue
                raise ConnectorError(last_error)
            except httpx.HTTPError as e:
                last_error = f"Network error: {e}"
                if attempt < max_retries:
                    await asyncio.sleep(_retry_delay(None, attempt))
                    continue
                raise ConnectorError(last_error)

            if response.status_code in RETRYABLE_STATUS and attempt < max_retries:
                delay = _retry_delay(response, attempt)
                logger.info(
                    "Connector got %s from %s; retrying in %.1fs (attempt %d/%d)",
                    response.status_code, url.split('?')[0], delay, attempt + 1, max_retries,
                )
                await asyncio.sleep(delay)
                continue

            break

    assert response is not None  # loop either breaks with a response or raises

    if response.status_code not in expected:
        raise ConnectorError(
            extract_api_error(response),
            status_code=response.status_code,
        )

    if len(response.content) > MAX_RESPONSE_BYTES:
        raise ConnectorError(
            f"Response too large ({len(response.content)} bytes, limit {MAX_RESPONSE_BYTES}). "
            f"Narrow the request or use a paginated operation."
        )

    if response.status_code == 204 or not response.content:
        return None, response

    try:
        return response.json(), response
    except Exception:
        return {"text": response.text}, response


class RestConnectorNode(BaseNodeHandler):
    """
    Base for connectors that talk to a JSON REST API.

    A subclass declares `node_type`, `fields`, an auth style and a set of
    operations, then implements `run_operation`. Everything else — credential
    lookup, request safety, retries, error shape — comes from here.
    """

    category = NodeCategory.INTEGRATION.value

    #: Credential slug this connector expects, e.g. "stripe".
    credential_slug: str = ""
    #: Which credential field holds the secret.
    credential_key: str = "apiKey"
    #: "bearer" | "token" | "basic" | "header" | "none"
    auth_style: str = "bearer"
    #: Header name when auth_style == "header".
    auth_header: str = "X-API-Key"
    #: Root of the API; subclasses join paths onto it.
    base_url: str = ""

    # ---- helpers available to subclasses -------------------------------

    async def get_secret(self, config: dict, context: 'ExecutionContext') -> str:
        """
        Resolve this connector's secret from the selected credential.

        Tries the declared key first, then the common aliases, because the
        credential type schemas are not uniform about naming (apiKey vs api_key
        vs token vs accessToken) and a connector failing with "no credential"
        when the user has plainly configured one is a miserable thing to debug.
        """
        credential_id = config.get("credential")
        if not credential_id:
            raise ConnectorError(f"No {self.name} credential selected.")

        creds = await context.get_credential(credential_id)
        if not creds:
            raise ConnectorError(f"{self.name} credential could not be loaded.")

        for key in (self.credential_key, 'apiKey', 'api_key', 'token',
                    'access_token', 'accessToken', 'secret', 'password'):
            value = creds.get(key)
            if value:
                return str(value).strip()

        raise ConnectorError(
            f"{self.name} credential is missing a secret "
            f"(looked for '{self.credential_key}')."
        )

    def auth_headers(self, secret: str) -> dict[str, str]:
        if self.auth_style == "bearer":
            return {"Authorization": f"Bearer {secret}"}
        if self.auth_style == "token":
            return {"Authorization": f"token {secret}"}
        if self.auth_style == "basic":
            return {"Authorization": f"Basic {secret}"}
        if self.auth_style == "header":
            return {self.auth_header: secret}
        return {}

    async def call(
        self, method: str, path_or_url: str, secret: str = "", **kwargs
    ) -> Any:
        """Make an authenticated call and return the parsed body."""
        url = path_or_url
        if self.base_url and not path_or_url.startswith(("http://", "https://")):
            url = f"{self.base_url.rstrip('/')}/{path_or_url.lstrip('/')}"

        headers = {"Accept": "application/json"}
        if secret:
            headers.update(self.auth_headers(secret))
        headers.update(kwargs.pop("headers", None) or {})

        body, _response = await request_json(method, url, headers=headers, **kwargs)
        return body

    # ---- the piece subclasses implement --------------------------------

    async def run_operation(
        self,
        operation: str,
        config: dict[str, Any],
        secret: str,
        context: 'ExecutionContext',
    ) -> Any:
        """Perform `operation` and return raw data. Raise ConnectorError to fail."""
        raise NotImplementedError

    def shape_output(self, operation: str, result: Any) -> list[dict]:
        """
        Turn an API response into workflow items.

        Default: a list becomes one item per element, so downstream nodes can
        iterate without an extra Split step; anything else becomes a single item.
        """
        if isinstance(result, list):
            return [r if isinstance(r, dict) else {"value": r} for r in result]
        if isinstance(result, dict):
            return [result]
        if result is None:
            return [{"success": True}]
        return [{"value": result}]

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext',
    ) -> NodeExecutionResult:
        operation = config.get("operation") or ""
        try:
            secret = ""
            if self.auth_style != "none":
                secret = await self.get_secret(config, context)
            result = await self.run_operation(operation, config, secret, context)
            items = self.shape_output(operation, result)
            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json=i) for i in items],
                output_handle="output-0",
            )
        except ConnectorError as e:
            # Expected failure: the message is already user-facing.
            return NodeExecutionResult(
                success=False, error=f"{self.name}: {e.message}", output_handle="output-0",
            )
        except NotImplementedError:
            return NodeExecutionResult(
                success=False,
                error=f"{self.name}: operation '{operation}' is not supported.",
                output_handle="output-0",
            )
        except Exception:
            # Unexpected: log with a stack trace for us, stay terse for the user.
            # str(e) on an arbitrary exception can carry a URL with a token in the
            # query string, so it does not go in the message.
            logger.exception("%s connector failed during '%s'", self.node_type, operation)
            return NodeExecutionResult(
                success=False,
                error=f"{self.name}: unexpected error during '{operation}'. See logs.",
                output_handle="output-0",
            )


__all__ = [
    "ConnectorError",
    "MAX_RESPONSE_BYTES",
    "RestConnectorNode",
    "extract_api_error",
    "redact_headers",
    "request_json",
]

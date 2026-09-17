"""
One door to Google's REST APIs, for every native Google connector tool.

Why this exists instead of an MCP server: every Google connector used to be an
`npx` subprocess — 70-150 MB of Node each, on a box whose web container has
384 MB — and the memory budget that kept those from killing daphne also meant
their tool lists were never built, so the tools silently never appeared. A
REST call needs a token and an HTTP client, both of which this process already
has, so the connector now costs one request instead of one process.

Three rules, each a failure mode this module exists to name rather than hide:

* **A failure has a code the model can act on.** `credential_missing` (connect
  Google), `scope_missing` (reconnect, the grant is too narrow), `api_disabled`
  (an operator problem — the API is off in the GCP project), `not_found`,
  `tool_error`. The shape matches `MCPToolProvider.execute`'s errors, so a
  model that learned what a connector failure looks like reads both alike.
* **A 401 forces one refresh and one retry**, never more. Our clock only knows
  what `expires_in` said at issue; a revoked or rotated token looks fresh
  until it is used. Retrying past one refresh would hammer the token endpoint
  with a grant that is dead.
* **The token never leaves this module.** Tools get parsed JSON or bytes, so
  nothing a tool returns to the model can carry the bearer by accident.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from workflow_backend.httpclient import shared_client
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

GOOGLE_CREDENTIAL_SLUG = "google-oauth2"

#: One request. Generous because Drive exports of a large Doc are slow, and
#: short enough that a hung socket cannot hold a turn for long.
REQUEST_TIMEOUT_SECONDS = 30.0

#: Retries for 429 / 5xx. One, because the turn is waiting and Google's own
#: guidance is exponential backoff measured in seconds — a second failure is
#: better reported than waited out.
TRANSIENT_RETRIES = 1
BACKOFF_SECONDS = 1.5

#: The most a download may be, for `download`. Drive files can be gigabytes;
#: anything past this is not a file a model should read in one turn.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024

CONNECTIONS_HINT = "Ask the user to connect Google on the Connections page."


class GoogleAPIError(Exception):
    """A Google call that failed in a way the tool should report, not raise."""

    def __init__(self, code: str, message: str, status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status

    def as_json(self) -> str:
        return json.dumps({"error": self.message, "code": self.code})


def _load_credential(user_id: int):
    from credentials.manager import CredentialManager

    return CredentialManager.lookup_by_slug_sync(GOOGLE_CREDENTIAL_SLUG, user_id)


def _token_for(credential, force_refresh: bool) -> str | None:
    return credential.get_valid_access_token(force_refresh=force_refresh)


def has_google_credential_sync(user_id: int | None) -> bool:
    """Whether this user has an active Google credential at all.

    Presence, not validity: whether the token still refreshes is only known by
    using it, and a tool that then fails with `credential_invalid` says so. A
    listing that refreshed tokens to decide what to offer would put a network
    call in front of every turn.
    """
    if not user_id:
        return False
    return _load_credential(user_id) is not None


async def _access_token(user_id: int | None, force_refresh: bool = False) -> str:
    if not user_id:
        raise GoogleAPIError("credential_missing", f"No signed-in user. {CONNECTIONS_HINT}")
    credential = await sync_to_async(_load_credential)(user_id)
    if credential is None:
        raise GoogleAPIError(
            "credential_missing", f"Google is not connected. {CONNECTIONS_HINT}",
        )
    token = await sync_to_async(_token_for)(credential, force_refresh)
    if not token:
        raise GoogleAPIError(
            "credential_invalid",
            "The Google connection has expired or was revoked. Ask the user to "
            "reconnect Google on the Connections page.",
        )
    return token


async def _send(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """The one network call. Separate so tests replace the wire, not the logic."""
    return await shared_client().request(
        method, url, timeout=REQUEST_TIMEOUT_SECONDS, **kwargs,
    )


def _reason(response: httpx.Response) -> tuple[str, str]:
    """(reason, message) from a Google error body, tolerating any shape."""
    try:
        err = response.json().get("error") or {}
    except ValueError:
        return "", response.text[:300]
    if isinstance(err, str):
        return err, err
    reasons = [
        (d.get("reason") or "")
        for d in (err.get("errors") or []) + (err.get("details") or [])
        if isinstance(d, dict)
    ]
    return " ".join(r for r in reasons if r), str(err.get("message") or "")


def _classify(response: httpx.Response) -> GoogleAPIError:
    reason, message = _reason(response)
    status = response.status_code
    lowered = f"{reason} {message}".lower()
    if status == 403 and (
        "scope" in lowered or "insufficientpermissions" in lowered
    ):
        return GoogleAPIError(
            "scope_missing",
            "The Google connection does not include permission for this. Ask the "
            "user to reconnect this service on the Connections page so the "
            "missing permission is granted.",
            status,
        )
    if status == 403 and (
        "accessnotconfigured" in lowered or "service_disabled" in lowered
        or "has not been used" in lowered or "is disabled" in lowered
    ):
        return GoogleAPIError(
            "api_disabled",
            "This Google API is not enabled for the platform's Google Cloud "
            "project. This is a platform configuration problem, not something "
            "the user can fix — say so plainly.",
            status,
        )
    if status == 404:
        return GoogleAPIError(
            "not_found", message or "Google could not find that item.", status,
        )
    if status == 400:
        return GoogleAPIError(
            "bad_request", f"Google rejected the request: {message}", status,
        )
    return GoogleAPIError(
        "tool_error", f"Google API error {status}: {message}".strip(), status,
    )


async def _request(
    context: dict[str, Any], method: str, url: str, **kwargs: Any,
) -> httpx.Response:
    user_id = (context or {}).get("user_id")
    base_headers = kwargs.pop("headers", None) or {}
    token = await _access_token(user_id)
    refreshed = False
    transient_left = TRANSIENT_RETRIES
    while True:
        headers = {**base_headers, "Authorization": f"Bearer {token}"}
        try:
            response = await _send(method, url, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            if transient_left > 0:
                transient_left -= 1
                await asyncio.sleep(BACKOFF_SECONDS)
                continue
            raise GoogleAPIError("tool_error", f"Could not reach Google: {exc}") from exc

        status = response.status_code
        if status == 401 and not refreshed:
            refreshed = True
            token = await _access_token(user_id, force_refresh=True)
            continue
        if status == 401:
            raise GoogleAPIError(
                "credential_invalid",
                "Google refused the connection even after refreshing it. Ask the "
                "user to reconnect Google on the Connections page.",
                401,
            )
        if (status == 429 or status >= 500) and transient_left > 0:
            transient_left -= 1
            await asyncio.sleep(BACKOFF_SECONDS)
            continue
        if status >= 400:
            error = _classify(response)
            logger.info(
                "[Google] %s %s -> %s (%s)", method, url.split("?")[0], status, error.code,
            )
            raise error
        return response


async def get_json(context: dict[str, Any], url: str, **kwargs: Any) -> Any:
    response = await _request(context, "GET", url, **kwargs)
    return response.json() if response.content else {}


async def send_json(
    context: dict[str, Any], method: str, url: str, **kwargs: Any,
) -> Any:
    response = await _request(context, method, url, **kwargs)
    return response.json() if response.content else {}


async def download(
    context: dict[str, Any], url: str, *, max_bytes: int = DEFAULT_MAX_BYTES,
    **kwargs: Any,
) -> bytes:
    """A body as bytes, refused past `max_bytes` rather than truncated.

    Truncating a binary file (a PDF, a docx) produces something no extractor
    can open, so the honest answer for an oversized file is to say so.
    """
    response = await _request(context, "GET", url, **kwargs)
    body = response.content
    if len(body) > max_bytes:
        raise GoogleAPIError(
            "too_large",
            f"That file is {len(body) // (1024 * 1024)} MB, over the "
            f"{max_bytes // (1024 * 1024)} MB a tool may read.",
        )
    return body


def handles_google_errors(func):
    """Turn a `GoogleAPIError` into the JSON the model reads, for one tool.

    Applied under `@tool`, so the registry still stores a plain coroutine
    function. Anything else propagates to `execute_tool`'s own backstop, which
    is where an unexpected bug belongs — this only translates the failures we
    classified.
    """
    import functools

    @functools.wraps(func)
    async def wrapper(args: dict, context: dict) -> str:
        try:
            return await func(args, context)
        except GoogleAPIError as e:
            return e.as_json()

    return wrapper

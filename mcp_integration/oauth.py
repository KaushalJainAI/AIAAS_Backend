"""
OAuth for remote MCP servers.

A hosted MCP server does not take a pasted token — it points at an
authorization server and expects a bearer. `mcp.notion.com` publishes
everything needed to do that automatically:

    /.well-known/oauth-protected-resource/mcp  -> which authorization server
    /.well-known/oauth-authorization-server    -> its endpoints + capabilities

and that second document advertises a `registration_endpoint`, so this module
registers a client for the deployment (RFC 7591 Dynamic Client Registration)
instead of every install having to create a Notion app and carry a secret.
Measured against Notion 2026-09-01: registration returns a `client_id` and **no**
`client_secret`, i.e. a public client, which is why PKCE is mandatory here
rather than optional.

Nothing in this module is Notion-specific. It reads what a server advertises
and refuses what it cannot do, so any server implementing the same discovery
documents works — that is the point of using the standard rather than
hand-coding one provider the way `credentials/oauth.py` does for Google.

Security notes, each of which is a decision rather than a default:

* **The PKCE verifier never travels in the URL.** It is stored server-side on
  `MCPOAuthFlow`, keyed by an opaque `state`. Putting it in the state — which
  rides the browser redirect next to the code — would hand it to anything able
  to intercept the code, defeating the point.
* **Discovery and token URLs are SSRF-guarded** exactly like a server URL, and
  re-checked at use, because they are read from a remote document.
* **The issuer must match the resource's declared authorization server**, so a
  compromised resource document cannot point the flow at an attacker's token
  endpoint and harvest a code.
"""
from __future__ import annotations

import base64
import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode, urljoin, urlparse

import httpx
from cryptography.fernet import Fernet
from django.conf import settings
from django.utils import timezone

from .models import MCPOAuthClient, MCPOAuthFlow, MCPOAuthToken, MCPServer

logger = logging.getLogger(__name__)

#: Budgets for the three outbound calls. Short: these all sit inside a request
#: a person is waiting on, and a hosted provider that cannot answer discovery
#: in ten seconds is not going to complete an authorization either.
DISCOVERY_TIMEOUT = 10.0
REGISTER_TIMEOUT = 15.0
TOKEN_TIMEOUT = 20.0

#: Refresh this long before the server's own expiry, so a token cannot lapse
#: between the check and the call that uses it.
EXPIRY_SKEW = timedelta(minutes=5)

CLIENT_NAME = "AIAAS"


class MCPOAuthError(RuntimeError):
    """Anything that stops an authorization from completing.

    One exception type, because every caller does the same thing with it:
    show the sentence to the user. The sentence is what varies.
    """


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ServerMetadata:
    """What an authorization server says it can do."""

    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    registration_endpoint: str | None
    scopes_supported: tuple[str, ...]
    supports_pkce_s256: bool

    @property
    def default_scope(self) -> str:
        return " ".join(self.scopes_supported) if self.scopes_supported else ""


def _guard(url: str, *, what: str) -> None:
    """Refuse a URL that resolves somewhere private.

    Applied to every URL taken from a discovery document, not just to the ones
    a user typed: the documents are fetched from a remote host, so their
    contents are third-party input with the same standing as a form field.
    """
    from core.safety.net import validate_url

    is_safe, reason = validate_url(url)
    if not is_safe:
        raise MCPOAuthError(f"{what} is not a safe URL: {reason}")


def _get_json(client: httpx.Client, url: str, *, what: str) -> dict[str, Any] | None:
    """GET a discovery document, or None when the server does not publish it."""
    _guard(url, what=what)
    try:
        response = client.get(url, timeout=DISCOVERY_TIMEOUT,
                              headers={"Accept": "application/json"})
    except httpx.HTTPError as exc:
        raise MCPOAuthError(f"Could not reach {what}: {exc}") from exc
    if response.status_code == 404:
        return None
    if response.status_code >= 400:
        raise MCPOAuthError(
            f"{what} returned {response.status_code}: {response.text[:200]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise MCPOAuthError(f"{what} did not return JSON") from exc


def _origin(url: str) -> str:
    parts = urlparse(url)
    return f"{parts.scheme}://{parts.netloc}"


def discover(resource_url: str) -> ServerMetadata:
    """Resolve an MCP endpoint to its authorization server's metadata.

    Two hops, in the order the spec defines them: the *resource* says which
    authorization server governs it, and only then do we read that server's
    document. Skipping the first hop and guessing the issuer from the
    resource's own origin happens to work for Notion and is wrong in general.
    """
    _guard(resource_url, what="The MCP endpoint")
    origin = _origin(resource_url)
    path = urlparse(resource_url).path.rstrip("/")

    with httpx.Client(follow_redirects=True) as client:
        # RFC 9728 puts the resource document under a path-suffixed well-known.
        # The unsuffixed form is the fallback for servers mounted at the root.
        resource_doc = None
        for candidate in (
            urljoin(origin, f"/.well-known/oauth-protected-resource{path}"),
            urljoin(origin, "/.well-known/oauth-protected-resource"),
        ):
            resource_doc = _get_json(client, candidate, what="The protected-resource document")
            if resource_doc:
                break

        issuers = (resource_doc or {}).get("authorization_servers") or []
        issuer = (issuers[0] if issuers else origin).rstrip("/")
        _guard(issuer, what="The authorization server")

        # The authorization server document lives under the issuer. Try the
        # RFC 8414 form first, then OIDC's, then the issuer itself — servers
        # differ and a missing document is not an error until all three miss.
        server_doc = None
        for candidate in (
            urljoin(issuer + "/", ".well-known/oauth-authorization-server"),
            urljoin(issuer + "/", ".well-known/openid-configuration"),
        ):
            server_doc = _get_json(client, candidate, what="The authorization-server document")
            if server_doc:
                break

    if not server_doc:
        raise MCPOAuthError(
            "This server does not publish OAuth metadata, so it cannot be "
            "connected this way. Add its token as a credential instead."
        )

    authorization_endpoint = server_doc.get("authorization_endpoint")
    token_endpoint = server_doc.get("token_endpoint")
    if not authorization_endpoint or not token_endpoint:
        raise MCPOAuthError(
            "The authorization server did not advertise the endpoints needed "
            "to sign in."
        )
    _guard(authorization_endpoint, what="The authorization endpoint")
    _guard(token_endpoint, what="The token endpoint")

    methods = server_doc.get("code_challenge_methods_supported") or []
    return ServerMetadata(
        issuer=(server_doc.get("issuer") or issuer).rstrip("/"),
        authorization_endpoint=authorization_endpoint,
        token_endpoint=token_endpoint,
        registration_endpoint=server_doc.get("registration_endpoint"),
        scopes_supported=tuple(server_doc.get("scopes_supported") or ()),
        supports_pkce_s256="S256" in methods,
    )


# ---------------------------------------------------------------------------
# Client registration
# ---------------------------------------------------------------------------

def _fernet() -> Fernet:
    return Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)


def get_or_register_client(
    metadata: ServerMetadata, redirect_uri: str,
) -> MCPOAuthClient:
    """The deployment's client for this issuer, registering one if needed.

    Registration is per `(issuer, redirect_uri)` and happens at most once; a
    later flow reuses the row. If the server offers no registration endpoint we
    stop with an explanation rather than inventing a client id — there is no
    fallback that could work.
    """
    existing = MCPOAuthClient.objects.filter(
        issuer=metadata.issuer, redirect_uri=redirect_uri,
    ).first()
    if existing:
        return existing

    if not metadata.registration_endpoint:
        raise MCPOAuthError(
            "This server does not support automatic app registration, so it "
            "needs a client ID configured manually."
        )
    _guard(metadata.registration_endpoint, what="The registration endpoint")

    body = {
        "client_name": CLIENT_NAME,
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }
    if scope := metadata.default_scope:
        body["scope"] = scope

    try:
        response = httpx.post(
            metadata.registration_endpoint, json=body, timeout=REGISTER_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise MCPOAuthError(f"Could not register with the server: {exc}") from exc
    if response.status_code >= 400:
        raise MCPOAuthError(
            f"The server refused app registration "
            f"({response.status_code}): {response.text[:200]}"
        )

    payload = response.json()
    client_id = payload.get("client_id")
    if not client_id:
        raise MCPOAuthError("The server registered no client id.")

    secret = payload.get("client_secret")
    client, _ = MCPOAuthClient.objects.update_or_create(
        issuer=metadata.issuer, redirect_uri=redirect_uri,
        defaults={
            "client_id": client_id,
            "client_secret": _fernet().encrypt(secret.encode()) if secret else None,
            "metadata": {
                "authorization_endpoint": metadata.authorization_endpoint,
                "token_endpoint": metadata.token_endpoint,
                "scopes_supported": list(metadata.scopes_supported),
            },
        },
    )
    logger.info("Registered MCP OAuth client %s with %s", client_id, metadata.issuer)
    return client


def _client_secret(client: MCPOAuthClient) -> str | None:
    if not client.client_secret:
        return None
    return _fernet().decrypt(bytes(client.client_secret)).decode()


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------

def _pkce_pair() -> tuple[str, str]:
    """`(verifier, challenge)` for S256."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).decode().rstrip("=")
    return verifier, challenge


def begin(server: MCPServer, user, redirect_uri: str) -> str:
    """Start an authorization and return the URL to send the user to."""
    if server.type not in MCPServer.REMOTE_TYPES:
        raise MCPOAuthError("Only remote (http/sse) servers can use OAuth.")
    if not server.url:
        raise MCPOAuthError("This server has no URL to authorize against.")

    metadata = discover(server.url)
    if not metadata.supports_pkce_s256:
        # Refused rather than downgraded to `plain`: a public client without
        # S256 has no meaningful protection on the code exchange.
        raise MCPOAuthError(
            "This server does not support PKCE (S256), which is required to "
            "connect it safely."
        )

    client = get_or_register_client(metadata, redirect_uri)
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)

    MCPOAuthFlow.objects.create(
        state=state, user=user, server=server, client=client,
        code_verifier=verifier, redirect_uri=redirect_uri,
    )
    # One in flight per (user, server) is plenty; clearing the rest keeps a
    # user who clicked Connect five times from leaving five live verifiers.
    MCPOAuthFlow.objects.filter(
        user=user, server=server,
    ).exclude(state=state).delete()

    params = {
        "response_type": "code",
        "client_id": client.client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    if scope := metadata.default_scope:
        params["scope"] = scope
    # `resource` is what binds the issued token to this MCP endpoint (RFC 8707);
    # without it a server may issue a token good for something else entirely.
    params["resource"] = server.url

    return f"{metadata.authorization_endpoint}?{urlencode(params)}"


def _store_tokens(flow: MCPOAuthFlow, payload: dict[str, Any]) -> MCPOAuthToken:
    access = payload.get("access_token")
    if not access:
        raise MCPOAuthError("The server returned no access token.")

    expires_at = None
    if expires_in := payload.get("expires_in"):
        try:
            expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            expires_at = None

    fernet = _fernet()
    refresh = payload.get("refresh_token")
    token, _ = MCPOAuthToken.objects.update_or_create(
        user=flow.user, server=flow.server,
        defaults={
            "client": flow.client,
            "access_token": fernet.encrypt(access.encode()),
            # Keep the previous refresh token when the server sends none: many
            # issue one only on first authorization, and overwriting it with
            # NULL turns a renewable connection into one that dies at expiry.
            "refresh_token": (
                fernet.encrypt(refresh.encode()) if refresh
                else _existing_refresh(flow.user_id, flow.server_id)
            ),
            "expires_at": expires_at,
            "scope": payload.get("scope") or "",
        },
    )
    return token


def _existing_refresh(user_id: int, server_id: int):
    row = MCPOAuthToken.objects.filter(
        user_id=user_id, server_id=server_id,
    ).values_list("refresh_token", flat=True).first()
    return row or None


def complete(state: str, code: str, user) -> MCPOAuthToken:
    """Exchange an authorization code for tokens and store them."""
    flow = MCPOAuthFlow.objects.select_related("client", "server").filter(
        state=state,
    ).first()
    if flow is None:
        raise MCPOAuthError("That sign-in link has already been used or is unknown.")
    # Checked before anything else is done with the row: a state belonging to
    # another account must not be usable even to learn which server it names.
    if flow.user_id != user.id:
        raise MCPOAuthError("That sign-in does not belong to this account.")
    if flow.is_expired():
        flow.delete()
        raise MCPOAuthError("That sign-in took too long. Start again.")

    client = flow.client
    token_endpoint = (client.metadata or {}).get("token_endpoint")
    if not token_endpoint:
        token_endpoint = discover(flow.server.url).token_endpoint
    _guard(token_endpoint, what="The token endpoint")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": flow.redirect_uri,
        "client_id": client.client_id,
        "code_verifier": flow.code_verifier,
        "resource": flow.server.url,
    }
    if secret := _client_secret(client):
        data["client_secret"] = secret

    try:
        response = httpx.post(token_endpoint, data=data, timeout=TOKEN_TIMEOUT)
    except httpx.HTTPError as exc:
        raise MCPOAuthError(f"Could not reach the token endpoint: {exc}") from exc
    if response.status_code >= 400:
        raise MCPOAuthError(
            f"The server refused the sign-in ({response.status_code}): "
            f"{response.text[:200]}"
        )

    token = _store_tokens(flow, response.json())
    # Single-use: the code is spent, and leaving the row would let a replayed
    # callback re-exchange it.
    flow.delete()
    return token


# ---------------------------------------------------------------------------
# Use
# ---------------------------------------------------------------------------

def _needs_refresh(token: MCPOAuthToken) -> bool:
    if token.expires_at is None:
        return False
    return timezone.now() >= token.expires_at - EXPIRY_SKEW


def access_token_for(server_id: int, user_id: int) -> str | None:
    """A currently-valid bearer for this (user, server), or None.

    None is a real answer — the user has not connected this server — and the
    injector treats it as "no header", not as an error.
    """
    token = MCPOAuthToken.objects.select_related("client").filter(
        server_id=server_id, user_id=user_id,
    ).first()
    if token is None:
        return None

    fernet = _fernet()
    if not _needs_refresh(token):
        try:
            return fernet.decrypt(bytes(token.access_token)).decode()
        except Exception:  # noqa: BLE001
            logger.warning("Could not decrypt MCP OAuth token %s", token.id)
            return None

    if not token.refresh_token:
        logger.info(
            "MCP OAuth token %s expired with no refresh token; reconnect needed",
            token.id,
        )
        return None

    try:
        refresh = fernet.decrypt(bytes(token.refresh_token)).decode()
    except Exception:  # noqa: BLE001
        return None

    client = token.client
    token_endpoint = (client.metadata or {}).get("token_endpoint")
    if not token_endpoint:
        return None
    try:
        _guard(token_endpoint, what="The token endpoint")
    except MCPOAuthError:
        return None

    data = {
        "grant_type": "refresh_token",
        "refresh_token": refresh,
        "client_id": client.client_id,
    }
    if secret := _client_secret(client):
        data["client_secret"] = secret

    try:
        response = httpx.post(token_endpoint, data=data, timeout=TOKEN_TIMEOUT)
    except httpx.HTTPError as exc:
        logger.warning("MCP OAuth refresh failed for token %s: %s", token.id, exc)
        return None
    if response.status_code >= 400:
        logger.warning(
            "MCP OAuth refresh refused for token %s: %s %s",
            token.id, response.status_code, response.text[:200],
        )
        return None

    payload = response.json()
    access = payload.get("access_token")
    if not access:
        return None

    token.access_token = fernet.encrypt(access.encode())
    if new_refresh := payload.get("refresh_token"):
        token.refresh_token = fernet.encrypt(new_refresh.encode())
    if expires_in := payload.get("expires_in"):
        try:
            token.expires_at = timezone.now() + timedelta(seconds=int(expires_in))
        except (TypeError, ValueError):
            pass
    token.save(update_fields=["access_token", "refresh_token", "expires_at", "updated_at"])
    return access


def disconnect(server_id: int, user_id: int) -> int:
    """Forget this user's authorization for a server. Returns rows removed."""
    removed, _ = MCPOAuthToken.objects.filter(
        server_id=server_id, user_id=user_id,
    ).delete()
    MCPOAuthFlow.objects.filter(server_id=server_id, user_id=user_id).delete()
    return removed


def is_connected(server_id: int, user_id: int) -> bool:
    return MCPOAuthToken.objects.filter(
        server_id=server_id, user_id=user_id,
    ).exists()

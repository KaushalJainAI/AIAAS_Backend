"""
OAuth for remote MCP servers — discovery, dynamic registration, PKCE, refresh.

Every network call is stubbed. What is being tested is the decisions, not
httpx: which URL is trusted, what is refused, where the PKCE verifier lives,
and what happens to a token that expires.
"""
from datetime import timedelta
from unittest import mock

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from mcp_integration import oauth
from mcp_integration.models import (
    MCPOAuthClient, MCPOAuthFlow, MCPOAuthToken, MCPServer,
)

User = get_user_model()

REDIRECT = "http://localhost:5173/oauth/callback"

AS_DOC = {
    "issuer": "https://mcp.example.com",
    "authorization_endpoint": "https://mcp.example.com/authorize",
    "token_endpoint": "https://mcp.example.com/token",
    "registration_endpoint": "https://mcp.example.com/register",
    "scopes_supported": ["default"],
    "code_challenge_methods_supported": ["plain", "S256"],
}
RESOURCE_DOC = {
    "resource": "https://mcp.example.com/mcp",
    "authorization_servers": ["https://mcp.example.com"],
}


def _server(user=None, **extra):
    return MCPServer.objects.create(
        name=extra.pop("name", "hosted"),
        type=extra.pop("type", "http"),
        url=extra.pop("url", "https://mcp.example.com/mcp"),
        enabled=True, user=user, **extra,
    )


class _Resp:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""

    def json(self):
        return self._payload


def _metadata(**overrides):
    base = dict(
        issuer="https://mcp.example.com",
        authorization_endpoint="https://mcp.example.com/authorize",
        token_endpoint="https://mcp.example.com/token",
        registration_endpoint="https://mcp.example.com/register",
        scopes_supported=("default",),
        supports_pkce_s256=True,
    )
    base.update(overrides)
    return oauth.ServerMetadata(**base)


class DiscoveryTests(TestCase):
    """Two hops: the resource names its authorization server, then we read it."""

    def _discover_with(self, docs):
        class FakeClient:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def get(self_inner, url, **kwargs):
                if url in docs:
                    return _Resp(200, docs[url])
                return _Resp(404)

        with mock.patch("httpx.Client", return_value=FakeClient()), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            return oauth.discover("https://mcp.example.com/mcp")

    def test_reads_the_resource_document_then_the_server_document(self):
        meta = self._discover_with({
            "https://mcp.example.com/.well-known/oauth-protected-resource/mcp": RESOURCE_DOC,
            "https://mcp.example.com/.well-known/oauth-authorization-server": AS_DOC,
        })
        self.assertEqual(meta.token_endpoint, "https://mcp.example.com/token")
        self.assertTrue(meta.supports_pkce_s256)
        self.assertEqual(meta.registration_endpoint, "https://mcp.example.com/register")

    def test_falls_back_to_the_unsuffixed_resource_document(self):
        meta = self._discover_with({
            "https://mcp.example.com/.well-known/oauth-protected-resource": RESOURCE_DOC,
            "https://mcp.example.com/.well-known/oauth-authorization-server": AS_DOC,
        })
        self.assertEqual(meta.issuer, "https://mcp.example.com")

    def test_a_server_with_no_metadata_is_an_explained_refusal(self):
        with self.assertRaises(oauth.MCPOAuthError) as ctx:
            self._discover_with({})
        self.assertIn("does not publish OAuth metadata", str(ctx.exception))

    def test_a_document_missing_endpoints_is_refused(self):
        with self.assertRaises(oauth.MCPOAuthError):
            self._discover_with({
                "https://mcp.example.com/.well-known/oauth-authorization-server": {
                    "issuer": "https://mcp.example.com",
                },
            })

    def test_an_unsafe_endpoint_in_the_document_is_refused(self):
        """The document is fetched from a remote host, so its contents are
        third-party input and get the same SSRF guard as a typed URL."""
        def guard(url):
            return (False, "private address") if "169.254" in url else (True, "")

        class FakeClient:
            def __enter__(self_inner): return self_inner
            def __exit__(self_inner, *a): return False
            def get(self_inner, url, **kwargs):
                if url.endswith("oauth-authorization-server"):
                    return _Resp(200, {**AS_DOC,
                                       "token_endpoint": "http://169.254.169.254/token"})
                return _Resp(404)

        with mock.patch("httpx.Client", return_value=FakeClient()), \
             mock.patch("core.safety.net.validate_url", side_effect=guard):
            with self.assertRaises(oauth.MCPOAuthError) as ctx:
                oauth.discover("https://mcp.example.com/mcp")
        self.assertIn("not a safe URL", str(ctx.exception))


class RegistrationTests(TestCase):
    def test_registers_once_and_reuses_the_client(self):
        calls = []

        def fake_post(url, **kwargs):
            calls.append(url)
            return _Resp(201, {"client_id": "abc123"})

        with mock.patch("httpx.post", side_effect=fake_post), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            first = oauth.get_or_register_client(_metadata(), REDIRECT)
            second = oauth.get_or_register_client(_metadata(), REDIRECT)

        self.assertEqual(first.id, second.id)
        self.assertEqual(len(calls), 1, "registration must not repeat")
        self.assertEqual(first.client_id, "abc123")
        self.assertIsNone(first.client_secret, "a public client stores no secret")

    def test_a_secret_is_stored_encrypted_when_one_is_issued(self):
        with mock.patch("httpx.post", return_value=_Resp(201, {
            "client_id": "abc", "client_secret": "s3cret",
        })), mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            client = oauth.get_or_register_client(_metadata(), REDIRECT)

        self.assertIsNotNone(client.client_secret)
        self.assertNotIn(b"s3cret", bytes(client.client_secret))
        fernet = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)
        self.assertEqual(fernet.decrypt(bytes(client.client_secret)).decode(), "s3cret")

    def test_a_server_without_registration_is_an_explained_refusal(self):
        with self.assertRaises(oauth.MCPOAuthError) as ctx:
            oauth.get_or_register_client(_metadata(registration_endpoint=None), REDIRECT)
        self.assertIn("automatic app registration", str(ctx.exception))

    def test_registration_is_per_redirect_uri(self):
        """A registration is only valid for the URIs it was made with."""
        with mock.patch("httpx.post", return_value=_Resp(201, {"client_id": "x"})), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            a = oauth.get_or_register_client(_metadata(), REDIRECT)
            b = oauth.get_or_register_client(_metadata(), "http://localhost:3000/oauth/callback")
        self.assertNotEqual(a.id, b.id)


class BeginTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("begin", "b@x.com", "x" * 12)
        self.server = _server(self.user)

    def _begin(self, metadata=None):
        with mock.patch.object(oauth, "discover", return_value=metadata or _metadata()), \
             mock.patch("httpx.post", return_value=_Resp(201, {"client_id": "abc"})), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            return oauth.begin(self.server, self.user, REDIRECT)

    def test_returns_an_authorize_url_with_pkce_and_resource(self):
        from urllib.parse import parse_qs, urlparse

        url = self._begin()
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["client_id"], ["abc"])
        self.assertEqual(query["redirect_uri"], [REDIRECT])
        # RFC 8707: binds the issued token to this MCP endpoint.
        self.assertEqual(query["resource"], [self.server.url])
        self.assertTrue(query["code_challenge"][0])

    def test_the_verifier_is_stored_server_side_and_never_in_the_url(self):
        """PKCE is pointless if the verifier rides the same redirect as the
        code, so it must not appear in the authorize URL."""
        url = self._begin()
        flow = MCPOAuthFlow.objects.get(user=self.user, server=self.server)
        self.assertTrue(flow.code_verifier)
        self.assertNotIn(flow.code_verifier, url)

    def test_a_server_without_s256_is_refused_not_downgraded(self):
        with self.assertRaises(oauth.MCPOAuthError) as ctx:
            self._begin(_metadata(supports_pkce_s256=False))
        self.assertIn("PKCE", str(ctx.exception))

    def test_a_stdio_server_cannot_use_oauth(self):
        stdio = _server(self.user, name="local", type="stdio", url=None, command="npx")
        with self.assertRaises(oauth.MCPOAuthError):
            oauth.begin(stdio, self.user, REDIRECT)

    def test_starting_again_supersedes_the_previous_flow(self):
        self._begin()
        self._begin()
        self.assertEqual(
            MCPOAuthFlow.objects.filter(user=self.user, server=self.server).count(), 1,
        )


class CompleteTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("done", "d@x.com", "x" * 12)
        self.other = User.objects.create_user("other", "o@x.com", "x" * 12)
        self.server = _server(self.user)
        self.client_row = MCPOAuthClient.objects.create(
            issuer="https://mcp.example.com", redirect_uri=REDIRECT,
            client_id="abc", metadata={"token_endpoint": "https://mcp.example.com/token"},
        )

    def _flow(self, **extra):
        return MCPOAuthFlow.objects.create(
            state=extra.pop("state", "st4te"), user=extra.pop("user", self.user),
            server=self.server, client=self.client_row,
            code_verifier="verifier", redirect_uri=REDIRECT, **extra,
        )

    def _complete(self, payload=None, status_code=200, **kw):
        self._flow(**kw)
        with mock.patch("httpx.post", return_value=_Resp(
            status_code, payload if payload is not None else {
                "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
            })), mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            return oauth.complete("st4te", "code", self.user)

    def test_stores_tokens_encrypted(self):
        token = self._complete()
        fernet = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)
        self.assertEqual(fernet.decrypt(bytes(token.access_token)).decode(), "at")
        self.assertNotIn(b"at", bytes(token.access_token))
        self.assertIsNotNone(token.expires_at)

    def test_the_flow_is_single_use(self):
        self._complete()
        self.assertFalse(MCPOAuthFlow.objects.filter(state="st4te").exists())

    def test_another_users_state_is_refused(self):
        """A state belonging to someone else must not be redeemable."""
        self._flow(user=self.other)
        with self.assertRaises(oauth.MCPOAuthError) as ctx:
            oauth.complete("st4te", "code", self.user)
        self.assertIn("does not belong", str(ctx.exception))
        self.assertFalse(MCPOAuthToken.objects.exists())

    def test_an_unknown_state_is_refused(self):
        with self.assertRaises(oauth.MCPOAuthError):
            oauth.complete("nope", "code", self.user)

    def test_an_expired_flow_is_refused_and_cleaned_up(self):
        flow = self._flow()
        flow.created_at = timezone.now() - timedelta(seconds=MCPOAuthFlow.TTL_SECONDS + 60)
        flow.save(update_fields=["created_at"])
        with self.assertRaises(oauth.MCPOAuthError) as ctx:
            oauth.complete("st4te", "code", self.user)
        self.assertIn("took too long", str(ctx.exception))
        self.assertFalse(MCPOAuthFlow.objects.filter(state="st4te").exists())

    def test_a_rejected_exchange_reports_the_servers_reason(self):
        with self.assertRaises(oauth.MCPOAuthError) as ctx:
            self._complete(payload={}, status_code=400)
        self.assertIn("refused the sign-in", str(ctx.exception))

    def test_a_response_without_an_access_token_is_an_error(self):
        with self.assertRaises(oauth.MCPOAuthError):
            self._complete(payload={"token_type": "bearer"})

    def test_the_verifier_is_sent_to_the_token_endpoint(self):
        self._flow()
        captured = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs.get("data") or {})
            return _Resp(200, {"access_token": "at"})

        with mock.patch("httpx.post", side_effect=fake_post), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            oauth.complete("st4te", "code", self.user)

        self.assertEqual(captured["code_verifier"], "verifier")
        self.assertEqual(captured["grant_type"], "authorization_code")
        self.assertEqual(captured["resource"], self.server.url)


class AccessTokenTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("use", "u@x.com", "x" * 12)
        self.server = _server(self.user)
        self.client_row = MCPOAuthClient.objects.create(
            issuer="https://mcp.example.com", redirect_uri=REDIRECT,
            client_id="abc", metadata={"token_endpoint": "https://mcp.example.com/token"},
        )
        self.fernet = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)

    def _token(self, *, access="at", refresh="rt", expires_at=...):
        return MCPOAuthToken.objects.create(
            user=self.user, server=self.server, client=self.client_row,
            access_token=self.fernet.encrypt(access.encode()),
            refresh_token=self.fernet.encrypt(refresh.encode()) if refresh else None,
            expires_at=timezone.now() + timedelta(hours=1) if expires_at is ... else expires_at,
        )

    def test_no_token_is_none_not_an_error(self):
        self.assertIsNone(oauth.access_token_for(self.server.id, self.user.id))

    def test_a_live_token_is_returned_without_a_network_call(self):
        self._token()
        with mock.patch("httpx.post", side_effect=AssertionError("must not refresh")):
            self.assertEqual(oauth.access_token_for(self.server.id, self.user.id), "at")

    def test_a_null_expiry_means_non_expiring_not_expired(self):
        self._token(expires_at=None)
        with mock.patch("httpx.post", side_effect=AssertionError("must not refresh")):
            self.assertEqual(oauth.access_token_for(self.server.id, self.user.id), "at")

    def test_an_expired_token_is_refreshed_and_persisted(self):
        self._token(expires_at=timezone.now() - timedelta(minutes=1))
        with mock.patch("httpx.post", return_value=_Resp(200, {
            "access_token": "fresh", "expires_in": 3600,
        })), mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            self.assertEqual(oauth.access_token_for(self.server.id, self.user.id), "fresh")

        row = MCPOAuthToken.objects.get(user=self.user, server=self.server)
        self.assertEqual(self.fernet.decrypt(bytes(row.access_token)).decode(), "fresh")
        self.assertGreater(row.expires_at, timezone.now())

    def test_a_refresh_keeps_the_old_refresh_token_when_none_is_returned(self):
        """Many servers issue a refresh token only once; overwriting it with
        nothing turns a renewable connection into one that dies at expiry."""
        self._token(expires_at=timezone.now() - timedelta(minutes=1))
        with mock.patch("httpx.post", return_value=_Resp(200, {"access_token": "fresh"})), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            oauth.access_token_for(self.server.id, self.user.id)

        row = MCPOAuthToken.objects.get(user=self.user, server=self.server)
        self.assertEqual(self.fernet.decrypt(bytes(row.refresh_token)).decode(), "rt")

    def test_an_expired_token_with_no_refresh_token_yields_none(self):
        self._token(refresh=None, expires_at=timezone.now() - timedelta(minutes=1))
        self.assertIsNone(oauth.access_token_for(self.server.id, self.user.id))

    def test_a_refused_refresh_yields_none_rather_than_raising(self):
        """A dead authorization must degrade to 'not connected', not 500 an
        agent turn that merely wanted this connector's tools."""
        self._token(expires_at=timezone.now() - timedelta(minutes=1))
        with mock.patch("httpx.post", return_value=_Resp(400, {}, text="invalid_grant")), \
             mock.patch("core.safety.net.validate_url", return_value=(True, "")):
            self.assertIsNone(oauth.access_token_for(self.server.id, self.user.id))

    def test_tokens_are_scoped_to_their_owner(self):
        self._token()
        stranger = User.objects.create_user("stranger", "s@x.com", "x" * 12)
        self.assertIsNone(oauth.access_token_for(self.server.id, stranger.id))

    def test_disconnect_removes_the_token(self):
        self._token()
        self.assertTrue(oauth.is_connected(self.server.id, self.user.id))
        oauth.disconnect(self.server.id, self.user.id)
        self.assertFalse(oauth.is_connected(self.server.id, self.user.id))


class InjectionTests(TestCase):
    """A completed authorization must reach the transport as a bearer."""

    def setUp(self):
        self.user = User.objects.create_user("inject", "i@x.com", "x" * 12)
        self.server = _server(self.user)
        self.fernet = Fernet(settings.CREDENTIAL_ENCRYPTION_KEY)

    def _connect(self):
        client_row = MCPOAuthClient.objects.create(
            issuer="https://mcp.example.com", redirect_uri=REDIRECT, client_id="abc",
            metadata={"token_endpoint": "https://mcp.example.com/token"},
        )
        MCPOAuthToken.objects.create(
            user=self.user, server=self.server, client=client_row,
            access_token=self.fernet.encrypt(b"live-token"),
            expires_at=timezone.now() + timedelta(hours=1),
        )

    def _resolve(self):
        from asgiref.sync import async_to_sync

        from mcp_integration.credential_injector import CredentialInjector
        return async_to_sync(CredentialInjector.resolve)(self.server, self.user)

    def test_a_connected_server_gets_an_authorization_header(self):
        self._connect()
        self.assertEqual(
            self._resolve().headers["Authorization"], "Bearer live-token",
        )

    def test_an_unconnected_server_gets_no_header(self):
        self.assertNotIn("Authorization", self._resolve().headers)

    def test_oauth_satisfies_a_required_credential_type(self):
        """Connecting is the credential — the user must not be asked twice."""
        from mcp_integration.credential_injector import CredentialMissingError

        self.server.required_credential_types = ["notion"]
        self.server.save(update_fields=["required_credential_types"])

        with self.assertRaises(CredentialMissingError):
            self._resolve()

        self._connect()
        self.assertEqual(self._resolve().headers["Authorization"], "Bearer live-token")

    def test_an_explicit_header_is_not_overwritten(self):
        self._connect()
        self.server.credential_header_map = {"Authorization": "Bearer hand-set"}
        self.server.save(update_fields=["credential_header_map"])
        self.assertEqual(
            self._resolve().headers["Authorization"], "Bearer hand-set",
        )


class EndpointTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user("api", "a@x.com", "x" * 12)
        self.server = _server(self.user)
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_init_returns_an_authorize_url(self):
        with mock.patch.object(oauth, "begin", return_value="https://auth/x"):
            res = self.client.get(
                f"/api/mcp/servers/{self.server.id}/oauth/init/",
                {"redirect_uri": REDIRECT},
            )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["url"], "https://auth/x")

    def test_a_disallowed_redirect_origin_is_refused(self):
        res = self.client.get(
            f"/api/mcp/servers/{self.server.id}/oauth/init/",
            {"redirect_uri": "https://evil.example/callback"},
        )
        self.assertEqual(res.status_code, 400)
        self.assertIn("not allowed", res.data["error"])

    def test_a_missing_redirect_uri_is_refused(self):
        res = self.client.get(f"/api/mcp/servers/{self.server.id}/oauth/init/")
        self.assertEqual(res.status_code, 400)

    def test_an_unconnectable_server_explains_itself(self):
        with mock.patch.object(
            oauth, "begin", side_effect=oauth.MCPOAuthError("no metadata"),
        ):
            res = self.client.get(
                f"/api/mcp/servers/{self.server.id}/oauth/init/",
                {"redirect_uri": REDIRECT},
            )
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.data["code"], "oauth_unavailable")

    def test_callback_requires_code_and_state(self):
        res = self.client.post(
            f"/api/mcp/servers/{self.server.id}/oauth/callback/", {}, format="json",
        )
        self.assertEqual(res.status_code, 400)

    def test_callback_completes(self):
        with mock.patch.object(oauth, "complete", return_value=mock.Mock()):
            res = self.client.post(
                f"/api/mcp/servers/{self.server.id}/oauth/callback/",
                {"code": "c", "state": "s"}, format="json",
            )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data["connected"])

    def test_disconnect_is_idempotent(self):
        for _ in range(2):
            res = self.client.post(
                f"/api/mcp/servers/{self.server.id}/oauth/disconnect/", {}, format="json",
            )
            self.assertEqual(res.status_code, 200)
            self.assertFalse(res.data["connected"])

    def test_another_users_server_is_not_reachable(self):
        stranger = User.objects.create_user("stranger2", "s2@x.com", "x" * 12)
        theirs = _server(stranger, name="theirs")
        res = self.client.get(
            f"/api/mcp/servers/{theirs.id}/oauth/init/", {"redirect_uri": REDIRECT},
        )
        self.assertEqual(res.status_code, 404)

"""
Tests for the connector layer.

Two kinds of test here, and the distinction matters:

* Behavioural tests for the shared base — SSRF, retries, redaction, error
  extraction. These pin the guarantees every connector inherits, so a regression
  in one place is caught once rather than twenty-five times.

* Structural tests over the whole connector set. Things like "every connector's
  credential_type actually exists" cannot be checked by exercising one node,
  because the failure only appears as an empty dropdown in the UI. Seven
  connectors shipped in exactly that state before these tests existed.

No test here makes a real network call. Where an HTTP response is needed it is
constructed directly, so the suite stays fast and works offline.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, TestCase

from core.net import redact_headers, validate_url
from credentials.models import CredentialType
from nodes.handlers.connectors import ALL_CONNECTORS
from nodes.handlers.registry import get_registry
from nodes.handlers.rest_base import (
    ConnectorError,
    RestConnectorNode,
    extract_api_error,
    request_json,
)


def _response(status: int, payload=None, headers: dict | None = None,
              text: str | None = None) -> httpx.Response:
    """Build a real httpx.Response without going near the network."""
    if text is not None:
        content = text.encode()
    else:
        content = json.dumps(payload if payload is not None else {}).encode()
    return httpx.Response(
        status_code=status, content=content,
        headers=headers or {"content-type": "application/json"},
        request=httpx.Request("GET", "https://example.test/"),
    )


# ─────────────────────────────────────────────────────────────────────────
# SSRF
# ─────────────────────────────────────────────────────────────────────────

class SsrfValidationTests(SimpleTestCase):
    BLOCKED = [
        # The one that matters most on EC2: the metadata service hands out IAM
        # role credentials to anything that can make a GET.
        "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
        "http://127.0.0.1:8000/admin/",
        "http://localhost:6379/",
        "http://10.1.2.3/internal",
        "http://192.168.1.1/",
        "http://172.16.0.9/",
        "https://metadata.google.internal/computeMetadata/v1/",
        "http://[::1]:9200/",
        "http://0.0.0.0:80/",
    ]

    def test_internal_targets_are_blocked(self):
        for url in self.BLOCKED:
            ok, reason = validate_url(url)
            self.assertFalse(ok, f"should have blocked {url}")
            self.assertTrue(reason)

    def test_non_http_schemes_are_blocked(self):
        for url in ("file:///etc/passwd", "gopher://x/", "ftp://x/", "data:text/plain,hi"):
            ok, _ = validate_url(url)
            self.assertFalse(ok, url)

    def test_public_urls_are_allowed(self):
        ok, reason = validate_url("https://api.github.com/repos")
        self.assertTrue(ok, reason)

    def test_malformed_url_is_rejected_not_crashed(self):
        ok, _ = validate_url("not-a-url")
        self.assertFalse(ok)

    def test_hostname_only_no_scheme_is_rejected(self):
        ok, _ = validate_url("example.com/path")
        self.assertFalse(ok)


class HeaderRedactionTests(SimpleTestCase):
    def test_sensitive_headers_are_masked(self):
        out = redact_headers({
            "Set-Cookie": "session=secret",
            "Authorization": "Bearer abc",
            "X-Api-Key": "k",
            "Content-Type": "application/json",
        })
        self.assertEqual(out["Set-Cookie"], "[redacted]")
        self.assertEqual(out["Authorization"], "[redacted]")
        self.assertEqual(out["X-Api-Key"], "[redacted]")
        self.assertEqual(out["Content-Type"], "application/json")

    def test_key_is_kept_so_its_presence_is_still_visible(self):
        # Dropping the key would make it look like the server never sent one,
        # which is misleading when that is the thing being debugged.
        self.assertIn("Set-Cookie", redact_headers({"Set-Cookie": "x"}))

    def test_matching_is_case_insensitive(self):
        self.assertEqual(redact_headers({"sEt-CooKie": "x"})["sEt-CooKie"], "[redacted]")


# ─────────────────────────────────────────────────────────────────────────
# Error extraction
# ─────────────────────────────────────────────────────────────────────────

class ApiErrorExtractionTests(SimpleTestCase):
    def test_stripe_shape(self):
        r = _response(400, {"error": {"message": "No such customer: cus_1"}})
        self.assertEqual(extract_api_error(r), "No such customer: cus_1")

    def test_slack_shape(self):
        self.assertEqual(extract_api_error(_response(200, {"error": "channel_not_found"})),
                         "channel_not_found")

    def test_plain_message_shape(self):
        self.assertEqual(extract_api_error(_response(422, {"message": "Validation failed"})),
                         "Validation failed")

    def test_jira_list_shape(self):
        r = _response(400, {"errorMessages": ["Field 'x' is required", "Bad project"]})
        self.assertIn("Field 'x' is required", extract_api_error(r))

    def test_field_error_dict_shape(self):
        r = _response(400, {"errors": {"title": "cannot be blank"}})
        self.assertIn("title", extract_api_error(r))

    def test_non_json_body_falls_back_to_text(self):
        self.assertIn("Gateway", extract_api_error(
            _response(502, text="502 Bad Gateway", headers={"content-type": "text/html"})))

    def test_empty_body_falls_back_to_status(self):
        self.assertEqual(
            extract_api_error(_response(500, text="", headers={"content-type": "text/plain"})),
            "HTTP 500",
        )

    def test_message_is_length_capped(self):
        r = _response(400, {"message": "x" * 5000})
        self.assertLessEqual(len(extract_api_error(r)), 400)


# ─────────────────────────────────────────────────────────────────────────
# request_json: retries and limits
# ─────────────────────────────────────────────────────────────────────────

class RequestJsonTests(SimpleTestCase):
    def test_ssrf_check_runs_before_any_request(self):
        with patch("httpx.AsyncClient.request") as mock_req:
            with self.assertRaises(ConnectorError) as ctx:
                async_to_sync(request_json)("GET", "http://169.254.169.254/")
            self.assertIn("Blocked", str(ctx.exception))
            mock_req.assert_not_called()

    def test_retries_on_429_then_succeeds(self):
        calls = [_response(429, {}, {"Retry-After": "0"}), _response(200, {"ok": True})]
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=calls)) as m:
            body, _ = async_to_sync(request_json)(
                "GET", "https://example.test/x", validate=False)
        self.assertEqual(body, {"ok": True})
        self.assertEqual(m.await_count, 2)

    def test_retry_after_is_honoured_over_backoff(self):
        # If Retry-After is ignored the client keeps hammering a server that has
        # explicitly said when to come back, and earns a longer ban.
        seen = {}

        async def fake_sleep(delay):
            seen["delay"] = delay

        calls = [_response(429, {}, {"Retry-After": "7"}), _response(200, {"ok": 1})]
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=calls)), \
             patch("nodes.handlers.rest_base.asyncio.sleep", new=fake_sleep):
            async_to_sync(request_json)("GET", "https://example.test/x", validate=False)
        self.assertEqual(seen["delay"], 7.0)

    def test_absurd_retry_after_is_capped(self):
        seen = {}

        async def fake_sleep(delay):
            seen["delay"] = delay

        calls = [_response(503, {}, {"Retry-After": "86400"}), _response(200, {"ok": 1})]
        with patch("httpx.AsyncClient.request", new=AsyncMock(side_effect=calls)), \
             patch("nodes.handlers.rest_base.asyncio.sleep", new=fake_sleep):
            async_to_sync(request_json)("GET", "https://example.test/x", validate=False)
        self.assertLessEqual(seen["delay"], 60)

    def test_gives_up_after_max_retries(self):
        with patch("httpx.AsyncClient.request",
                   new=AsyncMock(return_value=_response(503, {"message": "down"}))), \
             patch("nodes.handlers.rest_base.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectorError):
                async_to_sync(request_json)("GET", "https://example.test/x",
                                            validate=False, max_retries=2)

    def test_client_errors_are_not_retried(self):
        # A 400 will be a 400 next time too; retrying only delays the error.
        with patch("httpx.AsyncClient.request",
                   new=AsyncMock(return_value=_response(400, {"message": "bad"}))) as m:
            with self.assertRaises(ConnectorError):
                async_to_sync(request_json)("GET", "https://example.test/x", validate=False)
        self.assertEqual(m.await_count, 1)

    def test_oversized_response_is_refused(self):
        big = _response(200, text="x" * (6 * 1024 * 1024),
                        headers={"content-type": "text/plain"})
        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=big)):
            with self.assertRaises(ConnectorError) as ctx:
                async_to_sync(request_json)("GET", "https://example.test/x", validate=False)
        self.assertIn("too large", str(ctx.exception).lower())

    def test_204_returns_none_rather_than_failing_to_parse(self):
        r = httpx.Response(204, request=httpx.Request("DELETE", "https://example.test/"))
        with patch("httpx.AsyncClient.request", new=AsyncMock(return_value=r)):
            body, _ = async_to_sync(request_json)("DELETE", "https://example.test/x",
                                                  validate=False)
        self.assertIsNone(body)

    def test_timeout_is_reported_clearly(self):
        with patch("httpx.AsyncClient.request",
                   new=AsyncMock(side_effect=httpx.TimeoutException("boom"))), \
             patch("nodes.handlers.rest_base.asyncio.sleep", new=AsyncMock()):
            with self.assertRaises(ConnectorError) as ctx:
                async_to_sync(request_json)("GET", "https://example.test/x",
                                            validate=False, max_retries=1)
        self.assertIn("timed out", str(ctx.exception).lower())

    def test_redirects_are_not_followed(self):
        # Following a redirect would let a public URL bounce to a private one
        # after validation, defeating the SSRF check entirely.
        captured = {}
        real_init = httpx.AsyncClient.__init__

        def spy_init(self, *a, **kw):
            captured["follow_redirects"] = kw.get("follow_redirects")
            return real_init(self, *a, **kw)

        with patch.object(httpx.AsyncClient, "__init__", spy_init), \
             patch("httpx.AsyncClient.request", new=AsyncMock(return_value=_response(200, {}))):
            async_to_sync(request_json)("GET", "https://example.test/x", validate=False)
        self.assertFalse(captured["follow_redirects"])


# ─────────────────────────────────────────────────────────────────────────
# Base-class behaviour
# ─────────────────────────────────────────────────────────────────────────

class _FakeContext:
    def __init__(self, creds=None):
        self._creds = creds

    async def get_credential(self, _cid):
        return self._creds


class _DummyConnector(RestConnectorNode):
    node_type = "dummy_connector"
    name = "Dummy"
    credential_slug = "dummy"
    base_url = "https://example.test"

    async def run_operation(self, operation, config, secret, context):
        if operation == "boom":
            raise RuntimeError("token=sk-live-should-not-surface")
        if operation == "known_failure":
            raise ConnectorError("Field 'x' is required")
        if operation == "list":
            return [{"a": 1}, {"a": 2}]
        return {"secret_seen": secret}


class RestConnectorBaseTests(SimpleTestCase):
    def _run(self, config, creds=None):
        return async_to_sync(_DummyConnector().execute)({}, config, _FakeContext(creds))

    def test_missing_credential_is_a_clear_message(self):
        res = self._run({"operation": "ok"})
        self.assertFalse(res.success)
        self.assertIn("credential", res.error.lower())

    def test_secret_resolved_from_alias_key(self):
        # Credential schemas are inconsistent about naming; a connector failing
        # with "no credential" when one is plainly configured is miserable.
        res = self._run({"operation": "ok", "credential": "c1"}, {"token": "abc"})
        self.assertTrue(res.success)
        self.assertEqual(res.get_data()["secret_seen"], "abc")

    def test_list_result_becomes_one_item_each(self):
        res = self._run({"operation": "list", "credential": "c1"}, {"apiKey": "k"})
        self.assertTrue(res.success)
        self.assertEqual(len(res.items), 2)

    def test_connector_error_is_shown_verbatim(self):
        res = self._run({"operation": "known_failure", "credential": "c1"}, {"apiKey": "k"})
        self.assertFalse(res.success)
        self.assertIn("Field 'x' is required", res.error)

    def test_unexpected_exception_does_not_leak_its_message(self):
        # str(e) on an arbitrary exception can carry a URL with a token in it.
        res = self._run({"operation": "boom", "credential": "c1"}, {"apiKey": "k"})
        self.assertFalse(res.success)
        self.assertNotIn("sk-live", res.error)
        self.assertIn("unexpected error", res.error.lower())

    def test_unsupported_operation_is_reported_not_crashed(self):
        class Bare(_DummyConnector):
            async def run_operation(self, *a, **k):
                raise NotImplementedError("nope")

        res = async_to_sync(Bare().execute)(
            {}, {"operation": "wat", "credential": "c1"}, _FakeContext({"apiKey": "k"}))
        self.assertFalse(res.success)
        self.assertIn("not supported", res.error)

    def test_auth_header_styles(self):
        node = _DummyConnector()
        node.auth_style = "bearer"
        self.assertEqual(node.auth_headers("t")["Authorization"], "Bearer t")
        node.auth_style = "token"
        self.assertEqual(node.auth_headers("t")["Authorization"], "token t")
        node.auth_style = "header"
        node.auth_header = "X-Api-Key"
        self.assertEqual(node.auth_headers("t")["X-Api-Key"], "t")
        node.auth_style = "none"
        self.assertEqual(node.auth_headers("t"), {})


# ─────────────────────────────────────────────────────────────────────────
# Structural checks across every connector
# ─────────────────────────────────────────────────────────────────────────

class ConnectorContractTests(SimpleTestCase):
    def test_there_are_connectors(self):
        # The long tail is commented out of connectors/__init__.py for now, so this
        # guards that the package still registers something rather than counting to
        # the full catalogue. Raise it again if the disabled connectors come back.
        self.assertGreater(len(ALL_CONNECTORS), 0)

    def test_node_types_are_unique(self):
        types = [c.node_type for c in ALL_CONNECTORS]
        duplicates = {t for t in types if types.count(t) > 1}
        self.assertFalse(duplicates, f"duplicate node_type: {duplicates}")

    def test_every_connector_declares_the_basics(self):
        for c in ALL_CONNECTORS:
            self.assertTrue(c.node_type, c.__name__)
            self.assertTrue(c.name, c.__name__)
            self.assertTrue(c.description, c.__name__)
            self.assertTrue(c.fields, c.__name__)

    def test_every_connector_has_a_credential_and_operation_field(self):
        for c in ALL_CONNECTORS:
            names = {f.name for f in c.fields}
            self.assertIn("credential", names, c.__name__)
            self.assertIn("operation", names, c.__name__)

    def test_operation_options_are_declared_and_default_is_valid(self):
        # A default outside the option list renders a select with nothing chosen,
        # and the node then fails with "operation '' is not supported".
        for c in ALL_CONNECTORS:
            op = next(f for f in c.fields if f.name == "operation")
            self.assertTrue(op.options, c.__name__)
            if op.default:
                self.assertIn(op.default, op.options, c.__name__)

    def test_no_connector_hardcodes_a_secret(self):
        import inspect
        import re
        suspicious = re.compile(r'(sk-live|shpat_|glpat-|xoxb-|AKIA)[A-Za-z0-9_\-]{6,}')
        for c in ALL_CONNECTORS:
            src = inspect.getsource(inspect.getmodule(c))
            # Placeholders are fine; a real-looking key with a long tail is not.
            for match in suspicious.findall(src):
                self.fail(f"{c.__name__} may contain a hardcoded secret: {match}")

    def test_every_connector_is_registered(self):
        registry = get_registry()
        for c in ALL_CONNECTORS:
            self.assertTrue(registry.has_handler(c.node_type), c.node_type)


class ConnectorCredentialSeedTests(TestCase):
    """
    The check that would have caught the seven pre-existing broken connectors.

    A node's credential_type is a foreign key in all but enforcement: if no
    CredentialType has that slug, the picker is empty and the connector cannot be
    configured, with nothing logged to explain it.
    """

    def setUp(self):
        from django.core.management import call_command
        call_command("seed_connector_credentials", verbosity=0)

    def test_every_connector_credential_type_is_seeded(self):
        seeded = set(CredentialType.objects.values_list("slug", flat=True))
        missing = []
        for c in ALL_CONNECTORS:
            for f in c.fields:
                if f.credential_type and f.credential_type not in seeded:
                    missing.append(f"{c.__name__} -> {f.credential_type}")
        self.assertFalse(
            missing,
            "Connectors reference credential types that are not seeded, so their "
            "credential dropdown will be empty:\n  " + "\n  ".join(missing),
        )

    def test_seeding_is_idempotent(self):
        from django.core.management import call_command
        before = CredentialType.objects.count()
        call_command("seed_connector_credentials", verbosity=0)
        self.assertEqual(CredentialType.objects.count(), before)

    def test_previously_broken_types_now_exist(self):
        # These seven were referenced by shipped nodes with no matching type.
        for slug in ("aws", "serpapi", "wolfram_alpha", "openweathermap",
                     "bing_search", "huggingface-api", "xai-api"):
            self.assertTrue(
                CredentialType.objects.filter(slug=slug).exists(),
                f"{slug} still missing",
            )

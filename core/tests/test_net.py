"""
Tests for `core.safety.net` — SSRF validation and header redaction.

These moved here from `nodes/tests/test_connectors.py` when the REST connector
pack was deleted with the DAG runtime. The subjects never belonged to the
connectors: `validate_url` guards `chat/turn/pipeline.py` (the URLs a chat turn
fetches) and `mcp_integration/serializers.py` (the server URL a user registers),
both of which take a URL from a user and hand it to an HTTP client. Losing the
connectors must not silently lose the coverage on that.
"""
from __future__ import annotations

from django.test import SimpleTestCase

from core.safety.net import check_egress, redact_headers, validate_url


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


class EgressGuardTests(SimpleTestCase):
    def test_ssrf_refusal_wins_over_the_allowlist(self):
        ok, reason = check_egress(
            'http://169.254.169.254/latest/meta-data/',
            {'apiHosts': ['169.254.169.254']},
        )
        self.assertFalse(ok)
        self.assertTrue(reason)

    def test_a_listed_host_passes_with_its_subdomains(self):
        # github.com resolves in CI (see SsrfValidationTests above); the point
        # here is the allowlist match, not the DNS.
        for target in ('https://api.github.com/repos', 'https://github.com/'):
            ok, reason = check_egress(target, {'apiHosts': ['github.com']})
            self.assertTrue(ok, f'{target}: {reason}')

    def test_an_unlisted_host_is_refused_with_a_usable_reason(self):
        ok, reason = check_egress(
            'https://api.github.com/repos', {'apiHosts': ['example.com']},
        )
        self.assertFalse(ok)
        self.assertIn('allowlist', reason)

    def test_a_suffix_trick_does_not_match(self):
        ok, _ = check_egress(
            'https://example.com.evil.com/', {'apiHosts': ['example.com']},
        )
        self.assertFalse(ok)

    def test_no_allowlist_means_the_ssrf_answer_stands(self):
        ok, _ = check_egress('https://api.github.com/repos', {})
        self.assertTrue(ok)
        ok, _ = check_egress('not-a-url', {})
        self.assertFalse(ok)

    def test_bare_hosts_are_accepted_for_database_scopes(self):
        ok, _ = check_egress('db.example.com', {'dbHosts': ['example.com']})
        self.assertTrue(ok)

    def test_a_bare_private_ip_is_refused_without_dns(self):
        ok, reason = check_egress('169.254.169.254', {'dbHosts': ['example.com']})
        self.assertFalse(ok)
        self.assertTrue(reason)
